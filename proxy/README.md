# proxy

Reverse proxy [Caddy](https://caddyserver.com/) delante de las réplicas de `extract-service`.
Es el único punto de entrada al servicio: `http://localhost:8001` (`PROXY_PORT`).

## Balanceo

Caddy resuelve el nombre `extract` en el DNS de Docker, que devuelve una IP por réplica, y
reparte las solicitudes con `round_robin`. Vuelve a resolver cada 5s, así que si se cambia
la cantidad de réplicas no hace falta reiniciar el proxy. No usa el socket de Docker.

Cada respuesta trae el header `X-Upstream` con la réplica que la atendió. Para comprobar
el reparto:

```bash
for i in $(seq 50); do
  curl -s -o /dev/null -D - http://localhost:8001/health | grep -i x-upstream
done | sort | uniq -c
```

Con 5 réplicas tienen que aparecer 5 IPs con 10 solicitudes cada una. Para ver qué
contenedor tiene cada IP:

```bash
docker compose ps -q extract | xargs docker inspect \
  -f '{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
```

## Réplicas y límites

Se definen en `docker-compose.yml` y se configuran desde el `.env` de la raíz:

| Variable           | Default | Descripción                                   |
|--------------------|---------|-----------------------------------------------|
| `EXTRACT_REPLICAS` | `5`     | Réplicas de extract (la consigna permite hasta 5) |
| `EXTRACT_CPUS`     | `1.0`   | Límite de CPU por réplica (máx. 1.0)          |
| `EXTRACT_MEMORY`   | `1g`    | Límite de RAM por réplica (entre `512m` y `1g`) |

## Timeouts

Todos se configuran por variables de entorno (en el `.env` de la raíz) y se aplican al
reiniciar el proxy: `docker compose up -d proxy`.

| Variable                  | Default | Qué limita                                                        | Si se supera |
|---------------------------|---------|-------------------------------------------------------------------|--------------|
| `PROXY_DIAL_TIMEOUT`      | `5s`    | Abrir la conexión TCP con una réplica                             | Prueba otra réplica |
| `PROXY_TRY_DURATION`      | `5s`    | Tiempo total reintentando réplicas que no aceptan la conexión     | `502` |
| `PROXY_RESPONSE_TIMEOUT`  | `60s`   | Espera hasta que la réplica empieza a responder (la extracción)   | `504` |
| `PROXY_READ_BODY_TIMEOUT` | `30s`   | Recibir el body del cliente (el PDF)                              | Corta la conexión |
| `PROXY_WRITE_TIMEOUT`     | `2m`    | Escribir la respuesta completa al cliente                         | Corta la conexión |
| `PROXY_IDLE_TIMEOUT`      | `2m`    | Conexiones keep-alive inactivas                                   | Cierra la conexión |

### Cómo influyen en las pruebas de carga

- Con Vegeta y un timeout de cliente de 30s, `PROXY_RESPONSE_TIMEOUT` (60s) es mayor: si el
  servicio se satura, corta el cliente y Vegeta lo reporta como timeout (código `0`).
  Si se baja a menos de 30s, esas mismas solicitudes aparecen como `504` del proxy.
  Hay que tenerlo en cuenta al comparar corridas.
- `PROXY_WRITE_TIMEOUT` cuenta desde que se leen los headers del request (incluye subir el
  PDF y esperar la extracción), así que tiene que ser mayor que `PROXY_READ_BODY_TIMEOUT` +
  `PROXY_RESPONSE_TIMEOUT`; si no, el proxy corta respuestas que la réplica sí terminó.
- `PROXY_TRY_DURATION` solo actúa cuando una réplica rechaza conexiones (por ejemplo, si se
  reinició por falta de memoria). No reintenta solicitudes lentas.
