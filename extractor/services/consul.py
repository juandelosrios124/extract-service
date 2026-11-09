"""Autorregistro de la réplica en Consul (service discovery, issue #16).

Esta capa no conoce HTTP entrante ni FastAPI: expone el armado del registro,
un cliente mínimo del agente de Consul sobre ``urllib`` (sin dependencias
nuevas) y un registrador que **nunca** propaga errores, porque un Consul caído
no tiene que impedir que la réplica arranque.
"""

import asyncio
import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol
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

# Backoff del reintento de registro: 1s, se duplica, tope 30s. Cubre a Consul
# inicializando el ACL unos segundos después del primer intento (403).
RETRY_INITIAL_DELAY = 1.0
RETRY_MAX_DELAY = 30.0

# Cada cuántos segundos se verifica que el agente siga teniendo la réplica.
# Consul la borra tras DEREGISTER_CRITICAL_AFTER en critical (issue #61).
RECHECK_INTERVAL = 30.0

# (url, body, headers) -> None. Inyectable para poder testear sin red.
Transport = Callable[[str, bytes, dict[str, str]], None]
# (url, headers) -> código HTTP. El GET de verificación, también inyectable.
StatusGetter = Callable[[str, dict[str, str]], int]


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


def _urllib_get_status(url: str, headers: dict[str, str], timeout: float) -> int:
    """Código HTTP de un GET. Un 4xx/5xx se devuelve; un error de red, se propaga."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (URL propia)
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


class ConsulError(Exception):
    """Consul respondió algo que no sabemos interpretar."""


class ConsulAgent:
    """Cliente mínimo de la API HTTP del agente de Consul."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = REQUEST_TIMEOUT,
        transport: Optional[Transport] = None,
        getter: Optional[StatusGetter] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport: Transport = transport or (
            lambda url, body, headers: _urllib_put(url, body, headers, timeout)
        )
        self._getter: StatusGetter = getter or (
            lambda url, headers: _urllib_get_status(url, headers, timeout)
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

    def has_service(self, service_id: str) -> bool:
        """Si el agente tiene registrado ``service_id`` (200 sí, 404 no).

        Cualquier otro código (por ejemplo, un 403 del ACL) es ``ConsulError``:
        no dice nada sobre si el servicio está o no.
        """
        status = self._getter(f"{self._base_url}/v1/agent/service/{service_id}", self._headers())
        if status == 200:
            return True
        if status == 404:
            return False
        raise ConsulError(f"respuesta inesperada del agente: HTTP {status}")


class Registrar(Protocol):
    """Lo que el ciclo de vida de la app necesita saber del registro."""

    def register(self) -> bool: ...

    def deregister(self) -> bool: ...

    def is_registered(self) -> Optional[bool]: ...


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

    def is_registered(self) -> Optional[bool]:
        """Si el agente todavía tiene esta réplica.

        ``True`` si la tiene (200), ``False`` si no (404) y ``None`` si no se
        pudo saber (Consul caído, timeout, 403...). ``None`` no es motivo para
        volver a registrarse: con Consul caído el registro fallaría igual.
        """
        try:
            return self._agent.has_service(self.registration.service_id)
        except Exception as error:  # noqa: BLE001 - verificar nunca puede tirar abajo la tarea
            logger.warning(
                "No se pudo verificar el registro de %s en Consul: %s",
                self.registration.service_id,
                error,
            )
            return None


async def register_with_retry(
    registrar: Registrar,
    *,
    initial_delay: float = RETRY_INITIAL_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Reintenta el registro, con backoff acotado, hasta lograrlo.

    Corre dentro de la tarea de fondo (``keep_registered``): el arranque y
    ``/health`` no lo esperan. El cliente es bloqueante, así que cada intento va
    en un thread. Cada fallo ya deja su WARNING (lo loguea el registrador); al
    lograrlo se loguea ``consul_registrado=True``. Termina al registrarse o al
    cancelarse la tarea.
    """
    delay = initial_delay
    attempt = 1
    while not await asyncio.to_thread(registrar.register):
        await sleep(delay)
        delay = min(delay * 2, max_delay)
        attempt += 1
    logger.info("Registro en Consul completado: consul_registrado=True (intentos=%d)", attempt)


async def keep_registered(
    registrar: Registrar,
    *,
    recheck_interval: float = RECHECK_INTERVAL,
    initial_delay: float = RETRY_INITIAL_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Mantiene esta réplica en el catálogo hasta que la tarea se cancele.

    Registra con backoff (``register_with_retry``) y después verifica cada
    ``recheck_interval`` que el agente la siga teniendo. Si Consul la borró
    (``DEREGISTER_CRITICAL_AFTER`` tras quedar colgada, issue #61), vuelve a
    registrarla con el mismo backoff. Si la verificación no puede responder
    (``None``), no se toca nada y se vuelve a mirar en el próximo ciclo.

    Al cancelarse (apagado) se desregistra, solo si se llegó a registrar
    alguna vez.
    """
    registered_once = False
    try:
        while True:
            await register_with_retry(
                registrar, initial_delay=initial_delay, max_delay=max_delay, sleep=sleep
            )
            registered_once = True
            while True:
                await sleep(recheck_interval)
                # `is False` y no `not`: None significa "no sé", no "no estoy".
                if await asyncio.to_thread(registrar.is_registered) is False:
                    break
            logger.warning("Consul ya no tiene la réplica, se vuelve a registrar")
    finally:
        if registered_once:
            await asyncio.to_thread(registrar.deregister)


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
