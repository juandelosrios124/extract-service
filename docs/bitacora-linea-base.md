# Bitácora de la línea base (issue 1)

Rama `docs/1-linea-base`. Commit del código medido: `b1595c9`.
Esta bitácora junta lo que se midió y lo que se concluyó, para armar después el informe.
Todo lo marcado como **hipótesis** todavía no está confirmado.

> **Histórico.** Las secciones que siguen, hasta «Re-medición detrás de Traefik (issue #38)»,
> son de `v1.0` y anteriores: Caddy, Windows (Docker Desktop y WSL2), una corrida por prueba.
> Se conservan tal cual. Las mediciones vigentes están en la sección de la #38, al final.

## Entorno

- Equipo: notebook con AMD Ryzen 3 4300U (4 núcleos / 4 hilos), 6,9 GB de RAM (0,8 a 0,9 GB libres con todo cerrado).
- Docker 25.0.3 (Docker Desktop): ve 4 CPUs y 3,27 GiB de RAM.
- Herramientas: k6 v2.2.0, Vegeta v12.13.0, jq 1.8.2.
- Servicio: 5 réplicas de `extract` (límite de 1,0 CPU y 1 GiB cada una, `THREAD_POOL_SIZE=4`) detrás de Caddy en el puerto 8001.
- El generador de carga corre en la misma máquina que el servicio, así que compiten por CPU.

## Set de PDFs provisorio

Generado con `tests/stress/generate_pdfs.py`. **No son los oficiales de la cátedra.**

| Archivo | Páginas | Tamaño | Contenido |
|---|---|---|---|
| `01.pdf` | 2 | 0,03 MB | Solo texto |
| `02.pdf` | 40 | 0,61 MB | Solo texto |
| `03.pdf` | 292 | 4,50 MB | Solo texto |
| `04.pdf` | 3 | 8,68 MB | Gráficos vectoriales e imágenes de ruido |

## Tiempos individuales (un pedido a la vez)

| PDF | Con cargador, servicio sin carga (3 pedidos) | Ahorro de energía, sin cargador (1 pedido) |
|---|---|---|
| `01.pdf` | 0,05 a 0,16 s | 1,47 s |
| `02.pdf` | 0,23 a 0,29 s | 1,47 s |
| `03.pdf` | 1,6 a 2,4 s | 5,28 s |
| `04.pdf` | 0,77 a 1,3 s | 1,93 s |

Diagnóstico con `01.pdf`, 10 pedidos con cargador: el primer pedido a cada réplica tarda 0,08 a 0,19 s y los siguientes unos 0,02 s. `localhost` y `127.0.0.1` dan lo mismo.

## k6, spike (hasta 100 VUs, 40 s, timeout de 30 s)

| Corrida | Condición | Solicitudes | req/s (todas) | Error | Exitosas (aprox.) | Mediana | P90 | Máxima |
|---|---|---|---|---|---|---|---|---|
| 0 | Ahorro, sin cargador | 160 | 2,36 | 72,5 % | 44 | 19,6 s | 30,0 s | 30,2 s |
| 1 | Con cargador | 195 | 3,14 | 63,6 % | 71 | 10,6 s | 30,0 s | 30,1 s |
| Diag. A | Sin `03.pdf` | 222 | 3,73 | 48,7 % | 114 | 7,9 s | 30,0 s | 30,1 s |
| Diag. B | Con monitoreo corriendo | 180 | 2,65 | 62,8 % | 67 | 22,3 s | 30,0 s | 30,4 s |
| 2 | "Limpio" (ver nota) | 199 | 2,98 | 61,8 % | 76 | 30,0 s | 30,0 s | 30,7 s |

Notas:
- El throughput de k6 cuenta también las solicitudes fallidas. El throughput exitoso es la columna "Exitosas" dividida por la duración (unos 60 s).
- Todos los errores fueron `request timeout`. No hubo respuestas 5xx.
- Corrida 2: Docker Desktop estaba apagado al empezar y k6 se lanzó con las réplicas todavía en `health: starting`.
- Ninguna réplica tuvo `OOMKilled` ni reinicios.

## Vegeta, escalera de carga (20 s por tramo, 4 PDFs provisorios en rotación)

| Tasa | Solicitudes | Éxito | P50 | P95 |
|---|---|---|---|---|
| 1 req/s | 20 | 100 % | 454 ms | 1.938 ms |
| 2 req/s | 40 | 100 % | 444 ms | 2.229 ms |
| 4 req/s | 80 | 100 % | 1.216 ms | 6.013 ms |
| 8 req/s | 160 | 100 % | 8.867 ms | 21.374 ms |

## Monitoreo durante el pico de k6 (corrida Diag. B)

- CPU total del equipo: 95 a 100 %.
- Procesos que más consumían: `vmmemWSL` (la VM de Docker) con 122 a 279 %, `Memory Compression` hasta 139 %, VS Code hasta 79 %.
- Cada réplica de `extract` usó entre 25 y 68 % de CPU (tope de 100 %). El proxy, entre 11 y 21 %.
- Memoria por réplica: unos 55 a 75 MiB en reposo y 150 a 290 MiB después de la carga.

## Referencia de la cátedra

| Prueba | Resultado del profesor |
|---|---|
| k6 spike | 1.037 solicitudes, 25,35 req/s, 0 % de error, mediana 1,88 s, P90 7,83 s, P95 8,80 s, máxima 13,94 s |
| Vegeta 50 req/s, 30 s | 66,53 % de éxito, 33,40 % de timeouts, 16,65 req/s efectivos, P50 14,89 s |

## Hallazgos

1. El modo de ahorro de energía sin cargador degradó mucho las mediciones (`01.pdf`: 1,47 s contra unos 0,05 s).
2. La red local y el arranque en frío no explican la lentitud.
3. La memoria no es el cuello de botella: sin `OOMKilled` y muy por debajo del límite de 1 GiB.
4. Con 100 VUs en k6 el servicio completa pocas solicitudes con éxito (unas 1 a 2 por segundo) y el resto expira a los 30 s.
5. Con carga controlada, el servicio sostiene 4 req/s con 100 % de éxito. A 8 req/s la latencia se dispara (P95 de 21 s). La capacidad con este set es de unos 4 req/s.
6. Con la carga de k6, la CPU del equipo se satura, pero las réplicas no llegan a su límite de 1,0 CPU.

## Hipótesis (sin confirmar)

- **H1, trabajo abandonado:** los pedidos que el cliente abandona por timeout se siguen procesando, y esa capacidad se pierde. Prueba: comparar cuántas respuestas 200 registran los logs del servidor contra los éxitos que cuenta el cliente.
- **H2, CPU del equipo:** k6, Docker y las réplicas comparten 4 núcleos y la RAM está muy ajustada.
- **H3, costo de subir y parsear multipart grande:** `04.pdf` tarda unos 0,8 s aunque casi no tiene texto.
- **H4, pool de hilos sin cola limitada:** el análisis de commits indica un `ThreadPoolExecutor` de 4 hilos por réplica y ningún límite de cola.

## Vegeta, perfil de la consigna (50 req/s durante 30 s, timeout 30 s)

| Corrida | Condición | Solicitudes | Éxito | Código 0 (timeouts) | Otros | P50 | Media | Throughput efectivo |
|---|---|---|---|---|---|---|---|---|
| Base 1 | Réplicas healthy y 1 min de reposo, con cargador | 1.500 | 3,73 % (56) | 1.443 (1.442) | 1 x 504 | 30,0 s | 29,5 s | 0,93 req/s |

- El servidor registró 80 respuestas 200 contra 56 éxitos contados por el cliente: unas 24 se completaron cuando el cliente ya había abandonado.
- En total se completaron solo 80 pedidos en unos 150 s (0,5 req/s), contra unos 4 req/s sostenidos en la escalera: bajo sobrecarga el rendimiento útil colapsa.
- Errores del cliente: `Client.Timeout exceeded while awaiting headers`, `while reading body`, `unexpected EOF` y conexiones cerradas por el servidor.
- Sin `OOMKilled` ni reinicios.
- Cátedra: 66,53 % de éxito y P50 de 14,89 s.

## Lectura de `extract.py` (hallazgos para las issues 2, 3 y 4)

- El parseo del multipart (`BytesParser`) corre dentro de la corrutina, es decir en el event loop, y lo bloquea mientras dura.
- Se copia el cuerpo varias veces: `bytearray`, `bytes(body)`, `headers + body`, estructuras del parser y `get_payload(decode=True)`.
- No hay límite de pedidos en curso: se lee el cuerpo completo antes de decidir si se atiende.
- El pool de hilos solo envuelve la extracción de texto.

## Issue 4: parseo de multipart en memoria (rama `perf/4-parseo-multipart`)

Cambio: se reemplaza `email.BytesParser` por un parser con `bytes.find` (`extractor/api/multipart.py`). Con TDD: test en rojo, parser en verde y conexión en `extract.py`. 7 tests nuevos, 34 en total. En un banco de pruebas aparte, el parseo de un PDF de 9 MB pasó de unos 300 ms a unos 6 ms.

| Prueba (4 PDFs provisorios) | Antes | Después |
|---|---|---|
| Vegeta 4 req/s, 20 s: éxito | 100 % | 97,5 % (2 timeouts) |
| Vegeta 4 req/s: P50 / P95 | 1,2 s / 6,0 s | 0,54 s / 2,1 s |
| Vegeta 8 req/s, 20 s: éxito | 100 % | 72,5 % (44 timeouts) |
| Vegeta 8 req/s: P50 / P95 | 8,9 s / 21,4 s | 3,1 s / 30,0 s |
| Vegeta 50 req/s, 30 s: éxito | 3,73 % (56) | 6,13 % (92) |
| Código 0 en Vegeta 50 req/s | 1.443 (1.442 timeouts) | 1.352 (312 timeouts y unos 1.040 inmediatos) |
| Respuestas 200 en los logs del servidor | 80 | 117 |

Lectura (hipótesis, sin confirmar):
- La latencia mediana mejora mucho y el servidor completa más pedidos (117 contra 80).
- A 8 req/s aparecen 44 timeouts, cifra cercana a los 40 pedidos de `03.pdf` de la rotación. Podría ser que los PDFs pesados queden relegados.
- A 50 req/s el P50 de 2 ms indica que más de la mitad de los pedidos falla de inmediato (errores de conexión, no timeouts). Falta ver el `Error Set` del reporte completo.
- Con este set, el techo de éxito a 50 req/s es de unos 9 a 10 % (estimación gruesa: unos 4 a 5 req/s sobre 50). Compararlo con el 66,53 % de la cátedra solo tiene sentido con los PDFs oficiales.

## Pendiente

- [x] Vegeta con el perfil de la consigna (50 req/s durante 30 s) y conteo de 200 en los logs.
- [ ] Repetir k6 y Vegeta con el protocolo definitivo (réplicas `healthy`, 1 minuto de reposo, 2 minutos entre corridas).
- [ ] Conseguir los PDFs oficiales y repetir las mediciones finales con ellos.
- [ ] Ver el `Error Set` de `perf4-vegeta-50` y repetir 8 req/s sin `03.pdf` para comprobar la hipótesis de los PDFs pesados.

## Re-medición detrás de Traefik (issue #38)

Rama `docs/38-re-medicion`, 2026-11-06. Imagen medida: extract
`ghcr.io/juandelosrios124/extract-service:sha-ec5b762`
(`sha256:79793696c0f6ae7459a0fbdac7a062a2fa0e020113bbfa91271f4da5b1b1fbe6`, revision `ec5b762`)
en las 19 corridas; documents `sha-3ef65f2`
(`sha256:5e94251bdd5f482624e4f2bf1b1f0414ce0b81dc9ccc4730a1818cc9b449e560`).
Resultados en `tests/stress/results/38/2026-11-06/` (resumen en `resumen.md`).

**Datos crudos.** Lo que no se versiona (`.bin` de Vegeta, `k6-raw.json`, un pedido por línea,
access log de Traefik, logs de extract y documents, ~1,5 GB) y la corrida descartada están
comprimidos fuera del repo, junto con los archivos versionados, en `extract-service-38-crudos-2026-11-06.tar.zst`
(21 MB, 378 archivos, sha256
`d059b51ef8e2f7c552b750ab37e1c4bcc64ba655db3f75c556c72810c3dd3655`), en la máquina de
medición. Para revisarlos: `tar -I zstd -xf extract-service-38-crudos-2026-11-06.tar.zst`.

### Entorno

- La misma notebook (Lenovo 81W3, Ryzen 3 4300U, 4 núcleos y 4 hilos), ahora con CachyOS:
  kernel `7.3.1-1-cachyos`, Docker Engine 29.9.2 con cgroup v2, 6,6 GiB visibles, enchufada.
- Topes de CPU: 5 × 1,0 (extract) + 0,5 (Traefik) + 1,0 (documents) + 0,5 (Mongo) = 7,0 sobre
  4 núcleos, más Consul y el generador, sin tope.
- Herramientas en contenedor dentro de `mired`: `peterevans/vegeta:6.9.1-vegeta12.8.4` y
  `grafana/k6:2.2.0`, con digest (ver `run-38.sh`). Scripts de la cátedra sin cambios; k6 con
  `TIMEOUT=30s`.

### Comprobaciones antes de medir (pedidos de prueba con 1 réplica)

- `503` por cupo: `OriginStatus` 503, body de 66 B; en extract, solo la línea de acceso.
- `503` por cola: `OriginStatus` 503, body de 70 B.
- `503` por deadline: `OriginStatus` 503 y `WARNING ... stopped by deadline` con `request_id`.
- `503` del CB abierto (expresión forzada): `OriginStatus` 0, sin `ServiceURL` ni
  `ServiceName`, body `Service Unavailable`; cuenta en `traefik_entrypoint_requests_total`
  pero no en `traefik_service_requests_total` (18 − 8 = 10, exacto).
- `429` del inflight: `OriginStatus` 0, body `max connections reached: N`; solo en el entrypoint.
- `504`: un PDF sintético de 4000 páginas con 0,25 CPU y sin deadline. `OriginStatus` 504,
  `OriginDuration` 25,0 s, body `Gateway Timeout`, `RetryAttempts` 0; extract terminó después
  y logueó un `200` que el cliente no recibió.
- `404`: en una ráfaga contra una réplica de 0,5 CPU, Traefik la sacó de la rotación a mitad
  de camino (hipótesis: venció el check de readiness de Consul, de 2 s) y respondió `404`.
- `NetworkErrorRatio() > 1` **no es válida** en Traefik v3.7.17: el router queda `disabled`
  con `expected int, got int` y todo da `404`. Con `> 1.0` funciona.
- El retry (2 intentos) no reintenta `POST`: la API de Traefik lo muestra sin
  `retryNonIdempotentMethod` y `RetryAttempts` dio 0 en todas las pruebas.
- Durante el recreate, Traefik muestra un momento las réplicas viejas: contar servidores no
  alcanza para saber si ya rutea las nuevas (se compara con las IPs de los contenedores).

### Corridas cortas de validación

Vegeta 10 s por configuración (reposo de 20 s) y una de k6 en C, en
`tests/stress/results/38/prueba-2026-11-06/` (no versionadas y borradas después: solo validaban los scripts). Éxito: A 83,00 %, C 84,00 %,
C+CB 85,20 %; único no-`200`: `429`. k6: 976 `200`, 13.876 `429` y 3 `503` por cupo, iguales
en las cuatro fuentes. Ahí apareció el bucle de `429` de k6 (modelo cerrado sin pausas, 100
usuarios contra un tope de 80).

### Matriz (3 rondas, A, C y C+CB, Vegeta y k6 alternados)

Datos crudos resumidos por corrida (de los `desglose.json`). El motivo de cada no-`200` sale
del access log de Traefik; el código 0 sale del cliente. En la corrida 04, k6 vio 4 de los
`429` como código 0 (`broken pipe`: Traefik respondió y cerró mientras k6 subía el PDF); acá
cuentan como `429`, como en Traefik.

| Corrida | Inicio (UTC) | Pedidos | `200` | `429` | `503` cupo / cola / deadline | `503` CB | `504` | `404` | Código 0: timeout / conexión | Éxito | `200`/s | P50 / P95 de los `200` (s) | Fuentes cierran |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 01-A-vegeta | 20:19:08 | 1500 | 1156 | 344 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 77,07 % | 35,24 | 1,78 / 4,11 | sí |
| 02-C-vegeta | 20:21:00 | 1500 | 1146 | 354 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 76,40 % | 35,72 | 1,78 / 4,28 | sí |
| 03-C+CB-vegeta | 20:22:53 | 1500 | 1146 | 352 | 2 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 76,40 % | 35,31 | 1,38 / 5,02 | sí |
| 04-A-k6 | 20:24:48 | 9997 | 898 | 9099 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 8,98 % | 22,34 | 2,14 / 8,15 | sí |
| 05-C-k6 | 20:26:50 | 14809 | 1063 | 13746 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 7,18 % | 26,57 | 2,54 / 5,44 | sí |
| 06-C+CB-k6 | 20:28:52 | 15065 | 1012 | 14053 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 6,72 % | 25,17 | 2,71 / 4,75 | sí |
| 07-A-k6 | 20:30:56 | 16191 | 981 | 15210 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 6,06 % | 24,46 | 2,60 / 5,86 | sí |
| 08-C-k6 | 20:33:01 | 16256 | 1035 | 15221 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 6,37 % | 25,81 | 2,40 / 5,10 | sí |
| 09-C+CB-k6 | 20:35:08 | 15176 | 1067 | 14105 | 4 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 7,03 % | 26,68 | 2,13 / 5,46 | sí |
| 10-A-vegeta | 20:37:14 | 1500 | 1203 | 297 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 80,20 % | 36,88 | 1,62 / 5,02 | sí |
| 11-C-vegeta | 20:39:10 | 1500 | 1194 | 306 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 79,60 % | 36,28 | 1,81 / 3,89 | sí |
| 12-C+CB-vegeta | 20:41:07 | 1500 | 1197 | 303 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 79,80 % | 36,26 | 1,50 / 4,52 | sí |
| 13-A-vegeta | 20:43:04 | 1500 | 1208 | 292 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 80,53 % | 37,58 | 1,87 / 3,31 | sí |
| 14-C-vegeta | 20:44:55 | 1500 | 1194 | 306 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 79,60 % | 36,69 | 1,57 / 3,99 | sí |
| 15-C+CB-vegeta | 20:46:48 | 1500 | 1178 | 322 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 78,53 % | 36,35 | 1,51 / 4,60 | sí |
| 16-A-k6 | 20:59:17 | 15953 | 893 | 15060 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 5,60 % | 22,10 | 2,67 / 6,50 | sí |
| 17-C-k6 | 21:01:22 | 14414 | 934 | 13473 | 7 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 6,48 % | 23,35 | 2,48 / 6,48 | sí |
| 18-C+CB-k6 | 21:03:27 | 15717 | 1031 | 14686 | 0 / 0 / 0 | 0 | 0 | 0 | 0 / 0 | 6,56 % | 25,46 | 2,08 / 5,71 | sí |

- Corrida 16 original (A/k6): cortada a las 17:49 (−03) por falta de memoria del sistema, apenas
  empezada la carga. Descartada (no versionada; está en el archivo de crudos, ver abajo). Las 16 a 18 se
  corrieron unos 10 minutos después, en primer plano.
- En las 18 corridas: 0 reintentos, 0 líneas de aviso en el log de Traefik (el CB nunca abrió),
  0 `200` tardíos en extract.
- `docker stats`: extract promedió 51,5 % de CPU por réplica (máximo 102,9 %); Traefik, 16,7 %
  en Vegeta y 29,2 % en k6, y en k6 llegó a su tope de 0,5 CPU en 51 de 191 muestras.

### Camino real: `POST /documents`

Corrida 19 (C+CB), documents sobre `documents_bench` (antes: 0 documentos; después: 596).
1.600 variantes únicas de los 4 PDFs (actualización incremental de la metadata, mismas páginas
y mismo texto extraído que los originales). Vegeta 50 req/s, 30 s, `-lazy`.

| Respuesta | Pedidos |
|---|---|
| `201` creados | 596 |
| `200` duplicate | 0 |
| `503` admisión de documents | 904 |
| `503`/`504` de extract | 0 |
| Código 0 | 0 |

Los 596 `201` se cruzan uno a uno (por `X-Request-ID`) con 596 `/extract` internos con `200`;
los 904 `503` no llamaron a extract. 18,57 `201`/s; P50 / P95 de los `201`: 2,19 / 2,55 s.
Todas las fuentes cierran (cliente, access log, `/metrics`, log de documents con 596
«Documento almacenado» y log de extract).
