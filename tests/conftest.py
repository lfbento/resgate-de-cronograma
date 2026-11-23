"""Configuração e fixtures compartilhadas para os testes do resgate_cronograma."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


@pytest.fixture
def repo_dir() -> Path:
    """Diretório raiz do código-fonte e amostras."""
    return ROOT_DIR


@pytest.fixture
def amostra_xer(repo_dir: Path) -> Path:
    return repo_dir / "amostra.xer"


@pytest.fixture
def amostra_xml(repo_dir: Path) -> Path:
    return repo_dir / "amostra_project.xml"


@pytest.fixture
def demo_cronograma_csv(repo_dir: Path) -> Path:
    return repo_dir / "demo_cronograma.csv"


@pytest.fixture
def demo_mc_csv(repo_dir: Path) -> Path:
    return repo_dir / "demo_mc.csv"
