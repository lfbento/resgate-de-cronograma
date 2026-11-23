"""Leitor de cronogramas: Primavera P6 (.xer) e MS Project (.xml MSPDI).

Converte qualquer um dos formatos no CSV unificado que alimenta o motor
cpm_pid.py / monte_carlo.py (colunas: ID, Nome, Tipo, Predecessoras,
FolgaTotal, BaselineHH, HHRealizado, HHRestante, HistoricoDesvios,
DurO, DurM, DurP).

Uso:
  python ler_cronograma.py obra.xer  -o saida.csv
  python ler_cronograma.py cronograma.xml -o saida.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from resgate_cronograma.models import HORAS_POR_DIA
from resgate_cronograma.parsers import (
    CHAVES_EXTERNAS,
    atividade_para_linha_csv,
    escrever_csv,
    parse_mspdi as _parse_mspdi_modelos,
    parse_xer as _parse_xer_modelos,
)
from resgate_cronograma.reporting import configurar_stdout_utf8


def parse_xer(caminho: str) -> list[dict]:
    """Lê `.xer` e retorna lista de dicionários no formato CSV unificado."""
    return [atividade_para_linha_csv(a) for a in _parse_xer_modelos(caminho)]


def parse_mspdi(caminho: str) -> list[dict]:
    """Lê `.xml` MSPDI e retorna lista de dicionários no formato CSV unificado."""
    return [atividade_para_linha_csv(a) for a in _parse_mspdi_modelos(caminho)]


def main(argv: list[str] | None = None) -> int:
    configurar_stdout_utf8()
    parser = argparse.ArgumentParser(
        description="Converte cronogramas Primavera P6 (.xer) ou MS Project (.xml MSPDI) para CSV."
    )
    parser.add_argument("arquivo", help="Caminho do arquivo .xer ou .xml MSPDI")
    parser.add_argument("-o", "--output", required=True, help="Caminho do arquivo CSV de saída")
    args = parser.parse_args(argv)

    caminho = Path(args.arquivo)
    ext = caminho.suffix.lower()
    try:
        if ext == ".xer":
            atividades = _parse_xer_modelos(caminho)
        elif ext == ".xml":
            atividades = _parse_mspdi_modelos(caminho)
        else:
            print(
                "ERRO: formato não suportado (use .xer ou .xml MSPDI).",
                file=sys.stderr,
            )
            return 1
        escrever_csv(atividades, args.output)
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERRO ao processar {caminho}: {exc}", file=sys.stderr)
        return 1

    print(f"✓ {len(atividades)} atividades → {args.output}")
    return 0


__all__ = [
    "CHAVES_EXTERNAS",
    "HORAS_POR_DIA",
    "main",
    "parse_mspdi",
    "parse_xer",
]


if __name__ == "__main__":
    raise SystemExit(main())
