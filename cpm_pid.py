"""Sistema Prescritivo de Recuperação de Cronogramas — CPM + PID discreto.

Wrapper CLI retrocompatível delegando ao pacote modular `resgate_cronograma`.

Uso:
  python cpm_pid.py cronograma.csv [--ciclo N] [--prazo DIAS] [--custos JSON]
                    [--json out.json] [--html out.html]
  python cpm_pid.py --db obra.db --projeto Obra-A [--ciclo N] [--prazo DIAS]
"""
from __future__ import annotations

import argparse
import sys
from typing import Sequence

from resgate_cronograma.cost import avaliar_plano_custo_cpm, carregar_parametros_custo
from resgate_cronograma.cycle_store import resolver_atividades_cli
from resgate_cronograma.exporters import (
    construir_payload_cpm_pid,
    exportar_html,
    exportar_json,
)
from resgate_cronograma.models import Frente, ScheduleCycleError
from resgate_cronograma.parsers import carregar_csv
from resgate_cronograma.pid import (
    DEADBAND_PCT,
    FOLGA_ALERTA,
    FOLGA_CRITICA,
    KD,
    KI,
    KP,
    TETO_SATURACAO,
    pid_por_frente,
    prescrever,
)
from resgate_cronograma.reporting import (
    configurar_stdout_utf8,
    formatar_relatorio_cpm_pid,
)


def carregar(caminho: str) -> list[Frente]:
    """Carrega frentes de um CSV unificado (9 ou 12 colunas)."""
    return carregar_csv(caminho)


def relatorio(
    frentes: Sequence[Frente],
    ciclo: int | None = None,
    prazo: float | None = None,
) -> str:
    """Gera o relatório textual CPM + PID."""
    return formatar_relatorio_cpm_pid(frentes, ciclo=ciclo, prazo=prazo)


def main(argv: list[str] | None = None) -> int:
    configurar_stdout_utf8()
    parser = argparse.ArgumentParser(
        description="Diagnóstico determinístico da malha crítica (CPM + PID Modelo 1)."
    )
    parser.add_argument(
        "csv",
        nargs="?",
        default=None,
        help="Caminho para o arquivo CSV de cronograma (ou use --db + --projeto)",
    )
    parser.add_argument("--db", default=None, help="Caminho para banco SQLite de ciclos")
    parser.add_argument("--projeto", default=None, help="Nome do projeto no banco SQLite")
    parser.add_argument(
        "--ciclo",
        type=int,
        default=None,
        help="Número de ciclos do HistoricoDesvios a considerar (>= 1)",
    )
    parser.add_argument(
        "--prazo",
        type=float,
        default=None,
        help="Prazo contratual em dias úteis para recalcular folgas via CPM",
    )
    parser.add_argument(
        "--custos",
        default=None,
        help="JSON inline ou arquivo .json com parâmetros de custo em R$",
    )
    parser.add_argument("--json", dest="json_out", default=None, help="Caminho do relatório JSON")
    parser.add_argument("--html", dest="html_out", default=None, help="Caminho do relatório HTML")
    args = parser.parse_args(argv)

    if args.ciclo is not None and args.ciclo < 1:
        print("ERRO: --ciclo deve ser >= 1.", file=sys.stderr)
        return 1

    try:
        frentes, origem = resolver_atividades_cli(
            args.csv, args.db, args.projeto, ciclo=args.ciclo
        )
        params_custo = carregar_parametros_custo(args.custos)
        ciclo_pid = None if args.db else args.ciclo
        ciclo_exibicao = origem.get("ciclo") if args.db else args.ciclo
        plano_custo = avaliar_plano_custo_cpm(
            frentes,
            ciclo=ciclo_pid,
            prazo=args.prazo,
            parametros=params_custo,
        )
        texto = formatar_relatorio_cpm_pid(
            frentes,
            ciclo=ciclo_exibicao if isinstance(ciclo_exibicao, int) else args.ciclo,
            prazo=args.prazo,
            plano_custo=plano_custo,
        )
        if args.json_out or args.html_out:
            payload = construir_payload_cpm_pid(
                frentes,
                ciclo=ciclo_pid,
                prazo=args.prazo,
                parametros_custo=params_custo,
                origem=origem,
            )
            if args.json_out:
                exportar_json(payload, args.json_out)
            if args.html_out:
                exportar_html(payload, args.html_out)
    except (OSError, ValueError, TypeError, KeyError, ScheduleCycleError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    print(texto)
    return 0


__all__ = [
    "DEADBAND_PCT",
    "FOLGA_ALERTA",
    "FOLGA_CRITICA",
    "KD",
    "KI",
    "KP",
    "TETO_SATURACAO",
    "Frente",
    "carregar",
    "main",
    "pid_por_frente",
    "prescrever",
    "relatorio",
]


if __name__ == "__main__":
    raise SystemExit(main())
