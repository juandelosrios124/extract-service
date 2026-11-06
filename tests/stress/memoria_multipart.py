"""Prueba de memoria: N pedidos multipart grandes en paralelo contra UNA replica.

Reproduce el escenario de la issue #36: con `MAX_CONCURRENT_UPLOADS=8` y cuerpos
cercanos a `MAX_UPLOAD_SIZE`, la replica tiene que sostener los 8 pedidos dentro
de su tope de 1 GiB. El script dispara los pedidos y, en paralelo, muestrea
`docker stats` para quedarse con el pico de memoria del contenedor.

Desde la #33 el punto de entrada es Traefik, que vive en el repo de infra, asi
que hay que tenerlo levantado. Despues, el stack con UNA sola replica:

    EXTRACT_REPLICAS=1 docker compose up --build -d --wait
    uv run python tests/stress/memoria_multipart.py

En PowerShell:

    $env:EXTRACT_REPLICAS="1"; docker compose up --build -d --wait
    uv run python tests/stress/memoria_multipart.py

El PDF grande se genera una sola vez en tests/stress/pdfs_provisorios/ (ignorado
por git) a partir del PDF de 8,9 MB de la catedra.
"""

import argparse
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx2 as httpx  # el repo usa httpx2, no httpx
import pymupdf

RAIZ = Path(__file__).resolve().parents[2]
FUENTE = RAIZ / "tests/stress/pdfs/Essential-Kanban-Condensed-Spanish.pdf"
DESTINO = RAIZ / "tests/stress/pdfs_provisorios/grande.pdf"
BOUNDARY = "----memoria36BoundaryXYZ"


def pdf_grande(destino: Path, mb_objetivo: int) -> bytes:
    """Genera (y cachea) el PDF mas grande posible por debajo de ``mb_objetivo`` MiB."""
    tope = mb_objetivo * 1024 * 1024
    if destino.exists() and destino.stat().st_size <= tope:
        return destino.read_bytes()

    destino.parent.mkdir(parents=True, exist_ok=True)
    fuente = pymupdf.open(FUENTE)
    doc = pymupdf.open()
    ultimo = b""

    # Primero copias enteras del PDF fuente (saltos de ~8 MiB) y despues pagina
    # por pagina, para quedar lo mas cerca posible del tope sin pasarlo.
    for ultima_pagina in (fuente.page_count - 1, 0):
        while True:
            antes = doc.page_count
            doc.insert_pdf(fuente, from_page=0, to_page=ultima_pagina)
            actual = doc.tobytes()
            if len(actual) > tope:
                doc.delete_pages(from_page=antes, to_page=doc.page_count - 1)
                break
            ultimo = actual

    if not ultimo:
        raise SystemExit("el PDF fuente ya supera el tamano objetivo")
    destino.write_bytes(ultimo)
    return ultimo


def cuerpo_multipart(pdf: bytes) -> bytes:
    cabecera = (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="grande.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("latin-1")
    cierre = f"\r\n--{BOUNDARY}--\r\n".encode("latin-1")
    return cabecera + pdf + cierre


class MuestreoDocker(threading.Thread):
    """Muestrea `docker stats` hasta que se le pide parar y guarda el pico."""

    def __init__(self, servicio: str, intervalo: float = 0.5):
        super().__init__(daemon=True)
        self.servicio = servicio
        self.intervalo = intervalo
        self.pico_mib = 0.0
        self.muestras = 0
        self._parar = threading.Event()

    def run(self) -> None:
        while not self._parar.is_set():
            try:
                salida = subprocess.run(
                    ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                return
            for linea in salida.splitlines():
                if self.servicio not in linea:
                    continue
                usado = linea.split()[1]  # p. ej. "412.3MiB"
                self.pico_mib = max(self.pico_mib, _a_mib(usado))
                self.muestras += 1
            self._parar.wait(self.intervalo)

    def parar(self) -> None:
        self._parar.set()


def _a_mib(texto: str) -> float:
    unidades = {"B": 1 / 1024 / 1024, "KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0}
    for sufijo, factor in sorted(unidades.items(), key=lambda kv: -len(kv[0])):
        if texto.endswith(sufijo):
            return float(texto[: -len(sufijo)]) * factor
    return 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Traefik, publicado por infra en 127.0.0.1:8080. La IP y no "localhost":
    # el bind es solo IPv4 y localhost puede resolver a ::1.
    parser.add_argument("--url", default="http://127.0.0.1:8080/extract")
    parser.add_argument("--pedidos", type=int, default=8, help="pedidos en paralelo")
    parser.add_argument(
        "--mb",
        type=int,
        default=48,
        help="tamano del PDF en MiB (48 deja margen bajo MAX_UPLOAD_SIZE=50 MiB)",
    )
    parser.add_argument("--servicio", default="extract", help="filtro de nombre en docker stats")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--no-calentar",
        dest="calentar",
        action="store_false",
        help="no hacer el pedido previo de calentamiento",
    )
    args = parser.parse_args()

    pdf = pdf_grande(DESTINO, args.mb)
    cuerpo = cuerpo_multipart(pdf)
    cabeceras = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
    print(f"PDF   {len(pdf) / 1024 / 1024:.1f} MiB  ({DESTINO.relative_to(RAIZ)})")
    print(f"Cuerpo {len(cuerpo) / 1024 / 1024:.1f} MiB x {args.pedidos} pedidos en paralelo")
    print(f"URL   {args.url}")

    def pedido(_i: int):
        inicio = time.monotonic()
        try:
            respuesta = httpx.post(
                args.url, content=cuerpo, headers=cabeceras, timeout=args.timeout
            )
            return respuesta.status_code, time.monotonic() - inicio, len(respuesta.content)
        except httpx.HTTPError as error:
            return type(error).__name__, time.monotonic() - inicio, 0

    # El primer pedido sobre un contenedor recien arrancado tarda bastante mas
    # (MuPDF todavia no reservo sus arenas) y arrastra a los demas por encima de
    # MAX_QUEUE_WAIT, lo que da 503 que no tienen nada que ver con la memoria.
    if args.calentar:
        estado, segundos, _ = pedido(0)
        print(f"calentamiento: {estado} en {segundos:.2f} s")

    monitor = MuestreoDocker(args.servicio)
    monitor.start()

    comienzo = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.pedidos) as pool:
        resultados = list(pool.map(pedido, range(args.pedidos)))
    duracion = time.monotonic() - comienzo

    monitor.parar()
    monitor.join(timeout=25)

    print()
    for i, (estado, segundos, tamano) in enumerate(resultados, 1):
        print(f"  pedido {i}: {estado}  {segundos:6.2f} s  respuesta {tamano} B")

    codigos = [estado for estado, _, _ in resultados]
    print()
    print(f"total           {duracion:.2f} s")
    print(f"200             {codigos.count(200)}/{args.pedidos}")
    print(f"503 (sin cupo)  {codigos.count(503)}")
    print(f"otros           {sum(1 for c in codigos if c not in (200, 503))}")
    if monitor.muestras:
        print(f"pico de memoria {monitor.pico_mib:.1f} MiB  ({monitor.muestras} muestras)")
        print("tope por replica 1024.0 MiB (EXTRACT_MEMORY=1g)")
    else:
        print("pico de memoria: no se pudo leer docker stats")

    fallados = [c for c in codigos if c != 200]
    if fallados:
        raise SystemExit(f"hubo pedidos que no terminaron en 200: {fallados}")


if __name__ == "__main__":
    main()
