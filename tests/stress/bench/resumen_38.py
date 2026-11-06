"""Resumen de la matriz de la issue #38 a partir de los resultados guardados.

Para cada corrida (una carpeta ``<n>-<config>-<herramienta>`` de
``run-38.sh`` o ``run-38-documents.sh``) arma el desglose de los no-200 por
motivo con cuatro fuentes y las concilia:

* el cliente (Vegeta: código y body de cada pedido; k6: código de cada pedido);
* el access log JSON de Traefik (``DownstreamStatus``, ``OriginStatus``,
  ``OriginContentSize``, ``ServiceURL``, ``OriginDuration``);
* el log de extract (línea de acceso de uvicorn y ``WARNING`` del deadline,
  con ``request_id``, que Traefik guarda como ``origin_X-Request-Id``);
* el delta de ``/metrics`` de Traefik (``traefik_entrypoint_requests_total`` y
  ``traefik_service_requests_total``).

Después agrupa por configuración y herramienta (media, mínimo y máximo) y
escribe ``resumen.md`` y ``resumen.json`` en la carpeta de la fecha.

Uso:

    uv run python tests/stress/bench/resumen_38.py tests/stress/results/38/2026-11-06

Solo usa la biblioteca estándar. Cómo se reconoce cada motivo (verificado con
pedidos de prueba, ver informe §4): los 503 de extract llegan con
``OriginStatus`` 503 y su body (``detail``) dice el motivo, y el tamaño del body
alcanza para distinguirlos en el access log (cupo 66 B, cola 70 B); el deadline
además deja un ``WARNING`` en el log de extract. Lo que responde Traefik sin
llamar a una réplica (CB abierto, ``429`` del inflight, ``404`` sin ruta) llega
con ``OriginStatus`` 0, y el ``504`` del ``responseHeaderTimeout``, con
``OriginStatus`` 504 y ``OriginDuration`` de unos 25 s.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

# Tamaño en bytes del body JSON de cada 503 de extract (FastAPI, UTF-8).
SIZE_CUPO = 66
SIZE_COLA = 70
# El responseHeaderTimeout de infra es 25 s; un 504 más corto es otro corte
# (dialTimeout de 2 s).
HEADER_TIMEOUT_MIN_S = 24.0

MOTIVOS = [
    "200",
    "503_cupo",
    "503_cola",
    "503_deadline",
    "503_extract_otro",
    "503_cb",
    "429_inflight",
    "504_traefik",
    "504_otro",
    "404_sin_ruta",
    "0_timeout_cliente",
    "0_conexion",
    "otros",
]

METRIC_RE = re.compile(r"^(?P<name>\w+)\{(?P<labels>[^}]*)\}\s+(?P<value>\S+)$")
LABEL_RE = re.compile(r'(\w+)="([^"]*)"')
ACCESS_RE = re.compile(r'"(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[\d.]+" (?P<code>\d{3})')


def read_vars(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    return values


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def metrics(path: Path) -> dict[tuple, float]:
    """``{(nombre, (etiquetas ordenadas)): valor}`` de un snapshot de /metrics."""
    out: dict[tuple, float] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        match = METRIC_RE.match(line)
        if match:
            labels = tuple(sorted(LABEL_RE.findall(match["labels"])))
            out[(match["name"], labels)] = float(match["value"])
    return out


def metric_delta(run: Path, name: str, **want: str) -> Counter:
    """Delta por código de una métrica, filtrada por etiquetas."""
    before, after = metrics(run / "metrics-before.prom"), metrics(run / "metrics-after.prom")
    delta: Counter = Counter()
    for (metric, labels), value in after.items():
        if metric != name:
            continue
        labels_d = dict(labels)
        if any(labels_d.get(k) != v for k, v in want.items()):
            continue
        diff = value - before.get((metric, labels), 0.0)
        if diff:
            delta[labels_d.get("code", "?")] += int(round(diff))
    return delta


# --- Traefik ------------------------------------------------------------------


def motivo_traefik(entry: dict, deadline_ids: set[str]) -> str:
    down = entry.get("DownstreamStatus", 0)
    origin = entry.get("OriginStatus", 0)
    if down == 200:
        return "200"
    if down == 503 and origin == 503:
        if entry.get("origin_X-Request-Id") in deadline_ids:
            return "503_deadline"
        size = entry.get("OriginContentSize", 0)
        if size == SIZE_CUPO:
            return "503_cupo"
        if size == SIZE_COLA:
            return "503_cola"
        return "503_extract_otro"
    if down == 503 and origin == 0:
        return "503_cb"
    if down == 429 and origin == 0:
        return "429_inflight"
    if down == 504 and origin == 504:
        if entry.get("OriginDuration", 0) / 1e9 >= HEADER_TIMEOUT_MIN_S:
            return "504_traefik"
        return "504_otro"
    if down == 404 and origin == 0:
        return "404_sin_ruta"
    if down == 499:
        return "0_timeout_cliente"
    return "otros"


# --- Cliente ------------------------------------------------------------------


def motivo_vegeta(row: dict) -> str:
    code = row.get("code", 0)
    body = row.get("body") or ""
    error = row.get("error") or ""
    if code == 200:
        return "200"
    if code == 503:
        if "no hay cupo" in body:
            return "503_cupo"
        if "en la cola" in body:
            return "503_cola"
        if "no terminó en" in body:
            return "503_deadline"
        if body.strip() == "Service Unavailable":
            return "503_cb"
        return "503_extract_otro"
    if code == 429:
        return "429_inflight"
    if code == 504:
        return "504_traefik"
    if code == 404:
        return "404_sin_ruta"
    if code == 0:
        if re.search(r"Timeout|deadline exceeded|timeout", error):
            return "0_timeout_cliente"
        return "0_conexion"
    return "otros"


def motivo_k6(row: dict) -> str:
    code = row.get("code", 0)
    # Sin body: los 503 se separan con el access log de Traefik.
    mapping = {200: "200", 503: "503", 429: "429_inflight", 504: "504_traefik", 404: "404_sin_ruta"}
    if code in mapping:
        return mapping[code]
    if code == 0:
        # 1050: request timeout de k6.
        return "0_timeout_cliente" if str(row.get("error_code")) == "1050" else "0_conexion"
    return "otros"


def percentiles_200(latencias: list[float]) -> dict:
    """P50 y P95 (s) solo de los 200: los rechazos rápidos no las achican."""
    if len(latencias) < 2:
        return {}
    cuantiles = statistics.quantiles(latencias, n=100, method="inclusive")
    return {"p50_200_s": cuantiles[49], "p95_200_s": cuantiles[94]}


def k6_duracion(report: Path) -> float:
    if not report.exists():
        return 0.0
    tiempos = re.findall(r"running \((\d+)m([\d.]+)s\)", report.read_text(errors="replace"))
    if not tiempos:
        return 0.0
    minutos, segundos = tiempos[-1]
    return int(minutos) * 60 + float(segundos)


# --- Una corrida --------------------------------------------------------------


def resumir_corrida(run: Path) -> dict:
    vars_ = read_vars(run / "run.vars")
    tool = vars_.get("TOOL", run.name.rsplit("-", 1)[-1])
    if tool == "documents":
        return resumir_documents(run, vars_)
    config = vars_.get("CONFIG", "?")
    path = vars_.get("TARGET_PATH", "/extract")

    # Log de extract: líneas de acceso y deadlines, por request_id.
    extract = read_jsonl(run / "extract.jsonl")
    deadline_ids = {
        r.get("request_id") for r in extract if "stopped by deadline" in str(r.get("message", ""))
    }
    extract_codes: Counter = Counter()
    for r in extract:
        if r.get("logger") != "uvicorn.access":
            continue
        m = ACCESS_RE.search(str(r.get("message", "")))
        if m and m["method"] == "POST" and m["path"].startswith("/extract"):
            extract_codes[m["code"]] += 1

    # Access log de Traefik.
    access_all = [
        e for e in read_jsonl(run / "traefik-access.jsonl") if e.get("entryPointName") == "web"
    ]
    access = [
        e
        for e in access_all
        if str(e.get("RequestPath", "")).startswith(path) and e.get("RequestMethod") == "POST"
    ]
    traefik = Counter(motivo_traefik(e, deadline_ids) for e in access)
    traefik_down = Counter(str(e.get("DownstreamStatus")) for e in access_all)
    traefik_service = Counter(
        str(e.get("OriginStatus"))
        for e in access_all
        if e.get("ServiceURL") and str(e.get("RequestPath", "")).startswith("/extract")
    )
    retries = sum(e.get("RetryAttempts", 0) for e in access)
    otros_paths = Counter(
        e.get("RequestPath")
        for e in access_all
        if not str(e.get("RequestPath", "")).startswith(path)
    )

    # Cliente.
    client: Counter = Counter()
    resumen_cliente: dict = {}
    if tool == "vegeta":
        rows = read_jsonl(run / "vegeta-requests.jsonl")
        client = Counter(motivo_vegeta(r) for r in rows)
        report = json.loads((run / "vegeta-report.json").read_text())
        resumen_cliente = {
            "solicitudes": report["requests"],
            "exito_pct": report["success"] * 100,
            "throughput_efectivo": report["throughput"],
            "p50_s": report["latencies"]["50th"] / 1e9,
            "p95_s": report["latencies"]["95th"] / 1e9,
            **percentiles_200([r["latency"] / 1e9 for r in rows if r.get("code") == 200]),
        }
    elif tool == "k6":
        rows = read_jsonl(run / "k6-requests.jsonl")
        client = Counter(motivo_k6(r) for r in rows)
        summary = json.loads((run / "k6-summary.json").read_text())
        mets = summary["metrics"]
        dur = mets["http_req_duration"]
        # k6 2.2.0 no pone la duración en --summary-export ("state" vacío): sale
        # de la última línea "running (0m40.2s), ..." del reporte.
        duracion_s = k6_duracion(run / "k6-report.txt")
        total = mets["http_reqs"]["count"]
        ok = client["200"]
        resumen_cliente = {
            "solicitudes": total,
            "exito_pct": 100 * ok / total if total else 0.0,
            "req_s_todas": mets["http_reqs"]["rate"],
            "throughput_efectivo": ok / duracion_s if duracion_s else 0.0,
            "duracion_s": duracion_s,
            "p50_s": dur["med"] / 1000,
            "p95_s": dur["p(95)"] / 1000,
            **percentiles_200([r["duration_ms"] / 1000 for r in rows if r.get("code") == 200]),
        }

    # Desglose final: el motivo de cada no-200 sale del access log de Traefik
    # (por pedido, para las dos herramientas); el código 0 solo lo ve el cliente.
    desglose = {m: traefik.get(m, 0) for m in MOTIVOS}
    desglose["0_timeout_cliente"] = client.get("0_timeout_cliente", 0)
    # Un error de conexión del cliente puede haber llegado a Traefik: con un
    # "broken pipe", Traefik respondió 429 y cerró mientras el cliente todavía
    # subía el PDF, y ese pedido ya cuenta como 429. Acá van solo los que
    # Traefik nunca vio.
    desglose["0_conexion"] = max(0, sum(client.values()) - sum(traefik.values()))

    ep = metric_delta(run, "traefik_entrypoint_requests_total", entrypoint="web", method="POST")
    svc = metric_delta(
        run, "traefik_service_requests_total", service="extract@consulcatalog", method="POST"
    )

    controles = conciliar(
        tool,
        client,
        traefik,
        traefik_down,
        traefik_service,
        ep,
        svc,
        extract_codes,
        deadline_ids,
        access,
    )

    return {
        "corrida": run.name,
        "config": config,
        "herramienta": tool,
        "imagen": vars_.get("EXTRACT_IMAGE_TAG"),
        "inicio": read_vars(run / "times.env").get("START"),
        "fin": read_vars(run / "times.env").get("END"),
        "cliente": resumen_cliente,
        "desglose": desglose,
        "fuentes": {
            "cliente": dict(client),
            "traefik": dict(traefik),
            "traefik_downstream_web": dict(traefik_down),
            "traefik_retry_attempts": retries,
            "traefik_otros_paths": dict(otros_paths),
            "metrics_entrypoint_web_post": dict(ep),
            "metrics_service_extract_post": dict(svc),
            "extract_log_post": dict(extract_codes),
            "extract_log_deadlines": len(deadline_ids),
        },
        "controles": controles,
    }


def conciliar(
    tool,
    client,
    traefik,
    traefik_down,
    traefik_service,
    ep,
    svc,
    extract_codes,
    deadline_ids,
    access,
) -> list[dict]:
    """Comparaciones entre fuentes. Cada una dice si cierra y, si no, la diferencia."""
    checks = []

    def check(nombre: str, a: int, b: int, nota: str = "") -> None:
        checks.append({"control": nombre, "a": a, "b": b, "cierra": a == b, "nota": nota})

    n_client = sum(client.values())
    n_traefik = sum(traefik.values())
    sin_resp = max(0, n_client - n_traefik)
    check(
        "pedidos: cliente (sin los que Traefik nunca vio) vs access log",
        n_client - sin_resp,
        n_traefik,
        "un timeout del cliente aparece en Traefik con el código que terminó",
    )
    check("200: cliente vs access log", client.get("200", 0), traefik.get("200", 0))
    for code in ("200", "429", "503", "504", "404"):
        check(
            f"{code}: access log (entrypoint web) vs metrics entrypoint",
            int(traefik_down.get(code, 0)),
            int(ep.get(code, 0)),
        )
    for code in ("200", "503", "504"):
        check(
            f"{code}: access log con upstream vs metrics service",
            int(traefik_service.get(code, 0)),
            int(svc.get(code, 0)),
        )
    check(
        "503 del CB: access log vs metrics (entrypoint − service)",
        traefik.get("503_cb", 0),
        int(ep.get("503", 0)) - int(svc.get("503", 0)),
    )
    extract_503 = (
        traefik.get("503_cupo", 0)
        + traefik.get("503_cola", 0)
        + traefik.get("503_deadline", 0)
        + traefik.get("503_extract_otro", 0)
    )
    check("503 de extract: access log vs log de extract", extract_503, extract_codes.get("503", 0))
    check(
        "503 por deadline: access log vs WARNING en extract",
        traefik.get("503_deadline", 0),
        len(deadline_ids),
    )
    # Los 200 que extract terminó cuando el cliente ya no esperaba: el pedido
    # salió por 504 de Traefik o por timeout del cliente.
    tardios = extract_codes.get("200", 0) - traefik.get("200", 0)
    checks.append(
        {
            "control": "200 tardíos (log de extract − access log)",
            "a": tardios,
            "b": traefik.get("504_traefik", 0) + traefik.get("0_timeout_cliente", 0),
            "cierra": tardios
            <= traefik.get("504_traefik", 0) + traefik.get("0_timeout_cliente", 0),
            "nota": "a ≤ b: cada 200 tardío corresponde a un 504 o a un timeout",
        }
    )
    if tool == "vegeta":
        for motivo in (
            "503_cupo",
            "503_cola",
            "503_deadline",
            "503_cb",
            "429_inflight",
            "504_traefik",
            "404_sin_ruta",
        ):
            check(
                f"{motivo}: body en Vegeta vs access log",
                client.get(motivo, 0),
                traefik.get(motivo, 0),
            )
    else:
        check(
            "503: k6 vs access log",
            client.get("503", 0),
            sum(
                traefik.get(m, 0)
                for m in ("503_cupo", "503_cola", "503_deadline", "503_extract_otro", "503_cb")
            ),
        )
        # Los código 0 que Traefik sí vio son broken pipe sobre un 429 (ver desglose).
        vistos = client.get("0_conexion", 0) - sin_resp
        check(
            "429_inflight: k6 (+ código 0 que Traefik vio) vs access log",
            client.get("429_inflight", 0) + vistos,
            traefik.get("429_inflight", 0),
            f"{vistos} broken pipe: Traefik respondió 429 mientras k6 subía el body",
        )
        for motivo in ("504_traefik", "404_sin_ruta"):
            check(f"{motivo}: k6 vs access log", client.get(motivo, 0), traefik.get(motivo, 0))
    return checks


# --- Camino real: POST /documents ---------------------------------------------

# Tamaño del body JSON de cada error de documents-service (``{"detail": ...}``).
DOCS_SIZE = {67: "503_documents_cupo", 48: "503_extract_no_disponible", 51: "504_extract"}
MOTIVOS_DOCS = [
    "201_creado",
    "200_duplicado",
    "503_documents_cupo",
    "503_extract_no_disponible",
    "504_extract",
    "0_timeout_cliente",
    "0_conexion",
    "otros",
]


def motivo_documents_cliente(row: dict) -> str:
    code, body = row.get("code", 0), row.get("body") or ""
    if code == 201:
        return "201_creado"
    if code == 200:
        return "200_duplicado" if row.get("duplicate") else "otros"
    if code == 503 and "no hay cupo para recibir" in body:
        return "503_documents_cupo"
    if code == 503 and "extract-service no está disponible" in body:
        return "503_extract_no_disponible"
    if code == 504 and "no respondió a tiempo" in body:
        return "504_extract"
    if code == 0:
        error = row.get("error") or ""
        if re.search(r"Timeout|deadline exceeded|timeout", error):
            return "0_timeout_cliente"
        return "0_conexion"
    return "otros"


def motivo_documents_traefik(entry: dict) -> str:
    down = entry.get("DownstreamStatus", 0)
    if down == 201:
        return "201_creado"
    if down == 200:
        return "200_duplicado"
    if down in (503, 504) and entry.get("OriginStatus") == down:
        return DOCS_SIZE.get(entry.get("OriginContentSize"), "otros")
    if down == 499:
        return "0_timeout_cliente"
    return "otros"


def resumir_documents(run: Path, vars_: dict) -> dict:
    extract = read_jsonl(run / "extract.jsonl")
    deadline_ids = {
        r.get("request_id") for r in extract if "stopped by deadline" in str(r.get("message", ""))
    }
    extract_codes: Counter = Counter()
    for r in extract:
        m = (
            ACCESS_RE.search(str(r.get("message", "")))
            if r.get("logger") == "uvicorn.access"
            else None
        )
        if m and m["method"] == "POST" and m["path"].startswith("/extract"):
            extract_codes[m["code"]] += 1
    documents = read_jsonl(run / "documents.jsonl")
    # documents puede loguear en JSON o en texto (depende de su LOG_FORMAT):
    # se busca el mensaje en los dos.
    eventos = (
        "Documento almacenado",
        "Documento duplicado",
        "Duplicado detectado por índice único",
    )
    docs_log: Counter = Counter()
    for r in documents:
        linea = str(r.get("message") or r.get("raw") or "")
        for evento in eventos:
            if evento in linea:
                docs_log[evento] += 1

    access_all = [
        e for e in read_jsonl(run / "traefik-access.jsonl") if e.get("entryPointName") == "web"
    ]
    externos = [e for e in access_all if str(e.get("RequestPath", "")).startswith("/documents")]
    internos = [e for e in access_all if str(e.get("RequestPath", "")).startswith("/extract")]
    afuera = Counter(motivo_documents_traefik(e) for e in externos)
    adentro = Counter(motivo_traefik(e, deadline_ids) for e in internos)

    # Cada POST /documents lleva su X-Request-ID a extract: el motivo del 503 o
    # 504 que devolvió documents es el de su /extract interno.
    interno_por_id = {e.get("request_X-Request-Id"): e for e in internos}
    cruce: dict[str, Counter] = {}
    for e in externos:
        motivo = motivo_documents_traefik(e)
        interno = interno_por_id.get(e.get("downstream_X-Request-Id"))
        detalle = motivo_traefik(interno, deadline_ids) if interno else "sin_llamada_a_extract"
        cruce.setdefault(motivo, Counter())[detalle] += 1

    rows = read_jsonl(run / "vegeta-requests.jsonl")
    client = Counter(motivo_documents_cliente(r) for r in rows)
    report = json.loads((run / "vegeta-report.json").read_text())
    ok = [r["latency"] / 1e9 for r in rows if r.get("code") == 201]

    ep = metric_delta(run, "traefik_entrypoint_requests_total", entrypoint="web", method="POST")
    svc_docs = metric_delta(
        run, "traefik_service_requests_total", service="documents@consulcatalog", method="POST"
    )
    svc_extract = metric_delta(
        run, "traefik_service_requests_total", service="extract@consulcatalog", method="POST"
    )

    checks = []

    def check(nombre: str, a: int, b: int, nota: str = "") -> None:
        checks.append({"control": nombre, "a": a, "b": b, "cierra": a == b, "nota": nota})

    for motivo in MOTIVOS_DOCS[:5]:
        check(f"{motivo}: Vegeta vs access log /documents", client[motivo], afuera[motivo])
    check(
        "201: Vegeta vs 'Documento almacenado' en documents",
        client["201_creado"],
        docs_log["Documento almacenado"],
    )
    check(
        "duplicados en el log de documents (tiene que ser 0)",
        docs_log["Documento duplicado"] + docs_log["Duplicado detectado por índice único"],
        0,
    )
    down = Counter(str(e.get("DownstreamStatus")) for e in access_all)
    for code in sorted(set(down) | set(ep)):
        check(
            f"{code}: access log (web, /documents + /extract) vs metrics entrypoint",
            down[code],
            ep[code],
        )
    check(
        "/extract internos: 200 en el access log vs log de extract",
        adentro["200"],
        extract_codes["200"],
        "más 200 en extract = extracciones que terminaron después del 504",
    )
    check(
        "503 de extract: access log vs log de extract",
        sum(adentro[m] for m in ("503_cupo", "503_cola", "503_deadline", "503_extract_otro")),
        extract_codes["503"],
    )
    check(
        "201 de documents vs /extract internos con 200",
        afuera["201_creado"],
        cruce.get("201_creado", Counter())["200"],
    )

    return {
        "corrida": run.name,
        "config": vars_.get("CONFIG"),
        "herramienta": "documents",
        "imagen": vars_.get("EXTRACT_IMAGE_TAG"),
        "inicio": read_vars(run / "times.env").get("START"),
        "fin": read_vars(run / "times.env").get("END"),
        "cliente": {
            "solicitudes": report["requests"],
            "exito_pct": 100 * client["201_creado"] / report["requests"],
            "throughput_efectivo": len(ok) / (report["duration"] + report["wait"]) * 1e9,
            "p50_s": report["latencies"]["50th"] / 1e9,
            "p95_s": report["latencies"]["95th"] / 1e9,
            **percentiles_200(ok),
        },
        "desglose": {m: client[m] for m in MOTIVOS_DOCS},
        "extract_interno": dict(adentro),
        "cruce_documents_extract": {k: dict(v) for k, v in cruce.items()},
        "fuentes": {
            "cliente": dict(client),
            "traefik_documents": dict(afuera),
            "traefik_extract_interno": dict(adentro),
            "metrics_entrypoint_web_post": dict(ep),
            "metrics_service_documents_post": dict(svc_docs),
            "metrics_service_extract_post": dict(svc_extract),
            "extract_log_post": dict(extract_codes),
            "documents_log": dict(docs_log),
        },
        "controles": checks,
    }


def markdown_documents(c: dict) -> list[str]:
    out = [f"## Camino real: `POST /documents` ({c['corrida']})", ""]
    cl = c["cliente"]
    out.append(
        f"{cl['solicitudes']} pedidos; éxito (201) {cl['exito_pct']:.2f} %; "
        f"201/s {cl['throughput_efectivo']:.2f}; P50/P95 de los 201 "
        f"{cl.get('p50_200_s', 0):.2f} / {cl.get('p95_200_s', 0):.2f} s."
    )
    out += ["", "| Motivo (cliente) | Pedidos |", "|---|---|"]
    out += [f"| {m} | {c['desglose'][m]} |" for m in MOTIVOS_DOCS]
    out += [
        "",
        "| Respuesta de documents | Motivo del /extract interno | Pedidos |",
        "|---|---|---|",
    ]
    for motivo, detalle in c["cruce_documents_extract"].items():
        for d, n in sorted(detalle.items(), key=lambda kv: -kv[1]):
            out.append(f"| {motivo} | {d} | {n} |")
    fallan = [k for k in c["controles"] if not k["cierra"]]
    out += ["", "Controles: " + ("todos cierran." if not fallan else "")]
    for k in fallan:
        out.append(f"- no cierra: {k['control']}: {k['a']} vs {k['b']}")
    out.append("")
    return out


# --- Agregado -----------------------------------------------------------------

COLUMNAS = [
    ("exito_pct", "Éxito (%)", "cliente"),
    ("throughput_efectivo", "Throughput efectivo (200/s)", "cliente"),
    ("req_s_todas", "req/s (todas)", "cliente"),
    ("p50_s", "P50 (s)", "cliente"),
    ("p95_s", "P95 (s)", "cliente"),
    ("p50_200_s", "P50 de los 200 (s)", "cliente"),
    ("p95_200_s", "P95 de los 200 (s)", "cliente"),
] + [(m, m, "desglose") for m in MOTIVOS if m != "200"]


def agregar(corridas: list[dict]) -> list[dict]:
    grupos: dict[tuple, list[dict]] = {}
    for c in corridas:
        grupos.setdefault((c["config"], c["herramienta"]), []).append(c)
    orden = {"A": 0, "C": 1, "C+CB": 2}
    filas = []
    for (config, tool), runs in sorted(
        grupos.items(), key=lambda kv: (kv[0][1], orden.get(kv[0][0], 9))
    ):
        fila = {
            "config": config,
            "herramienta": tool,
            "n": len(runs),
            "corridas": [r["corrida"] for r in runs],
        }
        for key, _, fuente in COLUMNAS:
            valores = [r[fuente][key] for r in runs if key in r[fuente]]
            if valores:
                fila[key] = {
                    "media": statistics.fmean(valores),
                    "min": min(valores),
                    "max": max(valores),
                }
        filas.append(fila)
    return filas


def fmt(stat: dict | None, dec: int) -> str:
    if not stat:
        return "N/D"
    m, lo, hi = stat["media"], stat["min"], stat["max"]
    return f"{m:.{dec}f} ({lo:.{dec}f} a {hi:.{dec}f})".replace(".", ",")


def markdown(corridas: list[dict], filas: list[dict]) -> str:
    out = [
        "# Resumen de la matriz (issue #38)",
        "",
        "Generado por `tests/stress/bench/resumen_38.py`. Media (mínimo a máximo) por "
        "configuración y herramienta.",
        "",
    ]
    for tool in sorted({f["herramienta"] for f in filas}):
        sub = [f for f in filas if f["herramienta"] == tool]
        cols = [c for c in COLUMNAS if any(c[0] in f for f in sub)]
        out.append(f"## {tool}")
        out.append("")
        out.append("| Config | n | " + " | ".join(c[1] for c in cols) + " |")
        out.append("|---|---|" + "---|" * len(cols))
        for f in sub:
            celdas = []
            for key, _, fuente in cols:
                dec = 0 if fuente == "desglose" else 2
                celdas.append(fmt(f.get(key), dec))
            out.append(f"| {f['config']} | {f['n']} | " + " | ".join(celdas) + " |")
        out.append("")

    out += [
        "## Desglose por corrida",
        "",
        "| Corrida | Inicio (UTC) | Éxito (%) | " + " | ".join(MOTIVOS) + " | Controles |",
        "|---|---|---|" + "---|" * len(MOTIVOS) + "---|",
    ]
    for c in corridas:
        fallan = [k["control"] for k in c["controles"] if not k["cierra"]]
        estado = "cierran" if not fallan else f"{len(fallan)} no cierran"
        exito = f"{c['cliente'].get('exito_pct', 0):.2f}".replace(".", ",")
        out.append(
            f"| {c['corrida']} | {c['inicio'] or ''} | {exito} | "
            + " | ".join(str(c["desglose"][m]) for m in MOTIVOS)
            + f" | {estado} |"
        )
    out.append("")

    fallas = [(c["corrida"], k) for c in corridas for k in c["controles"] if not k["cierra"]]
    out += ["## Controles que no cierran", ""]
    if not fallas:
        out.append("Todos los controles cierran.")
    for corrida, k in fallas:
        nota = f" ({k['nota']})" if k["nota"] else ""
        out.append(f"- `{corrida}`: {k['control']}: {k['a']} vs {k['b']}{nota}")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("carpeta", type=Path, help="tests/stress/results/38/<fecha>")
    args = parser.parse_args(argv)

    runs = sorted(p for p in args.carpeta.iterdir() if p.is_dir() and (p / "run.vars").exists())
    if not runs:
        print(f"no hay corridas en {args.carpeta}", file=sys.stderr)
        return 1
    corridas, documentos = [], []
    for run in runs:
        resumen = resumir_corrida(run)
        (run / "desglose.json").write_text(json.dumps(resumen, indent=2, ensure_ascii=False))
        (documentos if resumen["herramienta"] == "documents" else corridas).append(resumen)
    filas = agregar(corridas)
    (args.carpeta / "resumen.json").write_text(
        json.dumps(
            {"filas": filas, "corridas": corridas, "documents": documentos},
            indent=2,
            ensure_ascii=False,
        )
    )
    texto = markdown(corridas, filas)
    for doc in documentos:
        texto += "\n" + "\n".join(markdown_documents(doc))
    (args.carpeta / "resumen.md").write_text(texto)
    print(texto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
