"""Controlador PID discreto (Modelo 1: dias -> HH) e matriz de prescrição."""
from __future__ import annotations

import math
from typing import Mapping, Sequence

from resgate_cronograma.models import (
    HORAS_POR_DIA,
    AtividadeCronograma,
    ResultadoPID,
)

KP = 0.5
KI = 0.15
KD = 0.25
TETO_SATURACAO = 0.35
DEADBAND_PCT = 0.015
FOLGA_CRITICA = 0.0
FOLGA_ALERTA = 5.0


def calcular_taxa_e_ref(f: AtividadeCronograma) -> tuple[float, float, float]:
    """Calcula `(taxa_hh_dia, dur_ref, hh_ref)` seguindo a hierarquia do Modelo 1."""
    hh_ref = f.hh_restante if f.hh_restante > 0 else f.baseline_hh
    if f.dur_m_explicita and f.dur_m > 0:
        dur_ref = f.dur_m
        if f.hh_restante > 0:
            taxa_hh_dia = f.hh_restante / f.dur_m
        elif f.baseline_hh > 0:
            taxa_hh_dia = f.baseline_hh / f.dur_m
        else:
            taxa_hh_dia = HORAS_POR_DIA
    else:
        taxa_hh_dia = HORAS_POR_DIA
        dur_ref = max(hh_ref / HORAS_POR_DIA, 1.0)
    return taxa_hh_dia, dur_ref, hh_ref


def calcular_pid(
    f: AtividadeCronograma,
    ciclo: int | None = None,
    ignorar_deadband: bool = False,
) -> ResultadoPID:
    """Executa o PID discreto em dias com anti-windup condicional iterativo e converte para HH."""
    if ciclo is not None and ciclo < 1:
        raise ValueError(f"--ciclo deve ser >= 1 (recebido: {ciclo}).")

    taxa_hh_dia, dur_ref, hh_ref = calcular_taxa_e_ref(f)
    teto_hh = TETO_SATURACAO * hh_ref
    teto_dias = (teto_hh / taxa_hh_dia) if taxa_hh_dia > 0 else 0.0

    historico = list(f.historico[:ciclo]) if ciclo is not None else list(f.historico)
    if not historico:
        return ResultadoPID(
            u_dias=0.0,
            uk_hh=0.0,
            taxa_hh_dia=taxa_hh_dia,
            dur_ref=dur_ref,
            hh_ref=hh_ref,
            teto_hh=teto_hh,
            p_dias=0.0,
            i_dias=0.0,
            d_dias=0.0,
            inativo=True,
        )

    ek = historico[-1]
    if (
        not ignorar_deadband
        and (ek == 0.0 or (len(historico) > 1 and abs(ek) / max(dur_ref, 1.0) < DEADBAND_PCT))
    ):
        return ResultadoPID(
            u_dias=0.0,
            uk_hh=0.0,
            taxa_hh_dia=taxa_hh_dia,
            dur_ref=dur_ref,
            hh_ref=hh_ref,
            teto_hh=teto_hh,
            p_dias=0.0,
            i_dias=0.0,
            d_dias=0.0,
            deadband=True,
        )

    integral_acc = 0.0
    p_dias = 0.0
    i_dias = 0.0
    d_dias = 0.0
    u_dias = 0.0
    saturado = False

    for j, ej in enumerate(historico):
        p_dias = KP * ej
        d_dias = KD * (ej - historico[j - 1]) if j > 0 else 0.0
        integral_cand = integral_acc + ej
        u_raw = p_dias + KI * integral_cand + d_dias

        if teto_dias > 0 and abs(u_raw) > teto_dias:
            saturado = True
            # Integração condicional (clamping): congela quando ej agrava saturação;
            # permite descarregar se ej reduz o acumulador em direção a zero.
            if abs(integral_cand) < abs(integral_acc) or (u_raw * ej <= 0):
                integral_acc = integral_cand
            u_dias = math.copysign(teto_dias, u_raw)
            i_dias = KI * integral_acc
        elif teto_dias == 0.0 and abs(u_raw) > 0:
            saturado = True
            u_dias = 0.0
            i_dias = KI * integral_acc
        else:
            saturado = False
            integral_acc = integral_cand
            i_dias = KI * integral_acc
            u_dias = u_raw

    uk_hh = u_dias * taxa_hh_dia
    return ResultadoPID(
        u_dias=u_dias,
        uk_hh=uk_hh,
        taxa_hh_dia=taxa_hh_dia,
        dur_ref=dur_ref,
        hh_ref=hh_ref,
        teto_hh=teto_hh,
        p_dias=p_dias,
        i_dias=i_dias,
        d_dias=d_dias,
        saturado=saturado,
    )


def pid_por_frente(
    f: AtividadeCronograma,
    ciclo: int | None = None,
) -> tuple[float, dict[str, object]]:
    """Adaptador retrocompatível retornando `(uk_hh, detalhes)`."""
    res = calcular_pid(f, ciclo=ciclo)
    return res.uk_hh, res.to_legacy_dict()


def prescrever(
    f: AtividadeCronograma,
    uk: float,
    det: dict[str, object] | ResultadoPID,
    *,
    tem_fs: bool | None = None,
    forcado_por_risco: bool = False,
) -> list[str]:
    """Gera ações prescritivas ordenadas por impacto para uma frente crítica ou de risco."""
    if f.criticidade == "OK" and not forcado_por_risco:
        return []

    if isinstance(det, ResultadoPID):
        inativo = det.inativo
        deadband = det.deadband
        saturado = det.saturado
    else:
        inativo = not det
        deadband = bool(det.get("deadband")) if det else False
        saturado = bool(det.get("saturado")) if det else False

    if deadband:
        return ["• Variação residual (deadband): nenhuma ação — monitorar."]
    if inativo:
        return [
            "• PID inativo: sem histórico de desvios ainda "
            "(preencha HistoricoDesvios com as medições semanais)."
        ]

    acoes: list[str] = []
    has_fs = (
        tem_fs
        if tem_fs is not None
        else any(dep.tipo == "FS" for dep in f.dependencias)
    )

    if f.eh_interna:
        if uk < 0:
            acoes.append(
                f"• Frente adiantada/em recuperação (uk={uk:.1f} HH): manter "
                "monitoramento e avaliar realocação de capacidade ociosa."
            )
            return acoes

        if saturado:
            acoes.append(
                "• Teto físico atingido: abrir 2º turno ou terceirizar etapa "
                "(ex.: jateamento externo) — hora extra no mesmo turno não resolve."
            )

        if uk > 0:
            pessoas = round(uk / 44.0, 1)
            acoes.append(
                f"• Crashing calibrado: +{pessoas} profissional(is) ou "
                f"{uk / 20.0:.1f}h extras/dia (~{uk:.0f} HH) "
                "só nas tarefas críticas desta frente."
            )

        if has_fs:
            acoes.append(
                "• Fast-tracking: fracionar lotes e sobrepor sucessora "
                "(FS → SS com defasagem), liberando subconjuntos parciais."
            )
    else:
        acoes.append(
            "• Externa/Espera: hora extra é INÓCUA — diligenciar fornecedor, "
            "cobrar aprovação formal ou re-sequenciar montagem."
        )
        if f.folga <= FOLGA_ALERTA or forcado_por_risco:
            acoes.append(
                f"• ALERTA: folga de apenas {f.folga:+.0f}d — prazo externo pode "
                "anular o ganho obtido na fábrica."
            )

    return acoes


def alocar_gap_probabilistico(
    atividades: Sequence[AtividadeCronograma],
    criticidade: Mapping[str, float],
    sensibilidade: Mapping[str, float],
    gap_dias: float,
    limiar_criticidade: float = 0.40,
) -> dict[str, float]:
    """Reparte a lacuna `max(0, P_alvo - prazo)` entre frentes críticas controláveis (`Interna`)."""
    if gap_dias <= 0.0:
        return {}

    pesos: dict[str, float] = {}
    pesos_fallback: dict[str, float] = {}
    for a in atividades:
        if not a.eh_interna or a.dur_m <= 0.0:
            continue
        ic = criticidade.get(a.id, 0.0)
        if ic < limiar_criticidade:
            continue
        hh_ref = a.hh_restante if a.hh_restante > 0 else a.baseline_hh
        if hh_ref <= 0.0:
            continue
        sens = max(sensibilidade.get(a.id, 0.0), 0.05)
        pesos[a.id] = ic * sens * a.dur_m
        pesos_fallback[a.id] = ic * a.dur_m

    if not pesos:
        return {}

    total_pesos = sum(pesos.values())
    if total_pesos <= 0.0:
        pesos = pesos_fallback
        total_pesos = sum(pesos.values())
    if total_pesos <= 0.0:
        return {}

    return {aid: gap_dias * (peso / total_pesos) for aid, peso in pesos.items()}


def comprimir_pert_por_esforco(
    atividade: AtividadeCronograma,
    uk_hh: float,
    eficiencia: float = 1.0,
) -> tuple[float, float, float, float]:
    """Comprime `(DurO, DurM, DurP)` por conservação de trabalho dado `uk_hh >= 0`."""
    if not (0.0 < eficiencia <= 1.0):
        raise ValueError(
            f"Eficiência de crashing deve estar em (0.0, 1.0]: {eficiencia}."
        )
    if uk_hh < 0.0:
        raise ValueError(f"Esforço aplicado uk_hh deve ser >= 0: {uk_hh}.")

    hh_ref = atividade.hh_restante if atividade.hh_restante > 0 else atividade.baseline_hh
    if hh_ref <= 0.0 or uk_hh == 0.0:
        return (atividade.dur_o, atividade.dur_m, atividade.dur_p, 1.0)

    fator = 1.0 / (1.0 + eficiencia * (uk_hh / hh_ref))
    return (
        atividade.dur_o * fator,
        atividade.dur_m * fator,
        atividade.dur_p * fator,
        fator,
    )
