# Bitácora de la línea base (issue 1)

Rama `docs/1-linea-base`. Commit del código medido: `b1595c9`.
Esta bitácora junta lo que se midió y lo que se concluyó, para armar después el informe.
Todo lo marcado como **hipótesis** todavía no está confirmado.

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
