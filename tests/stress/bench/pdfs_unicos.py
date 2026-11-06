"""Variantes únicas de los 4 PDFs de la cátedra para medir ``POST /documents``.

documents-service deduplica por checksum: si se manda dos veces el mismo PDF,
desde el segundo responde ``200`` con ``duplicate: true`` sin llamar a extract,
y la medición del camino real deja de medir extract. Este script genera N
variantes, todas con checksum distinto, rotando los 4 PDFs como
``test_carga.txt``.

Cada variante es el PDF original más una **actualización incremental** que solo
cambia la metadata (``/Keywords bench38-<n>``): el contenido y las páginas son
los mismos y el archivo sigue siendo un PDF válido. Con ``--verificar`` se abre
cada variante con PyMuPDF y se comprueba que tiene las mismas páginas que el
original y que todos los checksums son distintos.

Además arma, para cada variante, el cuerpo ``multipart/form-data`` (campo
``file``, como lo pide documents) y un archivo de targets de Vegeta que los
recorre en orden. Todo queda en ``tests/stress/pdfs_provisorios/`` (ignorado
por git): pesa unos 3,4 MB por variante.

Uso:

    uv run python tests/stress/bench/pdfs_unicos.py --cantidad 1500 --verificar
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

import pymupdf

STRESS = Path(__file__).resolve().parents[1]
PDFS = [
    "2020-Scrum-Guide-Spanish-Latin-South-American.pdf",
    "Essential-Kanban-Condensed-Spanish.pdf",
    "Filosofia_Lean.pdf",
    "scrum_manager_historias_usuario.pdf",
]
BOUNDARY = "bench38-boundary"


def variante(original: Path, destino: Path, n: int) -> None:
    """Copia ``original`` y le agrega una actualización incremental de metadata."""
    shutil.copyfile(original, destino)
    with pymupdf.open(destino) as doc:
        metadata = dict(doc.metadata or {})
        metadata["keywords"] = f"bench38-{n}"
        doc.set_metadata(metadata)
        doc.saveIncr()


def multipart(pdf: Path, destino: Path) -> None:
    with destino.open("wb") as out:
        out.write(f"--{BOUNDARY}\r\n".encode())
        disposition = f'form-data; name="file"; filename="{pdf.name}"'
        out.write(f"Content-Disposition: {disposition}\r\n".encode())
        out.write(b"Content-Type: application/pdf\r\n\r\n")
        out.write(pdf.read_bytes())
        out.write(f"\r\n--{BOUNDARY}--\r\n".encode())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cantidad", type=int, default=1500)
    parser.add_argument("--salida", type=Path, default=STRESS / "pdfs_provisorios" / "unicos-38")
    parser.add_argument("--url", default="http://traefik/documents")
    parser.add_argument(
        "--montaje",
        default="/stress/pdfs_provisorios/unicos-38",
        help="ruta de --salida dentro del contenedor de Vegeta",
    )
    parser.add_argument("--verificar", action="store_true")
    args = parser.parse_args(argv)

    pdfs_dir, bodies_dir = args.salida / "pdf", args.salida / "multipart"
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    bodies_dir.mkdir(parents=True, exist_ok=True)
    paginas = {name: pymupdf.open(STRESS / "pdfs" / name).page_count for name in PDFS}

    targets = []
    checksums: dict[str, str] = {}
    for n in range(args.cantidad):
        name = PDFS[n % len(PDFS)]
        pdf = pdfs_dir / f"{n:05d}-{name}"
        variante(STRESS / "pdfs" / name, pdf, n)
        body = bodies_dir / f"{n:05d}.multipart"
        multipart(pdf, body)
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        if digest in checksums:
            print(f"checksum repetido: {pdf} y {checksums[digest]}", file=sys.stderr)
            return 1
        checksums[digest] = pdf.name
        if args.verificar:
            with pymupdf.open(pdf) as doc:
                marca = doc.metadata.get("keywords")
                if doc.page_count != paginas[name] or marca != f"bench38-{n}":
                    print(f"variante inválida: {pdf}", file=sys.stderr)
                    return 1
        targets.append(
            f"POST {args.url}\n"
            f"Content-Type: multipart/form-data; boundary={BOUNDARY}\n"
            f"@{args.montaje}/multipart/{body.name}\n"
        )
    (args.salida / "targets.txt").write_text("\n".join(targets))
    tamano = sum(p.stat().st_size for p in bodies_dir.iterdir()) / 1e9
    print(
        f"{args.cantidad} variantes, {len(checksums)} checksums distintos, "
        f"{tamano:.2f} GB de cuerpos multipart en {args.salida}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
