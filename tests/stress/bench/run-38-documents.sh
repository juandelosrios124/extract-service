#!/usr/bin/env bash
# Corrida del camino real de la issue #38: POST /documents por Traefik, con
# extract en la configuración de producción (C+CB). documents llama a extract
# por Traefik (http://traefik/extract), así que pasa por los mismos
# middlewares que la matriz.
#
# Cada pedido manda un PDF con checksum distinto (pdfs_unicos.py): con el mismo
# PDF, documents respondería 200 duplicate sin llamar a extract. Vegeta corre
# con -lazy para leer los cuerpos de a uno (son unos 5 GB) y se detiene si se
# acaban los targets, en vez de repetirlos.
#
# Precondiciones:
#   - documents levantado sobre una base propia, nunca documents_db:
#       MONGO_DB_NAME=documents_bench make up RELEASE=1 EXTRACT_IMAGE_TAG=... DOCUMENTS_IMAGE_TAG=...
#   - las variantes generadas:
#       uv run python tests/stress/bench/pdfs_unicos.py --cantidad 1600 --verificar
#
# Guarda lo mismo que run-38.sh, más el log de documents de la ventana. Usa
# sus funciones y variables (IMAGE_TAG, SETTLE, VEGETA_RATE, ...).

set -euo pipefail

# shellcheck source=SCRIPTDIR/run-38.sh
source "$(dirname "$0")/run-38.sh"

DOCS_DIR=${DOCS_DIR:-$REPO/../documents-service}
DOCS_DB=${DOCS_DB:-documents_bench}
DOCS_CONFIG=${DOCS_CONFIG:-C+CB}
UNICOS_MOUNT=${UNICOS_MOUNT:-/stress/pdfs_provisorios/unicos-38}
UNICOS_DIR=$REPO/tests/stress/${UNICOS_MOUNT#/stress/}
RUN_NUMBER=${RUN_NUMBER:-19}

documents_ids() {
  docker compose --project-directory "$DOCS_DIR" -f "$DOCS_DIR/docker-compose.yml" ps -q documents
}

# La base de documents sale del entorno del contenedor; se lee solo esa clave.
check_documents() {
  local ids db
  mapfile -t ids < <(documents_ids)
  ((${#ids[@]} > 0)) || die "documents no está corriendo ($DOCS_DIR)"
  db=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "${ids[@]}" \
    | sed -n 's/^MONGO_DB_NAME=//p' | sort -u | paste -sd, -)
  [[ $db == "$DOCS_DB" ]] || die "documents usa MONGO_DB_NAME=$db y no $DOCS_DB"
}

run_documents() {
  local dir=$1 start until id
  {
    echo "TARGET_PATH=/documents"
    echo "DOCS_DB=$DOCS_DB"
    echo "UNICOS=$UNICOS_MOUNT/targets.txt"
  } >>"$dir/run.vars"
  start=$(now)
  docker run --rm --name bench38-vegeta --user "$(id -u):$(id -g)" --network mired \
    -v "$REPO/tests/stress:/stress:ro" -v "$dir:/out" \
    -e RATE="$VEGETA_RATE" -e DURATION="$VEGETA_DURATION" -e TIMEOUT="$CLIENT_TIMEOUT" \
    -e TARGETS="$UNICOS_MOUNT/targets.txt" \
    --entrypoint sh "$VEGETA_IMAGE" -c '
      set -e
      vegeta attack -lazy -targets="$TARGETS" -rate="$RATE" \
        -duration="$DURATION" -timeout="$TIMEOUT" > /out/vegeta-results.bin
      vegeta report /out/vegeta-results.bin > /out/vegeta-report.txt
      vegeta report -type=json /out/vegeta-results.bin > /out/vegeta-report.json
      vegeta encode -to json /out/vegeta-results.bin | jq -c "{t: .timestamp, code: .code,
        latency: .latency, bytes_in: .bytes_in, error: .error,
        duplicate: (if .code == 200 or .code == 201
                    then (.body | @base64d | fromjson? | .duplicate) else null end),
        body: (if .code == 200 or .code == 201 then null else (.body | @base64d) end)}" \
        > /out/vegeta-requests.jsonl
    '
  sleep 3
  until=$(now)
  for id in $(documents_ids); do
    docker logs --since "$start" --until "$until" "$id" 2>&1 \
      | jq -R -c 'fromjson? // {raw: .}' | grep -v '/health' || true
  done >"$dir/documents.jsonl"
}

main_documents() {
  [[ -n $IMAGE_TAG && $IMAGE_TAG != latest ]] || die "IMAGE_TAG tiene que ser un sha-xxxxxxx o vX.Y.Z"
  curl -fsS --max-time 3 "$TRAEFIK_API/api/overview" >/dev/null || die "Traefik no responde"
  [[ -f $UNICOS_DIR/targets.txt ]] || die "faltan las variantes: correr pdfs_unicos.py"
  local targets needed
  targets=$(grep -c '^POST ' "$UNICOS_DIR/targets.txt")
  needed=$((VEGETA_RATE * ${VEGETA_DURATION%s}))
  ((targets >= needed)) || die "hay $targets variantes y la corrida manda $needed pedidos"
  check_documents
  mkdir -p "$OUT_ROOT"
  run_one "$RUN_NUMBER" "$DOCS_CONFIG" documents
}

main_documents "$@"
