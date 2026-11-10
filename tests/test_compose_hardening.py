"""Hardening del contenedor en runtime (issue #71).

Regresión sobre ``docker-compose.yml``: que ninguna de las medidas desaparezca
sin que se note. Que el servicio funcione con ellas se comprueba levantando el
stack (``docker compose up -d --wait``); acá solo se mira que estén declaradas.
"""

import re
from pathlib import Path

import pytest

COMPOSE = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()


@pytest.mark.parametrize(
    "patron",
    [
        pytest.param(r"^\s*read_only:\s*true\s*$", id="read_only"),
        pytest.param(r"^\s*-\s*/tmp:size=\d+m", id="tmpfs-tmp-acotado"),
        pytest.param(r"^\s*cap_drop:\s*\n\s*-\s*ALL\s*$", id="cap_drop-all"),
        pytest.param(r"^\s*-\s*no-new-privileges:true\s*$", id="no-new-privileges"),
        pytest.param(r"^\s*pids:\s*\d+\s*$", id="pids"),
    ],
)
def test_el_compose_declara_el_hardening(patron):
    assert re.search(patron, COMPOSE, re.MULTILINE)
