"""Autorregistro de la réplica en Consul (service discovery, issue #16).

Esta capa no conoce HTTP entrante ni FastAPI: expone el armado del registro,
un cliente mínimo del agente de Consul sobre ``urllib`` (sin dependencias
nuevas) y un registrador que **nunca** propaga errores, porque un Consul caído
no tiene que impedir que la réplica arranque.
"""

import json
import logging
import socket
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, Protocol
from urllib.parse import urlsplit

from extractor.config import Settings

logger = logging.getLogger(__name__)

# Fijados por la issue #16: el nombre con el que documents-service y Traefik
# buscan al servicio, y los tiempos del health check.
SERVICE_NAME = "extract"
# Lo que chequea Consul: readiness, no liveness (issue #35).
READINESS_PATH = "/health/ready"
CHECK_INTERVAL = "10s"
CHECK_TIMEOUT = "2s"
# Un "docker kill" no da tiempo a desregistrarse: pasado este tiempo en estado
# critical, Consul saca la réplica del catálogo por su cuenta.
DEREGISTER_CRITICAL_AFTER = "1m"

# Segundos de tope para hablarle al agente: el arranque no puede quedarse
# esperando a un Consul que no responde.
REQUEST_TIMEOUT = 2.0

# (url, body, headers) -> None. Inyectable para poder testear sin red.
Transport = Callable[[str, bytes, dict[str, str]], None]


@dataclass(frozen=True)
class Registration:
    """Lo que esta réplica le dice a Consul sobre sí misma."""

    service_id: str
    address: str
    port: int
    name: str = SERVICE_NAME
    #: Tags del catálogo. Con ellos Traefik publica el servicio (issue #32).
    tags: tuple[str, ...] = ()

    def payload(self) -> dict:
        return {
            "ID": self.service_id,
            "Name": self.name,
            "Address": self.address,
            "Port": self.port,
            "Tags": list(self.tags),
            "Check": {
                # A la IP de la réplica: "localhost", visto desde Consul, es el
                # propio contenedor de Consul.
                # Readiness y no liveness (issue #35): si el hilo de extracción
                # está trabado, Traefik tiene que sacarla del balanceo. Reiniciar
                # el contenedor es otra decisión, y la toma el HEALTHCHECK de
                # Docker, que sigue apuntando a /health.
                "HTTP": f"http://{self.address}:{self.port}{READINESS_PATH}",
                "Interval": CHECK_INTERVAL,
                "Timeout": CHECK_TIMEOUT,
                "DeregisterCriticalServiceAfter": DEREGISTER_CRITICAL_AFTER,
            },
        }


def traefik_tags(settings: Settings) -> tuple[str, ...]:
    """Tags con los que Traefik publica este servicio (issue #32).

    Traefik corre con ``providers.consulcatalog`` y ``exposedByDefault=false``,
    así que sin ``traefik.enable=true`` la réplica no se publica. El router, los
    tres middlewares y el service se declaran acá y no con labels de Docker, de
    modo que la configuración viaja con el registro de cada réplica.

    ``serversTransport`` es lo único que no se puede declarar por tags: lo
    define el provider file de infra como ``extract-transport``.

    El retry va sin ``status`` a propósito (issue #55). Así, en Traefik v3.7,
    solo reintenta ante un error de red **antes de mandarle un byte a la
    réplica** (por ejemplo, el dial a una réplica muerta), también en ``POST``:
    ese camino no mira el método. Como no se mandó nada, el body se reenvía sin
    guardarlo en memoria, así que ``maxRequestBodyBytes`` no lo limita. Un
    ``504`` por ``responseHeaderTimeout`` no se reintenta, porque el pedido ya
    se mandó, y el presupuesto de timeouts se mantiene. ``retryNonIdempotentMethod``
    solo habilita el retry por ``status``, que no se usa, así que no se declara.
    Reintentar ``/extract`` sería seguro igual: es una función pura y no guarda
    nada.
    """
    router = f"traefik.http.routers.{SERVICE_NAME}"
    middleware = f"traefik.http.middlewares.{SERVICE_NAME}"
    service = f"traefik.http.services.{SERVICE_NAME}"
    tags = [
        "traefik.enable=true",
        f"{router}.rule=PathPrefix(`/{SERVICE_NAME}`)",
        # Solo el entrypoint de tráfico: el otro es el del dashboard.
        f"{router}.entrypoints=web",
        f"{router}.middlewares="
        f"{SERVICE_NAME}-cb,{SERVICE_NAME}-retry,{SERVICE_NAME}-inflight",
        f"{middleware}-cb.circuitbreaker.expression={settings.TRAEFIK_CB_EXPRESSION}",
        f"{middleware}-cb.circuitbreaker.fallbackDuration={settings.TRAEFIK_CB_FALLBACK}",
        f"{middleware}-cb.circuitbreaker.recoveryDuration={settings.TRAEFIK_CB_RECOVERY}",
        f"{middleware}-retry.retry.attempts={settings.TRAEFIK_RETRY_ATTEMPTS}",
        f"{middleware}-inflight.inflightreq.amount={settings.TRAEFIK_INFLIGHT}",
        f"{service}.loadbalancer.serverstransport=extract-transport@file",
    ]
    if settings.TRAEFIK_INFLIGHT_SOURCE_HEADER:
        # Sin sourceCriterion, Traefik cuenta los pedidos en vuelo por valor del
        # header Host: cada Host tiene su propio tope y un cliente lo saltea
        # cambiándolo. Traefik borra el X-Forwarded-Server que manda un cliente
        # no confiable y lo vuelve a escribir con su hostname, así que agrupar
        # por ese header deja un único contador que no se puede falsificar.
        tags.append(
            f"{middleware}-inflight.inflightreq.sourcecriterion.requestheadername="
            f"{settings.TRAEFIK_INFLIGHT_SOURCE_HEADER}"
        )
    return tuple(tags)


def shared_network_ip(consul_addr: str) -> str:
    """IP propia en la red por la que se alcanza a Consul.

    En vez de confiar en el orden que devuelve ``gethostbyname``, que no está
    garantizado, se le pregunta al sistema por qué interfaz saldría un paquete
    hacia Consul. Con UDP no se manda nada: ``connect`` solo resuelve la ruta.
    Desde la #33 la réplica está en una sola red, pero el método sigue siendo
    el correcto si mañana vuelve a estar en varias.
    """
    parts = urlsplit(consul_addr)
    host = parts.hostname or "consul"
    port = parts.port or 8500
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((host, port))
        return probe.getsockname()[0]


def _urllib_put(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
    request = urllib.request.Request(url, data=body, headers=headers, method="PUT")
    with urllib.request.urlopen(request, timeout=timeout):  # noqa: S310 (URL propia)
        pass


class ConsulAgent:
    """Cliente mínimo de la API HTTP del agente de Consul."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = REQUEST_TIMEOUT,
        transport: Optional[Transport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport: Transport = transport or (
            lambda url, body, headers: _urllib_put(url, body, headers, timeout)
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            # El token sale del entorno y nunca se loguea ni se versiona.
            headers["X-Consul-Token"] = self._token
        return headers

    def register(self, registration: Registration) -> None:
        body = json.dumps(registration.payload()).encode()
        self._transport(f"{self._base_url}/v1/agent/service/register", body, self._headers())

    def deregister(self, service_id: str) -> None:
        self._transport(
            f"{self._base_url}/v1/agent/service/deregister/{service_id}", b"", self._headers()
        )


class Registrar(Protocol):
    """Lo que el ciclo de vida de la app necesita saber del registro."""

    def register(self) -> bool: ...

    def deregister(self) -> bool: ...


class ReplicaRegistrar:
    """Registra esta réplica al arrancar y la desregistra al apagarse.

    Ningún fallo se propaga: la issue pide que el servicio funcione igual sin
    Consul y que un error de registro no impida arrancar. Se devuelve si la
    operación salió bien y se deja el rastro en el log.
    """

    def __init__(self, agent: ConsulAgent, registration: Registration) -> None:
        self._agent = agent
        self.registration = registration

    def register(self) -> bool:
        try:
            self._agent.register(self.registration)
        except Exception as error:  # noqa: BLE001 - arrancar es más importante que registrarse
            logger.warning(
                "No se pudo registrar %s en Consul: %s", self.registration.service_id, error
            )
            return False
        logger.info(
            "Réplica %s registrada en Consul como %s en %s:%d",
            self.registration.service_id,
            self.registration.name,
            self.registration.address,
            self.registration.port,
        )
        return True

    def deregister(self) -> bool:
        try:
            self._agent.deregister(self.registration.service_id)
        except Exception as error:  # noqa: BLE001 - apagarse prolijo no puede fallar ruidosamente
            logger.warning(
                "No se pudo desregistrar %s de Consul: %s", self.registration.service_id, error
            )
            return False
        logger.info("Réplica %s desregistrada de Consul", self.registration.service_id)
        return True


def build_registrar(
    *,
    consul_addr: str,
    token: str,
    port: int,
    service_id: Optional[str] = None,
    tags: tuple[str, ...] = (),
) -> Optional[ReplicaRegistrar]:
    """Arma el registrador de esta réplica, o ``None`` si no se pudo.

    El ID es el hostname del contenedor (único por réplica) y la dirección, la
    IP de la red por la que se llega a Consul. Si esa IP no se puede resolver
    (por ejemplo, Consul no está levantado) se loguea y se devuelve ``None``:
    el arranque sigue.
    """
    service_id = service_id or socket.gethostname()
    try:
        address = shared_network_ip(consul_addr)
    except OSError as error:
        logger.warning("No se pudo resolver la IP propia hacia %s: %s", consul_addr, error)
        return None
    return ReplicaRegistrar(
        ConsulAgent(consul_addr, token=token),
        Registration(service_id=service_id, address=address, port=port, tags=tags),
    )
