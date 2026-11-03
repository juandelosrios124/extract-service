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
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Fijados por la issue #16: el nombre con el que documents-service y el proxy
# buscan al servicio, y los tiempos del health check.
SERVICE_NAME = "extract"
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

    def payload(self) -> dict:
        return {
            "ID": self.service_id,
            "Name": self.name,
            "Address": self.address,
            "Port": self.port,
            "Check": {
                # A la IP de la réplica: "localhost", visto desde Consul, es el
                # propio contenedor de Consul.
                "HTTP": f"http://{self.address}:{self.port}/health",
                "Interval": CHECK_INTERVAL,
                "Timeout": CHECK_TIMEOUT,
                "DeregisterCriticalServiceAfter": DEREGISTER_CRITICAL_AFTER,
            },
        }


def shared_network_ip(consul_addr: str) -> str:
    """IP propia en la red por la que se alcanza a Consul.

    La réplica está en dos redes (la del proyecto y la compartida ``mired``) y
    el orden que devuelve ``gethostbyname`` no está garantizado, así que en vez
    de elegir a ciegas se le pregunta al sistema por qué interfaz saldría un
    paquete hacia Consul. Con UDP no se manda nada: ``connect`` solo resuelve
    la ruta.
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
        Registration(service_id=service_id, address=address, port=port),
    )
