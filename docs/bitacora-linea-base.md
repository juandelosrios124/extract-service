
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