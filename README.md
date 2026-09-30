# extract_service

Microservicio mínimo y sin estado que extrae el texto de un PDF con PyMuPDF.
No usa MongoDB, JWT ni Ollama, y procesa el PDF en memoria, sin escribir a disco.

## Endpoints

| Método | Ruta       | Descripción                                                              |
|--------|------------|--------------------------------------------------------------------------|
| POST   | `/extract` | PDF por `multipart/form-data` (campo `file`) o como body binario. Responde `{content, page_count}`. |
| GET    | `/health`  | Healthcheck: `{"status": "ok"}`.                                         |

Errores de `/extract`: `400` si el PDF es inválido, el body está vacío o falta el campo `file`; `413` si se supera `MAX_UPLOAD_SIZE`.

## Configuración

Todo se configura por variables de entorno (ver `.env.example`): `HOST`, `PORT`,
`MAX_UPLOAD_SIZE`, `THREAD_POOL_SIZE`, `LOG_LEVEL`. Los valores inválidos hacen fallar el arranque.
Los logs salen por stdout.

## Uso

```bash
cd extract_service
uv sync
uv run pytest
uv run --env-file .env python -m extractor   # después de: cp .env.example .env
```

Con Docker, desde la raíz del repo: `docker compose up --build extract proxy`. Levanta
las réplicas del servicio detrás del reverse proxy, que atiende en `http://localhost:8001`
(ver [proxy/README.md](../proxy/README.md)).

## Estructura

```
extractor/
├── __main__.py        # entrypoint: lee el entorno, configura logs y levanta uvicorn
├── main.py            # composition root: create_app(settings) y pool de threads
├── config.py          # Settings desde variables de entorno
├── logging_config.py  # logs a stdout
├── api/               # capa HTTP: routers, schemas, dependencias
└── services/          # caso de uso de extracción, sin dependencias de HTTP
```
