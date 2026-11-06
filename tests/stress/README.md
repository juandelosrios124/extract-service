# Pruebas de carga

Dos pruebas contra `POST /extract` de `extract-service`, a través de **Traefik**
(`http://127.0.0.1:8080`), que es el mismo camino que usa `documents-service`. Las dos
usan los mismos 4 PDFs de [`pdfs/`](pdfs/README.md) y los envían como
`multipart/form-data` (campo `file`).

| Prueba | Herramienta | Modelo | Perfil | Reporta |
|---|---|---|---|---|
| Spike | k6 (`k6/spike.js`) | Cerrado: cada VU espera la respuesta antes de enviar otra | 0 → 100 VUs en 10s, 20s sostenidos, 100 → 0 en 10s | Throughput, tasa de error, latencia mediana, P90, P95 y máxima |
| Carga fija | Vegeta (`vegeta/attack.sh`) | Abierto: ritmo fijo, sin esperar respuestas | 50 req/s durante 30s (1500 solicitudes), timeout de cliente 30s | Éxito, código 0 (timeouts) y latencias P50/P95 |

## Antes de correr

1. Poner los 4 PDFs en `tests/stress/pdfs/` (ver [pdfs/README.md](pdfs/README.md)).
2. Levantar **Consul y Traefik**, desde el repo de
   [infra](https://github.com/juandelosrios124/pdf_extractext-infra). Desde la #33 este
   repo no publica ningún puerto: sin Traefik no hay por dónde entrar.

   ```bash
   docker compose up -d --wait        # en pdf_extractext-infra
   ```

3. Levantar las réplicas, desde la raíz de este repo:

   ```bash
   docker compose up --build -d --wait
   ```

Antes de medir, comprobar que Traefik ya ve las 5 réplicas: en
<http://127.0.0.1:8081/api/http/services> tiene que aparecer `extract@consulcatalog`
con 5 servidores.

Los comandos de abajo se corren desde la raíz del repo. Los resultados quedan en
`tests/stress/results/`, que no se commitea.

## Spike con k6

Con Docker (no hace falta instalar k6):

```bash
docker run --rm --network mired \
  -v "$PWD/tests/stress:/stress" \
  -e BASE_URL=http://traefik \
  -e SUMMARY_JSON=/stress/results/k6-spike.json \
  grafana/k6 run /stress/k6/spike.js
```

Con k6 instalado:

```bash
SUMMARY_JSON=tests/stress/results/k6-spike.json k6 run tests/stress/k6/spike.js
```

Al terminar imprime:

```
== Spike (k6, modelo cerrado) ==
Solicitudes:     ...
Throughput:      ... req/s
Tasa de error:   ... %
Latencias:
  mediana        ... ms
  P90            ... ms
  P95            ... ms
  máxima         ... ms
```

La tasa de error cuenta toda respuesta que no sea 2xx/3xx, incluidos los timeouts.

## Carga fija con Vegeta

Con Docker (la imagen trae `vegeta`, `sh` y `jq`):

```bash
docker run --rm --network mired \
  -v "$PWD/tests/stress:/stress" \
  --entrypoint sh peterevans/vegeta /stress/vegeta/attack.sh
```

No hace falta pasar `BASE_URL`: el default del script es `http://traefik`, que es como se
llama Traefik dentro de `mired`. Con `vegeta` y `jq` instalados en el host hay que
apuntarlo al puerto publicado:

```bash
BASE_URL=http://127.0.0.1:8080 sh tests/stress/vegeta/attack.sh
```

Imprime el reporte completo de Vegeta (se guarda en `results/vegeta-report.txt`) y después
un resumen (en `results/vegeta-summary.txt`):

```
== Carga fija (Vegeta, modelo abierto) ==
Solicitudes:          1500
Éxito:                ... %
Código 0 (sin resp.): ...
  de esos, timeouts:  ...
Latencia P50:         ... ms
Latencia P95:         ... ms
```

Vegeta registra con código `0` las solicitudes que no recibieron respuesta. El resumen separa
las que terminaron por el timeout de cliente de las que fallaron por errores de conexión.
Los errores concretos están en el `Error Set` del reporte completo.

## Memoria con cuerpos grandes

Comprueba que una réplica sostiene sus 8 cupos ocupados con pedidos cercanos a
`MAX_UPLOAD_SIZE` sin pasarse del tope de 1 GiB (issue #36). Hay que levantar el
stack con **una sola réplica**, porque Traefik reparte entre todas y el objetivo
es saturar una:

```bash
EXTRACT_REPLICAS=1 docker compose up --build -d --wait
uv run python tests/stress/memoria_multipart.py --mb 49
```

En PowerShell: `$env:EXTRACT_REPLICAS="1"; docker compose up --build -d --wait`.

El script genera una sola vez un PDF grande en `pdfs_provisorios/` (ignorado por
git), hace un pedido de calentamiento, dispara los 8 en paralelo y muestrea
`docker stats` para quedarse con el pico del contenedor. Falla si algún pedido no
termina en `200`.

El calentamiento no es cosmético: sobre un contenedor recién arrancado el primer
pedido tarda bastante más y arrastra a los demás por encima de `MAX_QUEUE_WAIT`,
lo que produce `503` que no tienen nada que ver con la memoria.

## Variables de entorno

Sirven para los dos scripts, salvo donde se indica.

| Variable       | Default                  | Descripción |
|----------------|--------------------------|-------------|
| `BASE_URL`     | `http://127.0.0.1:8080`  | Traefik. En `vegeta/attack.sh` el default es `http://traefik`, porque corre dentro de `mired` |
| `PDF_DIR`      | `tests/stress/pdfs`      | Carpeta con los PDFs. En k6, una ruta relativa se toma desde `k6/` |
| `PDF_FILES`    | los 4 PDFs oficiales     | PDFs a rotar, separados por coma |
| `TIMEOUT`      | `30s`                    | Timeout de cliente por solicitud |
| `RATE`         | `50`                     | Solo Vegeta: solicitudes por segundo |
| `DURATION`     | `30s`                    | Solo Vegeta: duración del ataque |
| `OUT_DIR`      | `tests/stress/results`   | Solo Vegeta: dónde guardar targets, resultados y reportes |
| `SUMMARY_JSON` | _(sin definir)_          | Solo k6: si se define, guarda ahí el resumen completo en JSON |

El perfil del spike está fijo en `k6/spike.js` porque es el de la consigna.

## Al comparar corridas

- Los timeouts de Traefik también influyen en los resultados: viven en el repo de
  [infra](https://github.com/juandelosrios124/pdf_extractext-infra) y están explicados en
  su tabla «Presupuesto de timeouts». Con los valores por defecto, Traefik puede devolver
  `504` a los 30 s si no termina de recibir un PDF (`readTimeout` del entrypoint `web`) o
  a los 25 s si la réplica no manda los headers de respuesta (`responseHeaderTimeout`).
- Si el generador de carga corre en la misma máquina que el servicio, compiten por CPU.
  Anotarlo junto con los resultados.
