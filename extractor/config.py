"""Service configuration.

Follows 12-Factor App (III. Config): every setting comes from environment
variables. No .env file is read at runtime; for local development load it
explicitly, e.g. ``uv run --env-file .env python -m extractor``.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables, validated at startup."""

    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")

    HOST: str = "0.0.0.0"
    PORT: int = Field(default=8001, ge=1, le=65535)
    MAX_UPLOAD_SIZE: int = Field(default=50 * 1024 * 1024, gt=0)  # bytes
    # Los defaults de abajo son los valores de producción (docker-compose.yml): si una
    # variable no llega a la réplica, el servicio arranca protegido y no sin límites.
    THREAD_POOL_SIZE: int = Field(default=1, gt=0)
    # Backpressure: segundos máximos entre que empieza el pedido y que arranca la extracción.
    # Pasado ese tiempo el cliente ya suele haberse ido: se responde 503 sin gastar CPU.
    # Los 5 + 15 + 8 de abajo son el tramo de extract del presupuesto de timeouts
    # de punta a punta (cliente 30 s > documents 27 s > Traefik 25 s > extract 23 s).
    MAX_QUEUE_WAIT: float = Field(default=15.0, gt=0)
    # Backpressure: cuántos pedidos atiende a la vez cada réplica (leer el cuerpo y extraer).
    # 0 = sin límite. Los demás esperan hasta UPLOAD_SLOT_TIMEOUT segundos y después reciben 503.
    MAX_CONCURRENT_UPLOADS: int = Field(default=8, ge=0)
    UPLOAD_SLOT_TIMEOUT: float = Field(default=5.0, gt=0)
    # Segundos máximos de extracción, chequeados entre páginas. Pasado el
    # deadline se responde 503: run_in_executor no se puede cancelar, así que sin
    # esto un PDF pesado congela la réplica entera (issue #34). 0 = sin límite.
    EXTRACTION_DEADLINE: float = Field(default=8.0, ge=0)
    # Tope duro de páginas, rechazado con 422 antes de extraer nada. No está
    # afinado contra el deadline (el costo por página varía muchísimo): es un
    # techo para entradas absurdas, y el deadline es la protección real.
    # 0 = sin límite.
    MAX_PAGES: int = Field(default=10000, ge=0)
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # json para los recolectores de logs, text para leer en una terminal.
    LOG_FORMAT: Literal["text", "json"] = "text"

    # Service discovery. Estos tres nombres son comunes a los tres repos del
    # proyecto (infra, extract y documents): no renombrarlos.
    # Apagado por defecto: sin Consul el servicio funciona igual.
    CONSUL_ENABLED: bool = False
    CONSUL_HTTP_ADDR: str = "http://consul:8500"
    # Token de ACL: solo por entorno, nunca en el repo.
    CONSUL_HTTP_TOKEN: str = ""

    # Traefik, configurado con tags en el registro de Consul (issue #32). El
    # breaker cuenta el rango [500, 503): deja afuera el 503 del backpressure,
    # que es la respuesta sana de una replica llena y no una falla. Si contara,
    # cuanto mejor descarta carga el servicio, mas probable que Traefik lo corte
    # del todo. Ver "Publicacion en Traefik" en el README.
    TRAEFIK_CB_EXPRESSION: str = (
        "NetworkErrorRatio() > 0.30 || ResponseCodeRatio(500, 503, 0, 600) > 0.50"
    )
    TRAEFIK_CB_FALLBACK: str = "10s"
    TRAEFIK_CB_RECOVERY: str = "10s"
    TRAEFIK_RETRY_ATTEMPTS: int = Field(default=2, ge=1)
    # Tope de pedidos simultaneos en el borde. El doble de la capacidad agregada
    # (5 replicas x 8 cupos): el control primario sigue siendo la admision por
    # cupos de la app, que es la que esta medida en el informe.
    TRAEFIK_INFLIGHT: int = Field(default=80, gt=0)
