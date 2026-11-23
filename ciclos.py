"""Gerenciador CLI de snapshots semanais por projeto em SQLite (`HistoricoDesvios` automático).

Uso:
  python ciclos.py registrar obra.xer --db obra.db --projeto Obra-A [--prazo 45]
  python ciclos.py listar --db obra.db [--projeto Obra-A]
  python ciclos.py exportar --db obra.db --projeto Obra-A -o ciclo2.csv [--ciclo 2]
"""
from __future__ import annotations

import argparse
import sys

from resgate_cronograma.cycle_store import (
    exportar_ciclo_csv,
    listar_ciclos,
    listar_projetos,
    registrar_ciclo,
)
from resgate_cronograma.models import ScheduleCycleError
from resgate_cronograma.reporting import configurar_stdout_utf8


def main(argv: list[str] | None = None) -> int:
    configurar_stdout_utf8()
    parser = argparse.ArgumentParser(
        description="Gerencia snapshots de cronograma em SQLite e deriva HistoricoDesvios."
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    p_reg = sub.add_parser("registrar", help="Registra snapshot .xer, .xml ou .csv")
    p_reg.add_argument("arquivo", help="Caminho para .xer, .xml (MSPDI) ou .csv")
    p_reg.add_argument("--db", required=True, help="Caminho para o banco SQLite")
    p_reg.add_argument("--projeto", required=True, help="Nome do projeto/obra")
    p_reg.add_argument("--numero", type=int, default=None, help="Número explícito do ciclo (>= 1)")
    p_reg.add_argument("--rotulo", default=None, help="Rótulo do ciclo (ex.: Semana 12)")
    p_reg.add_argument("--data", default=None, help="Data da medição (YYYY-MM-DD)")
    p_reg.add_argument(
        "--prazo",
        type=float,
        default=None,
        help="Prazo contratual (dias úteis) para calcular folgas CPM ao registrar",
    )

    p_list = sub.add_parser("listar", help="Lista projetos ou ciclos de um projeto")
    p_list.add_argument("--db", required=True, help="Caminho para o banco SQLite")
    p_list.add_argument("--projeto", default=None, help="Filtrar e listar ciclos do projeto")

    p_exp = sub.add_parser("exportar", help="Exporta ciclo enriquecido com HistoricoDesvios")
    p_exp.add_argument("--db", required=True, help="Caminho para o banco SQLite")
    p_exp.add_argument("--projeto", required=True, help="Nome do projeto/obra")
    p_exp.add_argument("-o", "--output", required=True, help="Caminho do CSV de saída")
    p_exp.add_argument(
        "--ciclo",
        type=int,
        default=None,
        help="Número do ciclo a exportar (padrão: último registrado)",
    )

    args = parser.parse_args(argv)
    try:
        if args.comando == "registrar":
            info = registrar_ciclo(
                args.db,
                args.projeto,
                args.arquivo,
                numero=args.numero,
                rotulo=args.rotulo,
                data_medicao=args.data,
                prazo=args.prazo,
            )
            print(
                f"✓ Projeto '{info.projeto}' | Ciclo {info.numero} ({info.rotulo}) "
                f"| {info.total_atividades} atividades registradas em {args.db}"
            )
        elif args.comando == "listar":
            if args.projeto:
                ciclos = listar_ciclos(args.db, args.projeto)
                print(f"Ciclos de '{args.projeto}' em {args.db}:")
                if not ciclos:
                    print("  (nenhum ciclo registrado)")
                for c in ciclos:
                    prazo_str = f" | prazo={c.prazo_dias:g}d" if c.prazo_dias is not None else ""
                    print(
                        f"  Ciclo {c.numero:>3} | {c.rotulo} | {c.data_medicao} | "
                        f"origem={c.origem} | {c.total_atividades} atividades{prazo_str}"
                    )
            else:
                projetos = listar_projetos(args.db)
                print(f"Projetos em {args.db}:")
                if not projetos:
                    print("  (nenhum projeto registrado)")
                for p in projetos:
                    ult = p["ultimo_ciclo"] if p["ultimo_ciclo"] is not None else "—"
                    print(
                        f"  {p['projeto']} | {p['total_ciclos']} ciclo(s) | último: {ult}"
                    )
        elif args.comando == "exportar":
            atividades, info = exportar_ciclo_csv(
                args.db,
                args.projeto,
                args.output,
                ciclo=args.ciclo,
            )
            print(
                f"✓ Ciclo {info.numero} de '{info.projeto}' ({len(atividades)} atividades) "
                f"→ {args.output}"
            )
    except (OSError, ValueError, KeyError, ScheduleCycleError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    return 0


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
