#!/usr/bin/env bash
# Matriz de la issue #38: configuraciones A, C y C+CB de extract detrás de
# Traefik, con el Vegeta y el k6 de la cátedra, en corridas alternadas.
#
# Por cada corrida: recrea extract con la imagen fija y las variables de la
# configuración (solo en el comando, nunca en un .env), espera a que Traefik
# rutee las réplicas nuevas con los middlewares esperados, deja SETTLE segundos
# de reposo, corre la herramienta y guarda en
#   tests/stress/results/38/<STAMP>/<n>-<config>-<herramienta>/
# el reporte, los resultados crudos, "make images", las variables, /metrics de
# Traefik antes y después, el access log de Traefik y el log de extract de la
# ventana, docker stats y las horas de inicio y fin.
#
# Orden: en cada ronda, las tres configuraciones con una herramienta y después
# con la otra (A, C, C+CB, A, C, C+CB, ...); la herramienta que va primero se
# alterna entre rondas. Nunca hay dos corridas seguidas de la misma config.
#
# Requiere: infra (Consul y Traefik) y documents levantados ("make wait-routes"
# exige las dos rutas), docker, curl y jq. Uso y variables: ver
# tests/stress/README.md, "Matriz de la issue #38".

set -euo pipefail

REPO=$(cd "$(dirname "$0")/../../.." && pwd)
INFRA_DIR=${INFRA_DIR:-$REPO/../pdf_extractext-infra}

IMAGE_TAG=${IMAGE_TAG:-sha-ec5b762}
ROUNDS=${ROUNDS:-3}
CONFIGS=${CONFIGS:-A C C+CB}
TOOLS=${TOOLS:-vegeta k6}
VEGETA_RATE=${VEGETA_RATE:-50}
VEGETA_DURATION=${VEGETA_DURATION:-30s}
CLIENT_TIMEOUT=${CLIENT_TIMEOUT:-30s}
SETTLE=${SETTLE:-60}
# Número de la primera corrida: para retomar una matriz cortada sin pisar carpetas.
FIRST_RUN=${FIRST_RUN:-1}
STAMP=${STAMP:-$(date +%Y-%m-%d)}
OUT_ROOT=${OUT_ROOT:-$REPO/tests/stress/results/38/$STAMP}

# Versiones fijas de las herramientas (tag + digest).
VEGETA_IMAGE=peterevans/vegeta:6.9.1-vegeta12.8.4@sha256:eb65f499cd1b0f1402a56794d7711c49121db6ff8ea7d878513d76601ee0d502
K6_IMAGE=grafana/k6:2.2.0@sha256:9bd01d6941fca969cb61bb57d2da5ee9b385fe2aa8881df3798c196564d6ace6

TRAEFIK_API=${TRAEFIK_API:-http://127.0.0.1:8081}
TRAEFIK_CONTAINER=${TRAEFIK_CONTAINER:-pdf_extractext-infra-traefik-1}
CONSUL_CONTAINER=${CONSUL_CONTAINER:-pdf_extractext-infra-consul-1}

# El CB de producción (default de docker-compose.yml) y uno presente que nunca
# abre: NetworkErrorRatio() es un cociente entre 0 y 1. El literal tiene que
# ser flotante: con "> 1" Traefik v3.7 rechaza la expresión ("expected int,
# got int") y deja el router de extract deshabilitado.
CB_PRODUCTION='NetworkErrorRatio() > 0.30 || ResponseCodeRatio(500, 503, 0, 600) > 0.50'
CB_NEVER='NetworkErrorRatio() > 1.0'
REPLICAS=5

log() { printf '[run-38 %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
now() { date -u +%Y-%m-%dT%H:%M:%S.%NZ; }

# config_env CONFIG: las variables de extract de esa configuración, una por
# línea. Todas explícitas, para que ningún valor salga del .env (salvo el token
# de Consul, que Compose toma de ahí y nunca se escribe en los resultados).
config_env() {
  local uploads queue_wait cb
  case $1 in
    A) uploads=0 queue_wait=1000 cb=$CB_NEVER ;;
    C) uploads=8 queue_wait=15 cb=$CB_NEVER ;;
    C+CB) uploads=8 queue_wait=15 cb=$CB_PRODUCTION ;;
    *) die "configuración desconocida: $1 (A, C o C+CB)" ;;
  esac
  cat <<EOF
EXTRACT_IMAGE_TAG=$IMAGE_TAG
EXTRACT_REPLICAS=$REPLICAS
EXTRACT_CPUS=1.0
EXTRACT_MEMORY=1g
EXTRACT_THREAD_POOL_SIZE=1
EXTRACT_MAX_UPLOAD_SIZE=52428800
EXTRACT_MAX_CONCURRENT_UPLOADS=$uploads
EXTRACT_UPLOAD_SLOT_TIMEOUT=5
EXTRACT_MAX_QUEUE_WAIT=$queue_wait
EXTRACT_EXTRACTION_DEADLINE=8
EXTRACT_MAX_PAGES=10000
EXTRACT_READINESS_STUCK_AFTER=12
EXTRACT_LOG_LEVEL=INFO
EXTRACT_LOG_FORMAT=json
TRAEFIK_CB_EXPRESSION=$cb
TRAEFIK_CB_FALLBACK=10s
TRAEFIK_CB_RECOVERY=10s
TRAEFIK_RETRY_ATTEMPTS=2
TRAEFIK_INFLIGHT=80
SHARED_NETWORK=mired
EOF
}

# compose_extract ENVFILE ARGS...: docker compose del repo, sin el override
# (imagen de GHCR, sin build) y con las variables de ENVFILE en el entorno.
# Como el Makefile de infra, borra del entorno lo que pisaría el .env.
compose_extract() {
  local envfile=$1 vars=()
  shift
  mapfile -t vars <"$envfile"
  env -u CONSUL_HTTP_TOKEN -u CONSUL_HTTP_ADDR -u CONSUL_ENABLED \
    -u COMPOSE_PROJECT_NAME -u COMPOSE_FILE "${vars[@]}" \
    docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

extract_ids() { docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" ps -q extract; }

# wait_traefik: hasta que los servidores de extract en Traefik sean
# exactamente las IPs de los contenedores nuevos y el router esté habilitado.
# Contar servidores no alcanza: durante el recreate Traefik todavía muestra
# las réplicas viejas.
wait_traefik() {
  local want have status
  for _ in $(seq 90); do
    want=$(extract_ids | xargs docker inspect \
      --format '{{(index .NetworkSettings.Networks "mired").IPAddress}}' \
      | sed 's|^|http://|; s|$|:8001|' | sort | paste -sd, -)
    have=$(curl -fsS --max-time 3 "$TRAEFIK_API/api/http/services/extract@consulcatalog" 2>/dev/null \
      | jq -r '[.loadBalancer.servers[].url] | sort | join(",")' 2>/dev/null || true)
    status=$(curl -fsS --max-time 3 "$TRAEFIK_API/api/http/routers/extract@consulcatalog" 2>/dev/null \
      | jq -r '.status' 2>/dev/null || true)
    if [[ -n $want && $want == "$have" && $status == enabled ]]; then
      return 0
    fi
    sleep 2
  done
  die "Traefik no rutea las réplicas nuevas (quiero $want, tiene $have, router $status)"
}

# check_routing DIR EXPRESION: middlewares en Traefik y tags y checks en Consul.
check_routing() {
  local dir=$1 expression=$2 consul
  {
    for m in cb retry inflight; do
      curl -fsS "$TRAEFIK_API/api/http/middlewares/extract-$m@consulcatalog" | jq -c '{name, status, circuitBreaker, retry, inFlightReq}'
    done
  } >"$dir/traefik-middlewares.jsonl"
  jq -e -s --arg e "$expression" '
    .[0].circuitBreaker.expression == $e and .[0].status == "enabled"
    and .[1].retry.attempts == 2 and .[2].inFlightReq.amount == 80' \
    "$dir/traefik-middlewares.jsonl" >/dev/null \
    || die "middlewares inesperados en Traefik: ver $dir/traefik-middlewares.jsonl"

  # consul watch sin handler imprime el estado y sale. Corre dentro del
  # contenedor de Consul con su propio token: acá nunca se ve.
  consul=$(docker exec "$CONSUL_CONTAINER" consul watch -type=service -service=extract)
  jq -c '[.[] | {id: .Service.ID, address: .Service.Address,
      checks: [.Checks[] | {name: .Name, status: .Status}],
      tags: (.Service.Tags | sort)}]' <<<"$consul" >"$dir/consul-extract.json"
  jq -e --argjson n "$REPLICAS" --arg tag "traefik.http.middlewares.extract-cb.circuitbreaker.expression=$expression" '
    length == $n
    and all(.[]; all(.checks[]; .status == "passing"))
    and ([.[].tags] | unique | length) == 1
    and (.[0].tags | index($tag) != null)' "$dir/consul-extract.json" >/dev/null \
    || die "Consul: réplicas no passing o con tags distintos: ver $dir/consul-extract.json"
}

# apply_config CONFIG DIR: recrea extract con la configuración y espera.
apply_config() {
  local config=$1 dir=$2 expression
  config_env "$config" >"$dir/run.env"
  expression=$(sed -n 's/^TRAEFIK_CB_EXPRESSION=//p' "$dir/run.env")
  log "$config: recreando extract ($IMAGE_TAG)"
  compose_extract "$dir/run.env" up -d --force-recreate --wait >"$dir/compose-up.log" 2>&1 \
    || die "falló el up: ver $dir/compose-up.log"
  wait_traefik
  make --no-print-directory -C "$INFRA_DIR" wait-routes >"$dir/wait-routes.txt" 2>&1 \
    || die "make wait-routes falló: ver $dir/wait-routes.txt"
  check_routing "$dir" "$expression"
  log "$config: rutas OK, reposo de ${SETTLE}s"
  sleep "$SETTLE"
  # Tras el reposo siguen las 5 réplicas, passing y con los mismos tags.
  wait_traefik
  check_routing "$dir" "$expression"
}

metrics_snapshot() {
  curl -fsS "$TRAEFIK_API/metrics" \
    | grep -E '^traefik_(entrypoint|service)_requests_total|^traefik_service_retries_total|^traefik_open_connections' \
    || true
}

# Muestras de CPU y memoria de Traefik y extract cada ~2 s, hasta que exista
# el archivo DIR/.stop.
sample_stats() {
  local dir=$1 containers
  mapfile -t containers < <(extract_ids)
  containers+=("$TRAEFIK_CONTAINER")
  while [[ ! -e $dir/.stop ]]; do
    docker stats --no-stream --format '{{json .}}' "${containers[@]}" \
      | jq -c --arg t "$(now)" '{t: $t, name: .Name, cpu: .CPUPerc, mem: .MemUsage}' || true
  done >"$dir/docker-stats.jsonl"
}

run_vegeta() {
  local dir=$1
  docker run --rm --name bench38-vegeta --user "$(id -u):$(id -g)" --network mired \
    -v "$REPO/tests/stress:/stress:ro" -v "$dir:/out" \
    -e RATE="$VEGETA_RATE" -e DURATION="$VEGETA_DURATION" -e TIMEOUT="$CLIENT_TIMEOUT" \
    --entrypoint sh "$VEGETA_IMAGE" -c '
      set -e
      vegeta attack -targets=/stress/profesor/test_carga.txt -rate="$RATE" \
        -duration="$DURATION" -timeout="$TIMEOUT" > /out/vegeta-results.bin
      vegeta report /out/vegeta-results.bin > /out/vegeta-report.txt
      vegeta report -type=json /out/vegeta-results.bin > /out/vegeta-report.json
      vegeta encode -to json /out/vegeta-results.bin | jq -c "{t: .timestamp, code: .code,
        latency: .latency, bytes_in: .bytes_in, error: .error,
        body: (if .code == 200 then null else (.body | @base64d) end)}" > /out/vegeta-requests.jsonl
    '
}

run_k6() {
  local dir=$1
  docker run --rm --name bench38-k6 --user "$(id -u):$(id -g)" --network mired \
    -v "$REPO/tests/stress:/stress:ro" -v "$dir:/out" \
    -e BASE_URL=http://traefik -e PDF_DIR=/stress/pdfs -e TIMEOUT="$CLIENT_TIMEOUT" \
    "$K6_IMAGE" run --summary-export=/out/k6-summary.json --out json=/out/k6-raw.json \
    /stress/profesor/spike_tests.js >"$dir/k6-report.txt" 2>&1
  # Una línea por pedido: código (0 si no hubo respuesta), error_code y duración.
  jq -c 'select(.type == "Point" and .metric == "http_req_duration")
    | {t: .data.time, code: ((.data.tags.status // "0") | tonumber),
       error_code: (.data.tags.error_code // null), duration_ms: .data.value}' \
    "$dir/k6-raw.json" >"$dir/k6-requests.jsonl"
}

# run_one N CONFIG TOOL
run_one() {
  local n=$1 config=$2 tool=$3 dir start end
  dir="$OUT_ROOT/$(printf '%02d' "$n")-$config-$tool"
  mkdir -p "$dir"
  apply_config "$config" "$dir"
  {
    cat "$dir/run.env"
    echo "CONFIG=$config"
    echo "TOOL=$tool"
    echo "VEGETA_IMAGE=$VEGETA_IMAGE"
    echo "K6_IMAGE=$K6_IMAGE"
    echo "VEGETA_RATE=$VEGETA_RATE"
    echo "VEGETA_DURATION=$VEGETA_DURATION"
    echo "CLIENT_TIMEOUT=$CLIENT_TIMEOUT"
    echo "SETTLE=$SETTLE"
  } >"$dir/run.vars"
  make --no-print-directory -C "$INFRA_DIR" images >"$dir/images.txt" 2>&1

  metrics_snapshot >"$dir/metrics-before.prom"
  rm -f "$dir/.stop"
  sample_stats "$dir" &
  local sampler=$!
  start=$(now)
  log "$config/$tool: corriendo"
  "run_$tool" "$dir" || log "AVISO: $tool terminó con error (ver $dir)"
  end=$(now)
  touch "$dir/.stop"
  wait "$sampler" || true
  rm -f "$dir/.stop"
  # Margen para que Traefik escriba las últimas líneas del access log.
  sleep 3
  metrics_snapshot >"$dir/metrics-after.prom"
  local until
  until=$(date -u -d "$end + 3 seconds" +%Y-%m-%dT%H:%M:%S.%NZ)
  printf 'START=%s\nEND=%s\nLOAD_BEFORE=%s\n' "$start" "$end" "$(cut -d' ' -f1-3 /proc/loadavg)" >"$dir/times.env"

  docker logs --since "$start" --until "$until" "$TRAEFIK_CONTAINER" >"$dir/traefik.raw" 2>&1
  grep '^{' "$dir/traefik.raw" >"$dir/traefik-access.jsonl" || true
  grep -v '^{' "$dir/traefik.raw" >"$dir/traefik-log.txt" || true
  rm -f "$dir/traefik.raw"

  local id name
  for id in $(extract_ids); do
    name=$(docker inspect --format '{{.Name}}' "$id")
    docker logs --since "$start" --until "$until" "$id" 2>&1 \
      | jq -R -c --arg r "${name#/}" 'fromjson? // {raw: .} | . + {replica: $r}' \
      | grep -v '/health' || true
  done >"$dir/extract.jsonl"
  log "$config/$tool: listo ($dir)"
}

main() {
  [[ -n $IMAGE_TAG && $IMAGE_TAG != latest ]] || die "IMAGE_TAG tiene que ser un sha-xxxxxxx o vX.Y.Z, no latest"
  command -v jq >/dev/null || die "falta jq"
  [[ -d $INFRA_DIR ]] || die "no existe INFRA_DIR=$INFRA_DIR"
  curl -fsS --max-time 3 "$TRAEFIK_API/api/overview" >/dev/null || die "Traefik no responde en $TRAEFIK_API"
  if docker ps --format '{{.Names}}' | grep -q '^bench38-'; then
    die "ya hay una corrida en curso (contenedor bench38-*)"
  fi
  mkdir -p "$OUT_ROOT"
  log "salida: $OUT_ROOT"

  local n=$((FIRST_RUN - 1)) round tools config tool
  for ((round = 1; round <= ROUNDS; round++)); do
    read -r -a tools <<<"$TOOLS"
    if ((round % 2 == 0)); then
      mapfile -t tools < <(printf '%s\n' "${tools[@]}" | tac)
    fi
    for tool in "${tools[@]}"; do
      for config in $CONFIGS; do
        n=$((n + 1))
        run_one "$n" "$config" "$tool"
      done
    done
  done
  log "matriz terminada: corridas $FIRST_RUN a $n en $OUT_ROOT"
}

# Ejecutado (no incluido con "source" por run-38-documents.sh): corre la matriz.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
