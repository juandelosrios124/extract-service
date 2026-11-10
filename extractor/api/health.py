"""Liveness y readiness.

Los dos chequeos responden a preguntas distintas y tienen consumidores
distintos (issue #35):

* ``GET /health`` — **liveness**: el proceso está vivo y el event loop
  responde. No mira el pool de extracción a propósito: si lo mirara, bajo
  carga las 5 réplicas se marcarían mal a la vez y el servicio caería
  completo. Lo usa el ``HEALTHCHECK`` de Docker, que marca el contenedor
  ``unhealthy`` y es lo que espera ``docker compose up --wait``. Docker
  Engine y Compose **no reinician** un contenedor por estar ``unhealthy``
  (eso lo hacen Swarm o Kubernetes): lo que sí lo levanta es
  ``restart: unless-stopped`` si el proceso termina.
* ``GET /health/ready`` — **readiness**: además, el hilo de extracción no está
  trabado. Lo usa el check de Consul, y con él Traefik saca a la réplica del
  balanceo sin reiniciarla.

La readiness existe porque el ``EXTRACTION_DEADLINE`` de la issue #34 se
chequea *entre* páginas: una única página que cuelgue no se puede interrumpir,
y con ``THREAD_POOL_SIZE=1`` eso deja la réplica sin poder atender mientras
``/health`` sigue diciendo ``ok``.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from extractor.api.dependencies import get_running_extractions, get_settings
from extractor.config import Settings

router = APIRouter()


@router.get("")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    settings: Settings = Depends(get_settings),
    running=Depends(get_running_extractions),
) -> JSONResponse:
    segundos = running.seconds_of_oldest()
    limite = settings.READINESS_STUCK_AFTER

    if limite and segundos > limite:
        return JSONResponse(
            status_code=503,
            content={
                "status": "stuck",
                "extracting_for_seconds": round(segundos, 1),
                "stuck_after_seconds": limite,
            },
        )

    return JSONResponse(
        status_code=200,
        content={"status": "ready", "extracting_for_seconds": round(segundos, 1)},
    )
