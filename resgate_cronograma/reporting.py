"""Formatadores de saída textual para CLI (determinístico CPM+PID e Monte Carlo)."""
from __future__ import annotations

import sys
from typing import Sequence

from resgate_cronograma.cpm import RedeCPM, aplicar_folgas_cpm
from resgate_cronograma.models import (
    AtividadeCronograma,
    PlanoOtimizadoCusto,
    ResultadoMonteCarlo,
)
from resgate_cronograma.pid import FOLGA_ALERTA, calcular_pid, prescrever


def configurar_stdout_utf8() -> None:
    """Garante codificação UTF-8 segura no stdout (ex.: consoles Windows cp1252)."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    reconfigure_err = getattr(sys.stderr, "reconfigure", None)
    if callable(reconfigure_err):
        try:
            reconfigure_err(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _linha_aviso_externas(missing_predecessors: Sequence[str]) -> str | None:
    if not missing_predecessors:
        return None
    lista = ", ".join(missing_predecessors)
    return (
        f"AVISO: predecessoras externas/ausentes ancoradas em t=0: {lista}"
    )


def _formatar_brl(valor: float) -> str:
    texto = f"{valor:,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _formatar_bloco_custo(plano_custo: PlanoOtimizadoCusto | None) -> list[str]:
    if plano_custo is None:
        return []
    if not plano_custo.opcoes and plano_custo.custo_total_malha_fechada_brl is None:
        return []

    linhas = [
        "",
        "─" * 62,
        "RANKING CUSTO × PRAZO (R$/dia recuperado):",
    ]
    if plano_custo.custo_total_malha_fechada_brl is not None:
        linhas.append(
            "  Orçamento malha fechada (crashing): "
            f"{_formatar_brl(plano_custo.custo_total_malha_fechada_brl)}"
        )

    for op in plano_custo.opcoes:
        if op.custo_por_dia_brl is None:
            taxa_str = "N/A (externo)"
            ganho_str = "prazo externo"
        else:
            taxa_str = f"{_formatar_brl(op.custo_por_dia_brl)}/dia"
            ganho_str = f"-{op.dias_recuperados_estimados:.2f}d"
        linhas.append(
            f"  • ID {op.atividade_id} ({op.nome}) | {op.estrategia}: "
            f"{taxa_str} | custo { _formatar_brl(op.custo_estimado_brl) } ({ganho_str})"
        )
    return linhas


def formatar_relatorio_cpm_pid(
    atividades: Sequence[AtividadeCronograma],
    ciclo: int | None = None,
    prazo: float | None = None,
    plano_custo: PlanoOtimizadoCusto | None = None,
) -> str:
    """Gera relatório textual do diagnóstico CPM + PID discreto (Modelo 1)."""
    rede = RedeCPM(atividades)
    aplicar_folgas_cpm(atividades, prazo=prazo, rede=rede)

    sufixos = []
    if ciclo is not None:
        sufixos.append(f"ciclo {ciclo}")
    if prazo is not None:
        sufixos.append(f"prazo {prazo:g}d")
    sufixo_str = f" ({', '.join(sufixos)})" if sufixos else ""

    linhas = [
        "═" * 62,
        f"DIAGNÓSTICO DA MALHA CRÍTICA — CPM + PID{sufixo_str}",
        "═" * 62,
    ]
    aviso = _linha_aviso_externas(rede.missing_predecessors)
    if aviso:
        linhas.append(aviso)

    criticas = [f for f in atividades if f.folga <= FOLGA_ALERTA]
    if not criticas:
        return "\n".join(linhas + ["Nenhuma frente crítica. Cronograma saudável."])

    for f in sorted(criticas, key=lambda x: x.folga):
        pid_res = calcular_pid(f, ciclo=ciclo)
        if pid_res.inativo:
            detalhe_pid = "P=— I=— D=— → uk=— HH (PID inativo)"
        elif pid_res.deadband:
            detalhe_pid = "P=— I=— D=— → uk=0.0 HH (deadband)"
        else:
            tag_sat = f" [SATURADO teto {pid_res.teto_hh:.0f} HH]" if pid_res.saturado else ""
            detalhe_pid = (
                f"P={pid_res.p_dias:g}d I={pid_res.i_dias:g}d D={pid_res.d_dias:g}d "
                f"→ u={pid_res.u_dias:.2f}d × {pid_res.taxa_hh_dia:.1f} HH/d "
                f"= {pid_res.uk_hh:.1f} HH{tag_sat}"
            )

        linhas += [
            "",
            f"▸ [{f.criticidade}] ID {f.id} — {f.nome}",
            f"  Tipo: {f.tipo} | Folga: {f.folga:+.1f}d | "
            f"HH: {f.hh_realizado:.0f}/{f.baseline_hh:.0f} "
            f"(restam {f.hh_restante:.0f})",
            f"  PID (Modelo 1): {detalhe_pid}",
        ]
        for acao in prescrever(
            f,
            pid_res.uk_hh,
            pid_res,
            tem_fs=rede.tem_ligacao_fs(f.id),
        ):
            linhas.append(f"  {acao}")

    linhas.extend(_formatar_bloco_custo(plano_custo))
    linhas += [
        "",
        "─" * 62,
        "Ordem de impacto: 1) Fast-tracking (menor custo) → "
        "2) Crashing (HH) → 3) Turno/Terceirização → 4) Diligenciamento",
    ]
    return "\n".join(linhas)


def formatar_relatorio_monte_carlo(
    atividades: Sequence[AtividadeCronograma],
    resultado: ResultadoMonteCarlo,
    plano_custo: PlanoOtimizadoCusto | None = None,
) -> str:
    """Gera relatório textual da simulação Monte Carlo, Tornado e malha fechada."""
    por_id = {a.id: a for a in atividades}
    corr_str = f" | corr: {resultado.correlacao:.2f}" if resultado.correlacao > 0.0 else ""
    linhas = [
        "═" * 62,
        f"MONTE CARLO — {resultado.n_sims:,} simulações | "
        f"prazo contratual: {resultado.prazo:.0f}d | seed: {resultado.seed}{corr_str}",
        "═" * 62,
    ]
    aviso = _linha_aviso_externas(resultado.missing_predecessors)
    if aviso:
        linhas.append(aviso)

    linhas += [
        f"  Término P50: {resultado.prazo + resultado.p50:.1f}d  (50% de chance até aqui)",
        f"  Término P80: {resultado.prazo + resultado.p80:.1f}d  (compromisso conservador)",
        f"  Término P90: {resultado.prazo + resultado.p90:.1f}d",
        f"  Atraso esperado P{resultado.alvo:.0f}: {resultado.p_alvo:+.1f}d",
        "",
        "ÍNDICE DE CRITICIDADE (fração das simulações no caminho crítico):",
    ]
    for tid, val in sorted(resultado.criticidade.items(), key=lambda kv: -kv[1]):
        barra = "█" * int(val * 20)
        nome = por_id[tid].nome[:28] if tid in por_id else tid
        linhas.append(f"  ID {tid:>5} ({nome:28s}) {val:5.0%} {barra}")

    if resultado.sensibilidade:
        linhas += [
            "",
            "SENSIBILIDADE (Correlação de Spearman — Tornado):",
        ]
        for tid, val in sorted(
            resultado.sensibilidade.items(), key=lambda kv: -abs(kv[1])
        ):
            barra = "█" * int(abs(val) * 20)
            nome = por_id[tid].nome[:28] if tid in por_id else tid
            linhas.append(f"  ID {tid:>5} ({nome:28s}) {val:+6.2f} {barra}")

    linhas += [
        "",
        "─" * 62,
        "PRESCRIÇÃO INTEGRADA (PID × criticidade Monte Carlo):",
    ]
    if not resultado.prescricoes:
        linhas.append("Nenhuma atividade atingiu criticidade >= 40%.")
    else:
        for presc in resultado.prescricoes:
            f = presc.atividade
            uk_str = "—" if presc.pid.inativo else f"{presc.pid.uk_hh:.1f}"
            adj_str = "—" if presc.pid.inativo else f"{presc.uk_hh_ajustado:.1f}"
            linhas.append(f"\n▸ ID {f.id} — {f.nome} | criticidade MC: {presc.criticidade:.0%}")
            linhas.append(f"  PID: uk={uk_str} HH → ajustado pelo risco: {adj_str} HH")
            for acao in presc.acoes:
                linhas.append(f"  {acao}")

    if resultado.resgate is not None:
        resg = resultado.resgate
        prazo = resultado.prazo
        linhas += [
            "",
            "─" * 62,
            "SIMULAÇÃO PÓS-RESGATE (MALHA FECHADA ANTES × DEPOIS):",
            f"  Término P50: Antes {prazo + resg.p50_antes:.1f}d  →  Depois {prazo + resg.p50_depois:.1f}d",
            f"  Término P80: Antes {prazo + resg.p80_antes:.1f}d  →  Depois {prazo + resg.p80_depois:.1f}d",
            f"  Término P90: Antes {prazo + resg.p90_antes:.1f}d  →  Depois {prazo + resg.p90_depois:.1f}d",
            f"  Atraso P{resultado.alvo:.0f} : Antes {resg.p_alvo_antes:+.1f}d  →  Depois {resg.p_alvo_depois:+.1f}d  "
            f"(recuperados: {resg.dias_recuperados:.1f}d | iterações: {resg.iteracoes})",
        ]
        if resg.convergiu:
            linhas.append(
                f"  Status: ✓ META P{resultado.alvo:.0f} ATINGIDA (<= {prazo:.0f}d)"
            )
        else:
            linhas.append(
                f"  Status: ✗ META P{resultado.alvo:.0f} NÃO ATINGIDA — "
                f"gap residual: +{resg.gap_final:.1f}d"
            )
        if resg.alocacoes:
            linhas.append("  Esforço alocado na malha fechada:")
            for al in resg.alocacoes:
                tag_sat = " [SATURADO]" if al.saturado else ""
                linhas.append(
                    f"    • ID {al.atividade_id} ({al.nome}): +{al.uk_hh_aplicado:.1f} HH "
                    f"| DurM: {al.dur_m_antes:.1f}d → {al.dur_m_depois:.1f}d{tag_sat}"
                )
        if resg.gargalos_residuais:
            linhas.append(
                f"  Gargalos residuais: {', '.join(resg.gargalos_residuais)}"
            )

    linhas.extend(_formatar_bloco_custo(plano_custo))
    linhas += [
        "",
        "─" * 62,
        "Lógica: só age em atividade com criticidade ≥ 40% — crashing em",
        "frente que é gargalo em só 5% das simulações é dinheiro queimado.",
    ]
    return "\n".join(linhas)
