"""Tests del autorregistro de la réplica en Consul (issue #16).

Ninguno toca la red: el cliente del agente recibe un transporte falso y la app
un registrador falso. Lo único que usa sockets de verdad es la resolución de la
IP propia, y contra loopback.
"""

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from extractor.config import Settings
from extractor.main import create_app
from extractor.services.consul import (
    CHECK_INTERVAL,
    CHECK_TIMEOUT,
    DEREGISTER_CRITICAL_AFTER,
    READINESS_PATH,
    ConsulAgent,
    Registration,
    ReplicaRegistrar,
    build_registrar,
    register_with_retry,
    shared_network_ip,
    traefik_tags,
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
        # Readiness y no liveness: issue #35.
        "HTTP": "http://172.20.0.3:8001/health/ready",
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
    # gethostbyname no garantiza el orden de las interfaces: se pregunta por
    # cual saldria un paquete hacia Consul.
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


class FakeGetter:
    """Responde el GET de verificación con un código fijo, o falla como la red."""

    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.status = status
        self.error = error

    def __call__(self, url: str, headers: dict[str, str]) -> int:
        self.calls.append((url, headers))
        if self.error is not None:
            raise self.error
        return self.status


def registrador_con(getter: FakeGetter) -> ReplicaRegistrar:
    return ReplicaRegistrar(
        ConsulAgent(
            "http://consul:8500/", token="un-uuid", transport=FakeTransport(), getter=getter
        ),
        Registration(service_id="abc123", address="172.20.0.3", port=8001),
    )


def test_la_verificacion_pega_al_endpoint_del_servicio_con_el_token():
    getter = FakeGetter(200)

    registrador_con(getter).is_registered()

    url, headers = getter.calls[0]
    assert url == "http://consul:8500/v1/agent/service/abc123"
    assert headers["X-Consul-Token"] == "un-uuid"


@pytest.mark.parametrize(("status", "esperado"), [(200, True), (404, False)])
def test_is_registered_interpreta_200_y_404(status, esperado):
    assert registrador_con(FakeGetter(status)).is_registered() is esperado


@pytest.mark.parametrize(
    "getter",
    [
        FakeGetter(error=OSError("connection refused")),
        FakeGetter(error=TimeoutError("timed out")),
        FakeGetter(403),  # ACL: no dice si el servicio está
        FakeGetter(500),
    ],
)
def test_is_registered_da_none_si_no_se_puede_saber(getter, caplog):
    with caplog.at_level(logging.WARNING):
        assert registrador_con(getter).is_registered() is None

    assert "No se pudo verificar" in caplog.text
    assert "un-uuid" not in caplog.text  # el token nunca se loguea


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


def esperar(condicion, segundos: float = 2.0) -> None:
    """Espera a la tarea de fondo del registro (corre en el loop de la app)."""
    import time

    limite = time.monotonic() + segundos
    while not condicion() and time.monotonic() < limite:
        time.sleep(0.01)


def test_la_app_registra_al_arrancar_y_desregistra_al_apagar():
    fake = FakeRegistrar()
    app = create_app(Settings(CONSUL_ENABLED=True, LOG_LEVEL="WARNING"), consul_registrar=fake)

    with TestClient(app) as client:
        esperar(lambda: fake.registered >= 1)
        assert fake.registered == 1
        assert fake.deregistered == 0
        assert client.get("/health").status_code == 200

    assert fake.deregistered == 1


def test_si_el_registro_falla_la_app_igual_atiende():
    fake = FakeRegistrar(ok=False)
    app = create_app(Settings(CONSUL_ENABLED=True, LOG_LEVEL="WARNING"), consul_registrar=fake)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        esperar(lambda: fake.registered >= 1)

    assert fake.registered >= 1  # sigue reintentando hasta el apagado
    assert fake.deregistered == 0  # no se desregistra lo que nunca se registró


def test_el_registro_reintenta_tras_dos_403_hasta_el_200(caplog):
    class ConsulInicializandoAcl:
        def __init__(self) -> None:
            self.llamadas = 0

        def __call__(self, url, body, headers) -> None:
            self.llamadas += 1
            if self.llamadas <= 2:
                raise OSError("403 ACL not found")

    transport = ConsulInicializandoAcl()
    registrar = ReplicaRegistrar(
        ConsulAgent("http://consul:8500", transport=transport),
        Registration(service_id="abc", address="172.20.0.3", port=8001),
    )
    esperas: list[float] = []

    async def sleep_falso(segundos: float) -> None:
        esperas.append(segundos)

    with caplog.at_level(logging.INFO):
        asyncio.run(register_with_retry(registrar, sleep=sleep_falso))

    assert transport.llamadas == 3
    assert esperas == [1.0, 2.0]  # backoff exponencial
    assert caplog.text.count("No se pudo registrar") == 2  # un WARNING por fallo
    assert "consul_registrado=True" in caplog.text


def test_el_backoff_se_frena_en_el_maximo():
    class HastaElSexto:
        intentos = 0

        def register(self) -> bool:
            self.intentos += 1
            return self.intentos == 6

    esperas: list[float] = []

    async def sleep_falso(segundos: float) -> None:
        esperas.append(segundos)

    asyncio.run(
        register_with_retry(HastaElSexto(), initial_delay=1.0, max_delay=4.0, sleep=sleep_falso)
    )

    assert esperas == [1.0, 2.0, 4.0, 4.0, 4.0]


# --- Tags de Traefik en el registro (issue #32) ---------------------------


def tags_de(settings: Settings) -> dict[str, str]:
    """Los tags como diccionario clave -> valor, para poder afirmar de a uno."""
    return dict(tag.split("=", 1) for tag in traefik_tags(settings))


def test_los_tags_declaran_el_router_los_middlewares_y_el_service():
    tags = tags_de(Settings())

    assert tags["traefik.enable"] == "true"
    assert tags["traefik.http.routers.extract.rule"] == "PathPrefix(`/extract`)"
    # Solo el entrypoint de trafico: el otro publica el dashboard.
    assert tags["traefik.http.routers.extract.entrypoints"] == "web"
    assert (
        tags["traefik.http.routers.extract.middlewares"]
        == "extract-cb,extract-retry,extract-inflight"
    )
    assert (
        tags["traefik.http.services.extract.loadbalancer.serverstransport"]
        == "extract-transport@file"
    )


def test_los_valores_de_los_middlewares_salen_de_settings():
    settings = Settings(
        TRAEFIK_CB_EXPRESSION="NetworkErrorRatio() > 0.99",
        TRAEFIK_CB_FALLBACK="30s",
        TRAEFIK_CB_RECOVERY="45s",
        TRAEFIK_RETRY_ATTEMPTS=5,
        TRAEFIK_INFLIGHT=123,
    )

    tags = tags_de(settings)

    assert (
        tags["traefik.http.middlewares.extract-cb.circuitbreaker.expression"]
        == "NetworkErrorRatio() > 0.99"
    )
    assert tags["traefik.http.middlewares.extract-cb.circuitbreaker.fallbackDuration"] == "30s"
    assert tags["traefik.http.middlewares.extract-cb.circuitbreaker.recoveryDuration"] == "45s"
    assert tags["traefik.http.middlewares.extract-retry.retry.attempts"] == "5"
    assert tags["traefik.http.middlewares.extract-inflight.inflightreq.amount"] == "123"


INFLIGHT_SOURCE = (
    "traefik.http.middlewares.extract-inflight.inflightreq.sourcecriterion.requestheadername"
)


def test_el_inflight_es_un_unico_tope_por_defecto():
    """Issue #55: sin sourceCriterion, Traefik agrupa por Host y cada Host
    tiene su propio tope. X-Forwarded-Server lo reescribe Traefik con su
    hostname en todos los pedidos: un solo contador."""
    tags = tags_de(Settings())

    assert tags[INFLIGHT_SOURCE] == "X-Forwarded-Server"


def test_el_header_del_inflight_sale_de_settings():
    tags = tags_de(Settings(TRAEFIK_INFLIGHT_SOURCE_HEADER="X-Otro"))

    assert tags[INFLIGHT_SOURCE] == "X-Otro"


def test_sin_header_el_inflight_queda_con_el_default_de_traefik():
    tags = tags_de(Settings(TRAEFIK_INFLIGHT_SOURCE_HEADER=""))

    assert not any("sourcecriterion" in clave for clave in tags)


@pytest.mark.parametrize("valor", ["X-Forwarded-Server,otro", "a=b", "con espacio"])
def test_el_header_del_inflight_no_acepta_valores_que_rompan_el_tag(valor):
    with pytest.raises(ValueError):
        Settings(TRAEFIK_INFLIGHT_SOURCE_HEADER=valor)


@pytest.mark.parametrize(
    "opcion", ["status", "retrynonidempotentmethod", "maxrequestbodybytes", "timeout"]
)
def test_el_retry_solo_reintenta_errores_de_red(opcion):
    """Decisión de la issue #55. Sin ``status``, el retry de Traefik v3.7 solo
    actúa ante un error de red antes de mandar el pedido, también en POST, y no
    guarda el body en memoria. Con ``status`` reintentaría respuestas (el 503
    del backpressure, o un 504 que rompe el presupuesto de timeouts), y
    ``retryNonIdempotentMethod`` y ``maxRequestBodyBytes`` solo tienen efecto
    junto con ``status``."""
    prefijo = "traefik.http.middlewares.extract-retry.retry."

    retry = {clave for clave in tags_de(Settings()) if clave.startswith(prefijo)}

    assert retry == {prefijo + "attempts"}
    assert prefijo + opcion not in retry


def test_un_middleware_declarado_en_el_router_tiene_su_definicion():
    """Si el router nombra un middleware que nadie define, Traefik ignora la ruta."""
    tags = tags_de(Settings())
    nombrados = tags["traefik.http.routers.extract.middlewares"].split(",")

    for nombre in nombrados:
        prefijo = f"traefik.http.middlewares.{nombre}."
        assert any(clave.startswith(prefijo) for clave in tags), nombre


def test_el_503_del_backpressure_no_cuenta_para_el_circuit_breaker():
    """Decisión de la issue #32, documentada en el README.

    En Traefik el rango de ``ResponseCodeRatio`` es ``[from, to)``, así que
    ``(500, 503, 0, 600)`` cuenta 500, 501 y 502 y deja afuera el 503. El 503
    del backpressure es la respuesta sana de una réplica llena: si abriera el
    circuito, cuanto mejor descarta carga el servicio más probable que Traefik
    lo corte del todo.
    """
    default = Settings.model_fields["TRAEFIK_CB_EXPRESSION"].default

    assert "ResponseCodeRatio(500, 503, 0, 600)" in default
    assert "ResponseCodeRatio(500, 600" not in default


def test_el_payload_del_registro_lleva_los_tags():
    registration = Registration(
        service_id="abc123", address="172.20.0.3", port=8001, tags=("traefik.enable=true",)
    )

    assert registration.payload()["Tags"] == ["traefik.enable=true"]


def test_sin_tags_el_payload_trae_la_lista_vacia():
    payload = Registration(service_id="abc123", address="172.20.0.3", port=8001).payload()

    assert payload["Tags"] == []


def test_build_registrar_pasa_los_tags_al_registro():
    registrar = build_registrar(
        consul_addr="http://127.0.0.1:8500",
        token="",
        port=8001,
        tags=("traefik.enable=true", "otro=1"),
    )

    assert registrar is not None
    assert registrar.registration.tags == ("traefik.enable=true", "otro=1")


def test_la_app_registra_la_replica_con_los_tags_de_traefik(monkeypatch):
    """Que traefik_tags() exista no sirve si el lifespan no lo usa."""
    recibido = {}

    def fake_build_registrar(**kwargs):
        recibido.update(kwargs)
        return FakeRegistrar()

    monkeypatch.setattr("extractor.main.build_registrar", fake_build_registrar)
    settings = Settings(CONSUL_ENABLED=True, LOG_LEVEL="WARNING", TRAEFIK_INFLIGHT=99)

    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200

    assert recibido["tags"] == traefik_tags(settings)
    assert "traefik.http.middlewares.extract-inflight.inflightreq.amount=99" in recibido["tags"]


def test_el_check_de_consul_mira_la_readiness_y_no_la_liveness():
    """Issue #35: si el hilo de extracción se traba, Traefik tiene que sacar a
    la réplica del balanceo. Reiniciar el contenedor es otra decisión, y la
    toma el HEALTHCHECK de Docker, que sigue en /health."""
    check = Registration(service_id="abc", address="172.20.0.4", port=8001).payload()["Check"]

    assert READINESS_PATH == "/health/ready"
    assert check["HTTP"].endswith(READINESS_PATH)
