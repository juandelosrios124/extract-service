# Pruebas de carga

Dos pruebas contra `POST /extract` de `extract-service`, a través del reverse proxy
(`http://localhost:8001`). Las dos usan los mismos 4 PDFs de [`pdfs/`](pdfs/README.md) y
los envían como `multipart/form-data` (campo `file`).

| Prueba | Herramienta | Modelo | Perfil | Reporta |
|---|---|---|---|---|
| Spike | k6 (`k6/spike.js`) | Cerrado: cada VU espera la respuesta antes de enviar otra | 0 → 100 VUs en 10s, 20s sostenidos, 100 → 0 en 10s | Throughput, tasa de error, latencia mediana, P90, P95 y máxima |
| Carga fija | Vegeta (`vegeta/attack.sh`) | Abierto: ritmo fijo, sin esperar respuestas | 50 req/s durante 30s (1500 solicitudes), timeout de cliente 30s | Éxito, código 0 (timeouts) y latencias P50/P95 |

## Antes de correr

1. Poner los 4 PDFs en `tests/stress/pdfs/` (ver [pdfs/README.md](pdfs/README.md)).
2. Levantar el servicio con las réplicas y el proxy, desde la raíz del repo:

   ```bash
   docker compose up --build -d extract proxy
   ```

Los comandos de abajo se corren desde la raíz del repo. Los resultados quedan en
`tests/stress/results/`, que no se commitea.

## Spike con k6

Con Docker (no hace falta instalar k6):

```bash
docker run --rm \
  -v "$PWD/tests/stress:/stress" \
  -e BASE_URL=http://host.docker.internal:8001 \
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
docker run --rm \
  -v "$PWD/tests/stress:/stress" \
  -e BASE_URL=http://host.docker.internal:8001 \
  --entrypoint sh peterevans/vegeta /stress/vegeta/attack.sh
```

Con `vegeta` y `jq` instalados:

```bash
sh tests/stress/vegeta/attack.sh
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

## Variables de entorno

Sirven para los dos scripts, salvo donde se indica.

| Variable       | Default                  | Descripción |
|----------------|--------------------------|-------------|
| `BASE_URL`     | `http://localhost:8001`  | URL del proxy. Desde un contenedor: `http://host.docker.internal:8001` |
| `PDF_DIR`      | `tests/stress/pdfs`      | Carpeta con los PDFs. En k6, una ruta relativa se toma desde `k6/` |
| `PDF_FILES`    | `01.pdf,02.pdf,03.pdf,04.pdf` | PDFs a rotar, separados por coma |
| `TIMEOUT`      | `30s`                    | Timeout de cliente por solicitud |
| `RATE`         | `50`                     | Solo Vegeta: solicitudes por segundo |
| `DURATION`     | `30s`                    | Solo Vegeta: duración del ataque |
| `OUT_DIR`      | `tests/stress/results`   | Solo Vegeta: dónde guardar targets, resultados y reportes |
| `SUMMARY_JSON` | _(sin definir)_          | Solo k6: si se define, guarda ahí el resumen completo en JSON |

El perfil del spike está fijo en `k6/spike.js` porque es el de la consigna.

## Al comparar corridas

- Los timeouts del proxy también influyen en los resultados: ver
  [proxy/README.md](../../proxy/README.md). Con los valores por defecto, el proxy puede
  devolver `504` a los 30s si no termina de recibir un PDF (`PROXY_READ_BODY_TIMEOUT`).
- Si el generador de carga corre en la misma máquina que el servicio, compiten por CPU.
  Anotarlo junto con los resultados.
