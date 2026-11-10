# extract-service

Microservicio sin estado que extrae el texto de un PDF. Trabajo práctico de Desarrollo de Software (UTN Facultad Regional San Rafael): test de carga, estrés y optimización.

Las mediciones, las decisiones de diseño y el análisis del cuello de botella están en [`informe.md`](informe.md).

## Inicio rápido

Requisitos: Docker con Compose v2 (se usa `--wait`) y unos 4 GB de RAM libres. Las 5 réplicas tienen un tope de 1 GiB cada una; en la práctica usan bastante menos.

Para levantar el sistema completo en orden (infra, extract y documents), lo recomendado es `make up` en `pdf_extractext-infra`: ver [Levantar el sistema completo](https://github.com/juandelosrios124/pdf_extractext-infra#levantar-el-sistema-completo). Lo que sigue levanta solo este stack. En los dos casos, el `.env` de este repo tiene que tener el token de Consul (ver [Service discovery con Consul](#service-discovery-con-consul)).

```bash
git clone https://github.com/juandelosrios124/extract-service.git
cd extract-service

# Red externa compartida con Consul, Traefik y documents-service: una sola vez
# por maquina. Si ya existe (la pudo crear otro repo), ignorar el error.
docker network create mired

docker compose up --build -d --wait
```

Así se compila la imagen en la máquina (desarrollo, lo mismo que hace `make up` de infra). Para correr la imagen que publica la CI, sin compilar, ver [Imagen y releases](#imagen-y-releases-12-factor-v). La versión que se midió en el informe es la imagen `sha-ec5b762` (`EXTRACT_IMAGE_TAG=sha-ec5b762 docker compose -f docker-compose.yml up -d --wait`); no hay tag de git `v1.1`.

Levanta **5 réplicas** (1,0 CPU y 1 GiB cada una). Este compose **no publica ningún puerto**: el punto de entrada es **Traefik**, que vive en el repo de [infra](https://github.com/juandelosrios124/pdf_extractext-infra) y descubre las réplicas por el catálogo de Consul. Hay que levantarlo ahí:

```bash
docker compose up -d --wait        # en pdf_extractext-infra: Consul y Traefik
```

Traefik queda publicado en `http://127.0.0.1:8080` (el tráfico) y su dashboard en `http://127.0.0.1:8081`. Probar un pedido:

```bash
curl -s -H "Content-Type: application/pdf" --data-binary @tests/stress/pdfs/Filosofia_Lean.pdf http://127.0.0.1:8080/extract | head -c 300
```

En PowerShell: `curl.exe -s -H "Content-Type: application/pdf" --data-binary "@tests/stress/pdfs/Filosofia_Lean.pdf" http://127.0.0.1:8080/extract`.

Se usa `127.0.0.1` y no `localhost` porque Traefik se publica solo en IPv4 y `localhost` puede resolver a `::1`.

Estado y limpieza:

```bash
docker compose ps        # 5 réplicas "healthy"
docker compose down
```

## API

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/extract` | Recibe un PDF como cuerpo binario (`Content-Type: application/pdf`) o como `multipart/form-data` (campo `file`) y responde `{"content": "...", "page_count": N}`. `content` es texto plano |
| GET | `/health` | **Liveness**: el proceso está vivo. Lo usa el `HEALTHCHECK` de Docker |
| GET | `/health/ready` | **Readiness**: además, el hilo de extracción no está trabado. `503` si lo está. Lo usa el check de Consul, y con eso Traefik saca a la réplica del balanceo |

Errores: `400` (sin PDF o PDF inválido), `413` (supera `MAX_UPLOAD_SIZE`), `422` (más páginas que `MAX_PAGES`) y `503` con `Retry-After: 1` cuando el servicio está saturado (sin cupo, pedido vencido en la cola, o extracción cortada por `EXTRACTION_DEADLINE`). Un `503` indica reintentar más tarde; no es una falla del servicio. Un `422`, en cambio, no se arregla reintentando: el documento es el problema, y por eso no lleva `Retry-After`.

**Correlación.** Todo pedido acepta el header `X-Request-ID`. Si llega y cumple `[A-Za-z0-9-]{1,64}`, se respeta; si no llega, o no cumple (vacío, largo, con espacios, saltos de línea, acentos o caracteres de control), se genera uno nuevo con `uuid4().hex` y se loguea a nivel `info` que se reemplazó, con a lo sumo los primeros 64 caracteres del valor recibido, escapados. Son las mismas reglas que `documents-service` (#69): un id inválido podría falsificar líneas de log. El id vuelve en la respuesta y aparece en cada línea de log del pedido, incluido el access log de uvicorn y lo que loguea la extracción. Es el mismo header que propaga `documents-service`, así que un pedido se puede seguir por los dos servicios con el mismo id.

## Configuración y valores por defecto

Todo se configura por variables de entorno (Twelve-Factor). Los valores por defecto ya están en `docker-compose.yml` y documentados en `.env.example`. Para cambiarlos, copiá `.env.example` a `.env` o exportá la variable antes de `docker compose up`. Los valores por defecto del código están en `extractor/config.py` y son los mismos, así que fuera de Compose el servicio arranca con esta misma configuración.

| Variable de Compose | Valor | Por qué |
|---|---|---|
| `EXTRACT_REPLICAS` | `5` | Máximo permitido por la consigna |
| `EXTRACT_CPUS` | `1.0` | Límite de CPU por réplica pedido por la consigna; define cuántos hilos conviene usar |
| `EXTRACT_MEMORY` | `1g` | Tope de la consigna (512 MB a 1 GB); el uso real es mucho menor |
| `EXTRACT_THREAD_POOL_SIZE` | `1` | Un worker por núcleo asignado. La extracción consume CPU: con 4 hilos sobre 1,0 CPU solo había contención y latencias de 22 s o más |
| `EXTRACT_MAX_CONCURRENT_UPLOADS` | `8` | Cada réplica atiende como máximo 8 pedidos a la vez; acota la memoria y evita acumular pedidos que van a expirar |
| `EXTRACT_UPLOAD_SLOT_TIMEOUT` | `5` | Segundos que un pedido espera un cupo (sin leer el cuerpo) antes de recibir `503` |
| `EXTRACT_MAX_QUEUE_WAIT` | `15` | Si un pedido ya esperó 15 s antes de empezar a extraer, se descarta con `503` sin gastar CPU |
| `EXTRACT_EXTRACTION_DEADLINE` | `8` | Segundos máximos de extracción, chequeados **entre páginas**. Pasado eso, `503`: sin esto un PDF pesado congela la réplica, porque la extracción corre en el pool y no se puede cancelar |
| `EXTRACT_READINESS_STUCK_AFTER` | `12` | Segundos tras los cuales se considera que la extracción se trabó y `/health/ready` responde `503`. **Mayor que el deadline** a propósito: por debajo, una extracción legítima marcaría mal a una réplica sana |
| `EXTRACT_MAX_PAGES` | `10000` | Tope duro de páginas. Por encima se responde `422` sin extraer nada. No está afinado contra el deadline: es un techo para entradas absurdas |
| `EXTRACT_MAX_UPLOAD_SIZE` | `52428800` | 50 MiB; el cuerpo se corta apenas se supera |
| `EXTRACT_LOG_LEVEL` | `INFO` | Logs a stdout |
| `EXTRACT_LOG_FORMAT` | `json` | `json` emite un objeto por línea con `request_id`, para recolectores de logs; `text` queda legible en una terminal. El default del código es `text` |
| `EXTRACT_IMAGE_TAG` | `latest` | Solo para `docker compose -f docker-compose.yml`: tag de `ghcr.io/juandelosrios124/extract-service` que se corre. Vacío o `latest` en desarrollo; `sha-xxxxxxx` o `vX.Y.Z` para una release o una medición reproducible. Con el override (desarrollo, `make up`) no se usa. Ver [Imagen y releases](#imagen-y-releases-12-factor-v) |
| `SHARED_NETWORK` | `mired` | Nombre de la red externa compartida con Consul y documents-service. Es la misma variable en los tres repos: si se cambia, hay que cambiarla en todos |
| `CONSUL_ENABLED` | `true` | Autorregistro en Consul, que es cómo Traefik descubre las réplicas (#32). En `false` el servicio funciona igual, pero queda sin publicar |
| `CONSUL_HTTP_ADDR` | `http://consul:8500` | Agente de Consul, visto desde `mired` |
| `CONSUL_HTTP_TOKEN` | *(vacío)* | Token de ACL de Consul. Va en el `.env` (no versionado), con el valor de `CONSUL_EXTRACT_TOKEN` del `.env` de infra: ver [Qué va en el `.env` de cada repo](https://github.com/juandelosrios124/pdf_extractext-infra#qué-va-en-el-env-de-cada-repo) |

Los timeouts del borde ya no son de este repo: los define Traefik y están en el repo de [infra](https://github.com/juandelosrios124/pdf_extractext-infra), documentados en su tabla «Presupuesto de timeouts».

**Por qué esta configuración.** En la prueba de la cátedra (Vegeta, 50 req/s durante 30 s) la configuración sin límites logró 68,93 % de éxito y 17,30 req/s; con el límite de 8 por réplica, **76,13 % y 20,06 req/s**, con 262 timeouts contra 465 y 96 respuestas `503` controladas. La referencia de la cátedra es 66,53 % y 16,65 req/s. Los valores 8 cupos, 5 s de espera, 15 s de descarte y 8 s de deadline son un primer ajuste razonado, no un óptimo: los tres tiempos suman los 23 s que le tocan a extract en el presupuesto de punta a punta (cliente 30 s > documents 27 s > Traefik 25 s > extract 23 s), definido en el repo de [infra](https://github.com/juandelosrios124/pdf_extractext-infra). Las mediciones del informe se hicieron con 15 s y 20 s; volver a medirlas es la #38.

Para reproducir la configuración **sin límites** (A):

```bash
EXTRACT_MAX_CONCURRENT_UPLOADS=0 EXTRACT_MAX_QUEUE_WAIT=1000 docker compose up -d --force-recreate --wait
```

En PowerShell: `$env:EXTRACT_MAX_CONCURRENT_UPLOADS="0"; $env:EXTRACT_MAX_QUEUE_WAIT="1000"; docker compose up -d --force-recreate --wait`.

## Imagen y releases (12-Factor V)

La imagen se construye **una sola vez por commit**, en la CI, y se publica en GHCR: `ghcr.io/juandelosrios124/extract-service`. Desplegar no compila: una **release es una imagen con tag + el `.env`** de la máquina. Dos máquinas con el mismo tag corren exactamente el mismo binario, y volver atrás es cambiar el tag.

**Qué publica la CI y cuándo** (job `image` de `.github/workflows/ci.yml`, que corre solo si pasó `check`: lint, tests, compose y auditoría):

| Evento | ¿Publica? | Tags |
|---|---|---|
| Pull request | No: construye y escanea para validar el Dockerfile y la imagen | — |
| Push a `main` | Sí | `sha-<7 caracteres del commit>` y `latest` |
| Tag de git `vX.Y.Z` | Sí | `sha-<corto>`, `X.Y.Z` y `X.Y` (no `latest`: es solo de `main`) |

El push es uno solo: todos los tags apuntan al mismo digest. La imagen lleva los labels OCI `org.opencontainers.image.source` (vincula el paquete con este repo), `org.opencontainers.image.revision` (el commit) y `org.opencontainers.image.created`.

**Levantar una versión exacta sin compilar.** Con `-f docker-compose.yml`, Compose no lee el override: no hay `build`, solo la imagen de GHCR.

```bash
EXTRACT_IMAGE_TAG=sha-xxxxxxx docker compose -f docker-compose.yml up -d --wait
# Qué commit está corriendo
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
  ghcr.io/juandelosrios124/extract-service:sha-xxxxxxx
# El digest, que identifica la imagen aunque el tag se mueva
docker image inspect --format '{{index .RepoDigests 0}}' \
  ghcr.io/juandelosrios124/extract-service:sha-xxxxxxx
```

`EXTRACT_IMAGE_TAG` también puede ir en el `.env`. Vacío o `latest` es lo último de `main`; para una release o una medición reproducible, un `sha-xxxxxxx` o un `vX.Y.Z`, que no cambian. Compose baja la imagen solo si no la tiene (no hay `pull_policy`): con un tag inmutable alcanza, pero `latest` hay que refrescarlo a mano con `docker compose -f docker-compose.yml pull extract`.

**El desarrollo sigue compilando.** `docker-compose.override.yml` está versionado y Compose lo lee solo cuando no se pasa `-f`: lo usan `docker compose up --build` y `make up` de infra. Agrega `build: .` y etiqueta el resultado como `extract-service:dev`, un nombre local distinto del de GHCR a propósito: si el build local se llamara `ghcr.io/juandelosrios124/extract-service:latest`, taparía la imagen publicada y un `-f docker-compose.yml up` posterior correría lo compilado en la máquina sin avisar. Sin `-f`, Compose **siempre** lee el override: el modo release es solo con `-f docker-compose.yml`.

**Visibilidad del paquete.** GitHub crea el paquete privado en la primera publicación ([Working with the Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#pushing-container-images)), aunque el repo sea público. Hacerlo público es un paso manual, una sola vez, del dueño: en github.com/juandelosrios124 → *Packages* → `extract-service` → *Package settings* → *Danger Zone* → *Change visibility* → *Public* ([Configuring a package's access control and visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)). Mientras siga privado, para bajarlo hace falta `docker login ghcr.io` con un usuario que tenga acceso al paquete y un token con `read:packages`.

### Escaneo de vulnerabilidades de la imagen (Grype)

Antes de publicar, la CI escanea la imagen **recién construida** con [Grype](https://github.com/anchore/grype) (Anchore), fijado en `v0.121.2`. No se publica una imagen que no pasó el escaneo: el job construye con `load: true`, escanea ese tag local (`extract-service:ci`, que no se publica), y solo después hace el push, que sale de la caché de buildx y no recompila. En los pull requests también se escanea, aunque no se publique nada.

**Complementa a `pip-audit`, no lo reemplaza.** `pip-audit` (job `check`) audita las dependencias Python del lock, antes de construir. Grype escanea la imagen entera: además de esas dependencias, los paquetes Debian que trae `python:3.11-slim` (openssl, glibc, zlib, etc.), que hasta la #57 no miraba nadie.

**Umbral: `only-fixed: true` y `severity-cutoff: high`.** El job falla solo con vulnerabilidades *high* o *critical* **que tienen fix disponible**. Por qué ese umbral:

- `only-fixed`: una vulnerabilidad de Debian sin fix publicado no se arregla actualizando. Con la imagen base fijada por digest no hay acción posible, así que rompería la CI de cualquier commit sin que nadie pueda hacer nada al respecto.
- `high`: por debajo de *high*, el ruido de una imagen Debian es tanto que el umbral dejaría de significar algo y la salida se empezaría a ignorar.

El paso `Resumen` del job deja la cuenta de hallazgos por severidad en una tabla, también de los que no rompen el build.

**Correrlo localmente**, sobre la imagen de desarrollo (`docker compose up --build` la deja etiquetada como `extract-service:dev`):

```bash
docker save -o /tmp/ext.tar extract-service:dev
docker run --rm \
  -v grype-db:/cache -e GRYPE_DB_CACHE_DIR=/cache \
  -v /tmp/ext.tar:/image.tar:ro \
  -v "$PWD":/scan:ro -w /scan \
  anchore/grype:v0.121.2 --only-fixed --fail-on high docker-archive:/image.tar
```

**Sin `docker.sock`.** Montarle el socket del daemon a un contenedor es darle control total de Docker en la máquina, y la política de infra (sección "Escaneo de imágenes") lo prohíbe también para el escáner. En su lugar, la imagen se exporta con `docker save` y Grype lee el tar en solo lectura (`docker-archive:`). `documents-service` aplica el mismo criterio.

En PowerShell, `${PWD}` en lugar de `$PWD` (y otra ruta para el tar). El repo se monta en `/scan`, en solo lectura, y se trabaja ahí para que Grype levante el `.grype.yaml`: lo autodetecta en el directorio de trabajo. La primera corrida baja la base de vulnerabilidades (unos cientos de MB) al volumen `grype-db`, que queda para las siguientes.

**Qué se arregló en lugar de ignorar.** El primer escaneo encontró 4 high con fix, los 4 de la imagen base (y la base al día de hoy trae los mismos, así que mover el digest no servía de nada). El Dockerfile los resuelve en la etapa final: actualiza `zlib1g` al parche de Debian (`deb13u1`, dos CVE en la API `gz_*` de archivos gzip), con la versión fijada para que el build sea reproducible (si Debian la retira, el build falla y hay que actualizarla; el comentario del Dockerfile dice cómo), y borra `pip` y `setuptools` de `/usr/local`, que el runtime no usa porque el servicio corre del venv — eso se lleva además las copias vendorizadas en `setuptools/_vendor` (`wheel` y `jaraco.context`). Los hallazgos con fix bajaron de 22 a 9, todos medium o menos. Por eso hoy no hay `.grype.yaml`.

**Agregar una excepción.** Las excepciones van en `.grype.yaml`, versionado, bajo `ignore:`. Cada una lleva el motivo y la fecha en la que hay que revisarla; sin eso no se ignora nada:

```yaml
ignore:
  # Motivo: por que no aplica, o por que no se puede arreglar todavia.
  # Revisar: 2026-12-01.
  - vulnerability: CVE-0000-00000
```

Una excepción es temporal por definición: si en la fecha de revisión sigue ahí, se renueva con un motivo nuevo o se arregla. Mientras no haya ninguna, el archivo no existe.

## Red compartida

Desde la #33 cada réplica vive en **una sola** red:

- `mired`, la red Docker **externa** que comparten los tres repos del trabajo práctico ([pdf_extractext-infra](https://github.com/juandelosrios124/pdf_extractext-infra), este y [documents-service](https://github.com/OlivettiSantino/documents-service)). Gracias a ella Consul puede chequear la IP de cada réplica y `documents-service` puede llamarlas directo, sin pasar por el puerto publicado en el host.

Al ser externa, hay que crearla **una sola vez por máquina y antes** del primer `docker compose up`; es el único paso previo que agrega:

```bash
docker network create mired
```

`docker compose down` no la borra, justamente porque la comparten los otros stacks. El nombre se configura con `SHARED_NETWORK` (default `mired`), variable común a los tres repos.

Desde cualquier contenedor de `mired`, las réplicas se alcanzan en `http://extract:8001`: el DNS de Docker devuelve una IP por réplica. También responden al alias `extract-service`, pero el nombre canónico es `extract`.

Antes había además una red privada del proyecto, por donde entraba Caddy. Con Caddy eliminado (#33) no quedó nadie que la use: el tráfico entra por Traefik, que también está en `mired`, así que las réplicas se declaran solo ahí.

### Service discovery con Consul

Cada réplica se registra sola en [Consul](https://github.com/juandelosrios124/pdf_extractext-infra) al arrancar y se desregistra al apagarse. **Viene encendido** en el compose desde la #32, porque es así como Traefik descubre las réplicas. Igual nunca es fatal: si Consul no está o el registro falla, la réplica arranca de todas formas y solo queda un `WARNING` en el log (el default del código, fuera de Compose, sigue siendo `false`).

Ya registrada, cada 30 s verifica que el agente la siga teniendo (`GET /v1/agent/service/<id>`): si Consul la borró por `DeregisterCriticalServiceAfter` (por ejemplo, tras quedar colgada más de un minuto), se vuelve a registrar sola, con el mismo backoff, y lo avisa con el `WARNING` `Consul ya no tiene la réplica, se vuelve a registrar`; si Consul no responde, no toca nada y vuelve a mirar en el próximo ciclo.

El Consul de infra corre con ACLs en `default_policy = "deny"`: sin un token válido, Consul rechaza el registro y Traefik nunca rutea las réplicas, aunque Docker las muestre `healthy`. El token va en el `.env` de este repo, con el valor de `CONSUL_EXTRACT_TOKEN` del `.env` de infra (no el de management):

```dotenv
# .env de extract-service
CONSUL_HTTP_TOKEN=<valor de CONSUL_EXTRACT_TOKEN del .env de infra>
```

Después:

```bash
docker compose up -d --wait        # Compose recrea las réplicas si cambió el token
```

No hay que exportarlo en el shell ni anteponerlo al comando: Compose les da prioridad a las variables del shell sobre el `.env`, así que un valor exportado (por ejemplo, el de management) pisaría el correcto. Si las réplicas no se registran, el log lo dice:

```bash
docker compose logs extract | grep "No se pudo registrar"
```

Qué token va en cada repo: [Qué va en el `.env` de cada repo](https://github.com/juandelosrios124/pdf_extractext-infra#qué-va-en-el-env-de-cada-repo).

Lo que registra cada réplica:

| Campo | Valor | Por qué |
|---|---|---|
| `ID` | hostname del contenedor | Único por réplica, así las 5 conviven en el catálogo |
| `Name` | `extract` | El nombre con el que la busca `documents-service` |
| `Address` | su IP en `mired` | Se resuelve preguntando por qué interfaz saldría un paquete hacia Consul, en vez de confiar en el orden de `gethostbyname` |
| `Port` | `8001` | |
| `Check` | `HTTP` a `http://<IP>:8001/health`, `Interval 10s`, `Timeout 2s` | A la IP de la réplica y no a `localhost`, que desde Consul es el propio contenedor de Consul |
| `DeregisterCriticalServiceAfter` | `1m` | Un `docker kill` no da tiempo a desregistrarse: Consul la saca del catálogo por su cuenta |

Comprobar:

```bash
# Que servicios ve Consul: deberia listar "extract"
docker exec pdf_extractext-infra-consul-1 consul catalog services

# Una se cae: pasa a critical y, un minuto despues, desaparece del catalogo
docker kill extract-service-extract-1
```

La API de Consul necesita token: sin él, las lecturas devuelven `200` con `[]`, no `403`. Alcanza el de Traefik, que es de solo lectura, tomado del `.env` de infra sin imprimirlo (ver [UI y API con token](https://github.com/juandelosrios124/pdf_extractext-infra#ui-y-api-con-token)). Desde este repo, en bash:

```bash
# Leer solo esa variable, sin exportarla: un "set -a; . .env" de infra
# exportaria tambien el CONSUL_HTTP_TOKEN de management y pisaria este .env
token=$(sed -n 's/^CONSUL_TRAEFIK_TOKEN=//p' ../pdf_extractext-infra/.env)

# Las 5 replicas con su ID, su IP de mired y el estado del check (sin jq)
curl -s -H "X-Consul-Token: $token" http://127.0.0.1:8500/v1/health/service/extract | python -c "import sys,json;[print(s['Service']['ID'], s['Service']['Address'], [c['Status'] for c in s['Checks'] if c['CheckID'].startswith('service:')][0]) for s in json.load(sys.stdin)]"

# Solo las que estan passing
curl -s -H "X-Consul-Token: $token" "http://127.0.0.1:8500/v1/health/service/extract?passing"
```

En fish:

```fish
set -l token (string match -rg '^CONSUL_TRAEFIK_TOKEN=(.*)' < ../pdf_extractext-infra/.env)

curl -s -H "X-Consul-Token: $token" http://127.0.0.1:8500/v1/health/service/extract | python -c "import sys,json;[print(s['Service']['ID'], s['Service']['Address'], [c['Status'] for c in s['Checks'] if c['CheckID'].startswith('service:')][0]) for s in json.load(sys.stdin)]"

curl -s -H "X-Consul-Token: $token" "http://127.0.0.1:8500/v1/health/service/extract?passing"
```

La UI está en <http://127.0.0.1:8500/ui>. El nombre del contenedor de Consul sale del proyecto Compose de infra; si lo levantaste con otro nombre, ajustá el `docker exec`.

El token de ACL se toma de `CONSUL_HTTP_TOKEN` y viaja en el header `X-Consul-Token`; no está en el repo ni se escribe en los logs.

### Liveness y readiness

Son dos preguntas distintas, con consumidores distintos:

| | `/health` | `/health/ready` |
|---|---|---|
| Pregunta | ¿el proceso está vivo? | ¿además puede atender? |
| Mira el hilo de extracción | no | sí |
| Lo consume | `HEALTHCHECK` de Docker | check de Consul → Traefik |
| Qué pasa si falla | Docker **reinicia** el contenedor | Traefik la **saca del balanceo**, sin reiniciarla |

`/health` no mira el pool a propósito: si lo mirara, bajo carga las 5 réplicas se marcarían mal a la vez y el servicio caería completo (es la regresión que cubre `test_health.py`).

La readiness existe por una limitación concreta del `EXTRACTION_DEADLINE`: se chequea **entre páginas**, así que una sola página que cuelgue no se puede interrumpir, y con `THREAD_POOL_SIZE=1` eso deja a la réplica sin poder atender mientras `/health` sigue diciendo `ok`. `READINESS_STUCK_AFTER` (12 s) es mayor que el deadline (8 s) justamente para que solo dispare en ese caso.

```bash
curl -s 127.0.0.1:8080/health/ready
# {"status":"ready","extracting_for_seconds":0.0}
```

**Réplica trabada más de un minuto.** El check de Consul lleva `DeregisterCriticalServiceAfter: 1m`, así que una réplica que quede trabada más de un minuto no solo sale del balanceo: Consul la **borra del catálogo**. Cuando se destraba, la verificación periódica (cada 30 s) lo detecta y la vuelve a registrar, sin reiniciar el contenedor (#61; ver [Service discovery con Consul](#service-discovery-con-consul)).

### Publicación en Traefik

El circuit breaker, el retry y el límite de concurrencia **no están en el código del servicio**: los aplica Traefik, y cada réplica los configura con los **tags que pone en su propio registro de Consul** (`traefik_tags()` en [`extractor/services/consul.py`](extractor/services/consul.py)). No se usan labels de Docker: así la configuración viaja con el registro y no con el contenedor.

Traefik corre con `providers.consulcatalog` y `exposedByDefault=false`, así que sin `traefik.enable=true` la réplica queda sin publicar. Los tags que manda:

| Tag | Variable | Valor por defecto |
|---|---|---|
| `traefik.enable=true` | — | — |
| `...routers.extract.rule` | — | `PathPrefix(`/extract`)` |
| `...routers.extract.entrypoints` | — | `web` (el otro entrypoint es el del dashboard) |
| `...routers.extract.middlewares` | — | `extract-cb,extract-retry,extract-inflight` |
| `...extract-cb.circuitbreaker.expression` | `TRAEFIK_CB_EXPRESSION` | ver abajo |
| `...extract-cb.circuitbreaker.fallbackDuration` | `TRAEFIK_CB_FALLBACK` | `10s` |
| `...extract-cb.circuitbreaker.recoveryDuration` | `TRAEFIK_CB_RECOVERY` | `10s` |
| `...extract-retry.retry.attempts` | `TRAEFIK_RETRY_ATTEMPTS` | `2` |
| `...extract-inflight.inflightreq.amount` | `TRAEFIK_INFLIGHT` | `80` |
| `...extract-inflight.inflightreq.sourcecriterion.requestheadername` | `TRAEFIK_INFLIGHT_SOURCE_HEADER` | `X-Forwarded-Server` (vacío: no se manda el tag) |
| `...services.extract.loadbalancer.serverstransport` | — | `extract-transport@file` |

`serversTransport` es lo único que no se puede declarar por tags; lo define el provider file de infra.

**Decisión: el `503` del backpressure no abre el circuito.**

```
NetworkErrorRatio() > 0.30 || ResponseCodeRatio(500, 503, 0, 600) > 0.50
```

En Traefik el rango de `ResponseCodeRatio` es `[from, to)`: `from` incluido, `to` excluido. `(500, 503, ...)` cuenta entonces **500, 501 y 502, y deja afuera el 503**. El default que suele citarse, `(500, 600, ...)`, sí lo contaría.

Se eligió excluirlo porque **el `503` del backpressure no es una falla de la réplica: es su respuesta sana cuando está llena** (sin cupo, o pedido vencido en la cola), y siempre lleva `Retry-After: 1`. Si contara para el breaker:

- Cuanto mejor descarta carga el servicio, más probable que Traefik lo corte **del todo**. La degradación elegante se convertiría en una caída completa.
- Las 5 réplicas descartan a la vez bajo la misma carga, así que tampoco habría aislamiento: el circuito se abriría para el servicio entero.
- Con el circuito abierto Traefik responde `503` por su cuenta, el mismo código: el cliente no podría distinguir «estoy lleno, reintentá en 1 s» de «no te atiendo».

El breaker queda entonces para fallas de verdad: errores de red (réplica caída o inalcanzable) y `500`/`502`.

**Qué reintenta el `retry`** (comprobado contra el código de Traefik v3.7.17 y con el stack, issue #55). Con `attempts=2` y sin `status`:

- **Reintenta** cuando el pedido falla **antes de que Traefik le mande un byte a la réplica**: conexión rechazada, o el `dialTimeout` de 2 s contra una réplica caída que Consul todavía no sacó. Vale también para `POST /extract`: ese camino no mira el método. Como no se mandó nada, el PDF se reenvía sin guardarlo en memoria, así que pasa con cualquier tamaño, también los de más de 2 MiB. Con una réplica muerta bajo carga, esos pedidos tardan ~2,1 s (el dial más el segundo intento) y terminan en `200`.
- **No reintenta** cuando el pedido ya salió hacia la réplica: un `502` porque la réplica murió en medio del pedido, un `504` por `responseHeaderTimeout` (25 s) o cualquier respuesta, incluido el `503` del backpressure. Así no pelea con la admisión por cupos y no rompe el [presupuesto de timeouts](https://github.com/juandelosrios124/pdf_extractext-infra#presupuesto-de-timeouts): un upstream que no responde da **un solo** `504` a los ~25 s. El único caso que suma tiempo es un dial fallido seguido de una réplica que no responde: 2 s + 25 s, el `504` sale a los ~27 s, el mismo valor que `EXTRACT_TIMEOUT` de documents, que puede cortar antes. Hace falta una réplica caída y otra colgada a la vez; con una réplica que responde dentro de sus 23 s, el total queda en ≤ 25 s.
- No se declaran `status`, `retryNonIdempotentMethod` ni `maxRequestBodyBytes`. `retryNonIdempotentMethod` solo habilita el retry **por status** para `POST`; con `status`, el retry guardaría en memoria cada body de hasta `maxRequestBodyBytes` (2 MiB por defecto; uno más grande pasa igual, pero no se reintenta) y reintentaría respuestas. `/extract` es una función pura y reintentarlo sería seguro, pero el retry por status no se necesita para el failover, y sumaría memoria en Traefik y un riesgo para el presupuesto de timeouts.
- En Traefik v3.7.17 un `retry` declarado como middleware **no informa sus reintentos**: `RetryAttempts` del access log queda en 0 y `traefik_service_retries_total` no aparece aunque reintente (`retry.Listeners{}` con un `TODO missing metrics / accessLog` en `pkg/server/middleware/middlewares.go`). Un reintento se reconoce por la duración: ~2,1 s y `200`, contra ~80 ms de un pedido normal.

**Pedidos en vuelo.** El `inflightreq` responde `429` al pasarse del tope y está puesto en el doble de la capacidad agregada (5 réplicas × 8 cupos) a propósito: el control primario sigue siendo la admisión por cupos de la app, que es la que está medida en el informe; el tope del borde solo acota el encolado. Es un primer valor razonado, sin afinar. Sin `sourceCriterion`, Traefik cuenta los pedidos **por valor del header `Host`**: documents (`Host: traefik`) y un cliente que entra por `127.0.0.1:8080` tendrían 80 cada uno, y cualquiera saltearía el tope cambiando el `Host`. Por eso se agrupa por `X-Forwarded-Server`: Traefik borra el que manda un cliente no confiable y lo reescribe con su propio hostname, así que el tope es **uno solo para todos** y no se puede falsificar (comprobado con dos `Host` distintos y con un `X-Forwarded-Server` falso, issue #55).

## Arquitectura

```mermaid
flowchart LR
  C[Cliente / k6 / Vegeta] --> T[Traefik]
  D[documents-service] --> T
  T --> R1[extract 1]
  T --> R2[extract 2]
  T --> R5[extract 5]
  R1 -. se registra .-> K[(Consul)]
  K -. catalogo .-> T
```

Traefik vive en el repo de infra y descubre las réplicas por el catálogo de Consul: cada una se registra al arrancar con los tags que configuran su router, su circuit breaker, su retry y su límite de concurrencia (ver [Publicación en Traefik](#publicación-en-traefik)).

Cada réplica admite como máximo `MAX_CONCURRENT_UPLOADS` pedidos, lee el cuerpo en memoria (sin escribir a disco) y extrae el texto con PyMuPDF en un pool de hilos separado del event loop. Decisiones principales:

- **PyMuPDF** y no `pypdfium2`: esta fue solo un 11,4 % más rápida, por debajo del 15 % que el equipo fijó para justificar el riesgo de cambiar.
- **Un hilo por réplica**, porque cada contenedor tiene 1,0 CPU.
- **Backpressure** con `503` y `Retry-After`, para fallar rápido en lugar de acumular pedidos.

Capas: `extractor/api` (HTTP), `extractor/services` (extracción, sin conocer HTTP) y `extractor/main.py` (composición).

## Tests

```bash
uv run pytest -q
```

Se escribieron con TDD (rojo, verde, refactor).

## Pruebas de carga

Los 4 PDFs oficiales están en `tests/stress/pdfs/` y los scripts de la cátedra, adaptados para leer la URL y la carpeta de PDFs del entorno, en `tests/stress/profesor/` (los originales, en `original/`). Con el stack levantado:

```bash
# Vegeta: 50 req/s durante 30 s, timeout de 30 s (por mired, la red de Traefik)
docker run --rm --network mired -v "$PWD/tests/stress:/stress" --entrypoint sh peterevans/vegeta:6.9.1-vegeta12.8.4 -c "vegeta attack -targets=/stress/profesor/test_carga.txt -rate=50 -duration=30s -timeout=30s | vegeta report"

# k6: spike de 100 usuarios virtuales
k6 run tests/stress/profesor/spike_tests.js
```

En PowerShell, reemplazar `$PWD` por `${PWD}`. Recomendaciones para medir:
- Dejar la máquina enchufada y cerrar otros programas: **k6 carga los 4 PDFs en cada uno de los 100 usuarios (unos 1,4 GB)** y es sensible a la memoria disponible.
- Recrear las réplicas y esperar ~60 s antes de cada corrida.
- Una sola corrida varía unos 13 puntos de éxito: comparar promedios de varias corridas alternadas.

Los scripts `tests/stress/k6/spike.js` y `tests/stress/vegeta/attack.sh` envían los PDFs como `multipart/form-data`. Los resultados se guardan en `tests/stress/results/` (no se versiona, salvo los resúmenes de la #38). `tests/stress/generate_pdfs.py` genera un set sintético para diagnóstico.

**Matriz del informe (issue #38).** La comparación A, C y C+CB de §5.3 se corre con un solo script, que usa la imagen publicada (`sha-ec5b762`), los dos scripts de la cátedra en contenedores con versión fija dentro de `mired` y 3 rondas alternadas, y después arma la tabla con media, mínimo y máximo:

```bash
tests/stress/bench/run-38.sh                                                    # 18 corridas, ~45 min
uv run python tests/stress/bench/resumen_38.py tests/stress/results/38/$(date +%Y-%m-%d)
```

Necesita infra y documents levantados (`make up RELEASE=1 ...` en infra). Las variables, el desglose de errores por motivo y la corrida de `POST /documents` están en [`tests/stress/README.md`](tests/stress/README.md#matriz-de-la-issue-38-a-c-y-ccb).

## Estructura

```
extractor/            código del servicio (api, services, config, main)
extractor/services/consul.py  autorregistro de la réplica en Consul
tests/                tests unitarios
tests/stress/         PDFs oficiales, scripts de k6 y Vegeta, resultados
informe.md            informe técnico
docs/                 bitácora de mediciones
docker-compose.yml    5 réplicas con límites de recursos, en la red mired, con la imagen de GHCR
docker-compose.override.yml  build local para desarrollo (extract-service:dev)
```

## Metodología

Twelve-Factor (configuración por entorno, logs a stdout, procesos sin estado), TDD, KISS, DRY, YAGNI y SOLID; una rama por issue, commits chicos y pull requests.
