"""Monte Carlo + CPM + PID — camada probabilística sobre o motor prescritivo.

Wrapper CLI retrocompatível delegando ao pacote modular `resgate_cronograma`.

Uso:
  python monte_carlo.py cronograma.csv --prazo 45 [--sims 10000] [--alvo 80]
                        [--seed 42] [--correlacao 0.5]
                        [--fatores-risco '{"Clima": 0.7}']
                        [--eficiencia-crashing 1.0] [--max-iter 5] [--sem-resgate]
                        [--custos JSON] [--json out.json] [--html out.html]
  python monte_carlo.py --db obra.db --projeto Obra-A --prazo 45 [--ciclo N]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from typing import Mapping

from resgate_cronograma.cost import avaliar_plano_custo_mc, carregar_parametros_custo
from resgate_cronograma.cycle_store import resolver_atividades_cli
from resgate_cronograma.exporters import (
    construir_payload_monte_carlo,
    exportar_html,
    exportar_json,
)
from resgate_cronograma.models import (
    AtividadeCronograma,
    Dependencia,
    ScheduleCycleError,
    parse_dependencias,
)
from resgate_cronograma.monte_carlo import (
    amostrar_pert,
    simular_cronograma,
)
from resgate_cronograma.parsers import carregar_csv
from resgate_cronograma.reporting import (
    configurar_stdout_utf8,
    formatar_relatorio_monte_carlo,
)


@dataclass
class Atividade:
    """Adaptador retrocompatível de atividade estocástica PERT."""

    id: str
    nome: str
    tipo: str
    preds: list
    dur_o: float
    dur_m: float
    dur_p: float

    def amostrar(self, rng: random.Random) -> float:
        """Amostra duração usando Modified PERT sem singularidades."""
        return amostrar_pert(self.dur_o, self.dur_m, self.dur_p, rng)


def parse_preds(s: str) -> list[tuple[str, str, float]]:
    """Converte string de predecessoras em lista `(pred_id, tipo, lag_dias)`."""
    return [(d.pred_id, d.tipo, d.lag_dias) for d in parse_dependencias(s)]


def simular(
    ats: Mapping[str, Atividade | AtividadeCronograma],
    prazo: float,
    n_sims: int,
    alvo: float,
    seed: int = 42,
    correlacao: float = 0.0,
) -> dict[str, object]:
    """Executa simulação Monte Carlo compatível com a interface legada."""
    atividades: list[AtividadeCronograma] = []
    for item in ats.values():
        if isinstance(item, AtividadeCronograma):
            atividades.append(item)
        else:
            deps = [
                Dependencia(pred_id=str(p), tipo=str(t).upper(), lag_dias=float(lag))  # type: ignore[arg-type]
                for p, t, lag in item.preds
            ]
            atividades.append(
                AtividadeCronograma(
                    id=item.id,
                    nome=item.nome,
                    tipo=item.tipo,
                    dependencias=deps,
                    dur_o=item.dur_o,
                    dur_m=item.dur_m,
                    dur_p=item.dur_p,
                    dur_m_explicita=item.dur_m > 0,
                )
            )

    res = simular_cronograma(
        atividades,
        prazo=prazo,
        n_sims=n_sims,
        alvo=alvo,
        seed=seed,
        correlacao=correlacao,
    )
    return {
        "P50": res.p50,
        "P80": res.p80,
        "P90": res.p90,
        "P(alvo)": res.p_alvo,
        "criticidade": res.criticidade,
        "sensibilidade": res.sensibilidade,
        "missing_predecessors": res.missing_predecessors,
    }


def _parse_fatores_risco(raw_json: str | None) -> dict[str, float] | None:
    if not raw_json:
        return None
    try:
        bruto = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--fatores-risco JSON inválido: {exc}") from exc
    if not isinstance(bruto, dict):
        raise ValueError("--fatores-risco deve ser um objeto JSON {grupo: rho}.")
    return {str(k): float(v) for k, v in bruto.items()}


def main(argv: list[str] | None = None) -> int:
    configurar_stdout_utf8()
    parser = argparse.ArgumentParser(
        description="Simulação Monte Carlo (Modified PERT + Iman-Conover) e malha fechada PID."
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
    parser.add_argument("--sims", type=int, default=10000, help="Número de simulações")
    parser.add_argument("--prazo", type=float, required=True, help="Prazo contratual (dias úteis)")
    parser.add_argument("--alvo", type=float, default=80.0, help="Percentil alvo do compromisso")
    parser.add_argument("--seed", type=int, default=42, help="Semente aleatória determinística")
    parser.add_argument(
        "--correlacao",
        type=float,
        default=0.0,
        help="Correlação de risco global [0.0, 1.0) entre atividades do mesmo grupo",
    )
    parser.add_argument(
        "--fatores-risco",
        type=str,
        default=None,
        help='JSON de correlação por grupo (ex.: \'{"Clima": 0.7}\')',
    )
    parser.add_argument(
        "--eficiencia-crashing",
        type=float,
        default=1.0,
        help="Eficiência da conversão de HH em redução de prazo (0, 1]",
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=5,
        help="Máximo de iterações da malha fechada Antes x Depois",
    )
    parser.add_argument(
        "--sem-resgate",
        action="store_true",
        help="Desativa a re-simulação em malha fechada Antes x Depois",
    )
    parser.add_argument(
        "--custos",
        default=None,
        help="JSON inline ou arquivo .json com parâmetros de custo em R$",
    )
    parser.add_argument("--json", dest="json_out", default=None, help="Caminho do relatório JSON")
    parser.add_argument("--html", dest="html_out", default=None, help="Caminho do relatório HTML")
    args = parser.parse_args(argv)

    try:
        fatores = _parse_fatores_risco(args.fatores_risco)
        atividades, origem = resolver_atividades_cli(
            args.csv, args.db, args.projeto, ciclo=args.ciclo
        )
        params_custo = carregar_parametros_custo(args.custos)
        ciclo_pid = None if args.db else args.ciclo
        resultado = simular_cronograma(
            atividades,
            prazo=args.prazo,
            n_sims=args.sims,
            alvo=args.alvo,
            seed=args.seed,
            ciclo=ciclo_pid,
            correlacao=args.correlacao,
            fatores_risco=fatores,
            fechar_malha=not args.sem_resgate,
            eficiencia_crashing=args.eficiencia_crashing,
            max_iter=args.max_iter,
        )
        plano_custo = avaliar_plano_custo_mc(
            atividades,
            resultado,
            parametros=params_custo,
            eficiencia_crashing=args.eficiencia_crashing,
        )
        texto = formatar_relatorio_monte_carlo(
            atividades, resultado, plano_custo=plano_custo
        )
        if args.json_out or args.html_out:
            payload = construir_payload_monte_carlo(
                atividades,
                resultado,
                parametros_custo=params_custo,
                eficiencia_crashing=args.eficiencia_crashing,
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
    "Atividade",
    "main",
    "parse_preds",
    "simular",
]


if __name__ == "__main__":
    raise SystemExit(main())
