"""Tests del autorregistro de la réplica en Consul (issue #16).

Ninguno toca la red: el cliente del agente recibe un transporte falso y la app
un registrador falso. Lo único que usa sockets de verdad es la resolución de la
IP propia, y contra loopback.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from extractor.config import Settings
from extractor.main import create_app
from extractor.services.consul import (
    CHECK_INTERVAL,
    CHECK_TIMEOUT,
    DEREGISTER_CRITICAL_AFTER,
    ConsulAgent,
    Registration,
    ReplicaRegistrar,
    build_registrar,
    shared_network_ip,
)


class FakeTransport:
    """Guarda los PUT que recibiría el agente de Consul."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []
        self.error = error

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> None:
        self.calls.append((url, body, headers))
        if self.error is not None:
            raise self.error


class FakeRegistrar:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.registered = 0
        self.deregistered = 0

    def register(self) -> bool:
        self.registered += 1
        return self.ok

    def deregister(self) -> bool:
        self.deregistered += 1
        return True


# --- El registro que se le manda a Consul ---------------------------------


def test_el_registro_tiene_lo_que_pide_la_issue():
    payload = Registration(service_id="abc123", address="172.20.0.3", port=8001).payload()

    assert payload["ID"] == "abc123"  # hostname del contenedor: único por réplica
    assert payload["Name"] == "extract"
    assert payload["Address"] == "172.20.0.3"
    assert payload["Port"] == 8001
    assert payload["Check"] == {
        "HTTP": "http://172.20.0.3:8001/health",
        "Interval": "10s",
        "Timeout": "2s",
        "DeregisterCriticalServiceAfter": "1m",
    }


def test_los_tiempos_del_check_son_los_de_la_issue():
    assert CHECK_INTERVAL == "10s"
    assert CHECK_TIMEOUT == "2s"
    assert DEREGISTER_CRITICAL_AFTER == "1m"


@pytest.mark.parametrize("prohibido", ["localhost", "127.0.0.1"])
def test_el_check_no_apunta_a_localhost(prohibido):
    # localhost, visto desde Consul, es el contenedor de Consul y no la réplica.
    check = Registration(service_id="abc", address="172.20.0.4", port=8001).payload()["Check"]

    assert prohibido not in check["HTTP"]


# --- La IP que se publica -------------------------------------------------


def test_la_ip_publicada_es_la_de_la_interfaz_que_llega_a_consul():
    # La réplica está en dos redes y gethostbyname no garantiza el orden:
    # se pregunta por qué interfaz saldría un paquete hacia Consul.
    assert shared_network_ip("http://127.0.0.1:8500") == "127.0.0.1"


def test_la_ip_se_resuelve_aunque_la_url_no_traiga_puerto():
    assert shared_network_ip("http://127.0.0.1") == "127.0.0.1"


# --- El cliente del agente de Consul --------------------------------------


def test_registrar_hace_put_al_endpoint_del_agente():
    transport = FakeTransport()
    agent = ConsulAgent("http://consul:8500", transport=transport)

    agent.register(Registration(service_id="abc", address="172.20.0.3", port=8001))

    url, body, headers = transport.calls[0]
    assert url == "http://consul:8500/v1/agent/service/register"
    assert b'"Name": "extract"' in body
    assert headers["Content-Type"] == "application/json"
    assert "X-Consul-Token" not in headers  # sin token configurado no se manda


def test_desregistrar_pega_al_endpoint_con_el_id():
    transport = FakeTransport()
    agent = ConsulAgent("http://consul:8500/", transport=transport)

    agent.deregister("abc123")

    url, _, _ = transport.calls[0]
    assert url == "http://consul:8500/v1/agent/service/deregister/abc123"


def test_el_token_de_acl_viaja_en_el_header():
    transport = FakeTransport()
    agent = ConsulAgent("http://consul:8500", token="un-uuid", transport=transport)

    agent.register(Registration(service_id="abc", address="172.20.0.3", port=8001))

    assert transport.calls[0][2]["X-Consul-Token"] == "un-uuid"


# --- Un Consul caído no puede impedir arrancar ----------------------------


def test_un_fallo_de_registro_no_se_propaga(caplog):
    transport = FakeTransport(error=OSError("connection refused"))
    registrar = ReplicaRegistrar(
        ConsulAgent("http://consul:8500", token="un-uuid", transport=transport),
        Registration(service_id="abc", address="172.20.0.3", port=8001),
    )

    with caplog.at_level(logging.WARNING):
        assert registrar.register() is False

    assert "un-uuid" not in caplog.text  # el token nunca se loguea


def test_un_fallo_al_desregistrar_tampoco_se_propaga():
    transport = FakeTransport(error=OSError("connection refused"))
    registrar = ReplicaRegistrar(
        ConsulAgent("http://consul:8500", transport=transport),
        Registration(service_id="abc", address="172.20.0.3", port=8001),
    )

    assert registrar.deregister() is False


def test_build_registrar_devuelve_none_si_no_puede_resolver_la_ip():
    # Host inexistente: tampoco acá se puede romper el arranque.
    assert build_registrar(consul_addr="http://no-existe.invalid:8500", token="", port=8001) is None


def test_build_registrar_usa_el_id_que_se_le_pasa():
    registrar = build_registrar(
        consul_addr="http://127.0.0.1:8500", token="", port=8001, service_id="replica-7"
    )

    assert registrar is not None
    assert registrar.registration.service_id == "replica-7"
    assert registrar.registration.address == "127.0.0.1"


# --- Integración con el ciclo de vida de la app ---------------------------


def test_la_app_no_toca_consul_si_esta_deshabilitado():
    fake = FakeRegistrar()
    app = create_app(Settings(CONSUL_ENABLED=False, LOG_LEVEL="WARNING"), consul_registrar=fake)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert fake.registered == 0
    assert fake.deregistered == 0


def test_la_app_registra_al_arrancar_y_desregistra_al_apagar():
    fake = FakeRegistrar()
    app = create_app(Settings(CONSUL_ENABLED=True, LOG_LEVEL="WARNING"), consul_registrar=fake)

    with TestClient(app) as client:
        assert fake.registered == 1
        assert fake.deregistered == 0
        assert client.get("/health").status_code == 200

    assert fake.deregistered == 1


def test_si_el_registro_falla_la_app_igual_atiende():
    fake = FakeRegistrar(ok=False)
    app = create_app(Settings(CONSUL_ENABLED=True, LOG_LEVEL="WARNING"), consul_registrar=fake)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert fake.registered == 1
    assert fake.deregistered == 0  # no se desregistra lo que nunca se registró
