"""Motor financeiro custo x prazo (R$/dia recuperado) para alavancas de resgate."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from resgate_cronograma.cpm import RedeCPM, aplicar_folgas_cpm
from resgate_cronograma.models import (
    HORAS_POR_DIA,
    AlocacaoValorada,
    AtividadeCronograma,
    OpcaoIntervencao,
    ParametrosCusto,
    PlanoOtimizadoCusto,
    ResultadoMonteCarlo,
)
from resgate_cronograma.pid import FOLGA_ALERTA, calcular_pid

_CAMPOS_CUSTO_VALIDOS = frozenset(ParametrosCusto.__dataclass_fields__.keys())


def validar_parametros_custo(params: ParametrosCusto) -> ParametrosCusto:
    """Valida faixas numéricas dos parâmetros financeiros em R$."""
    campos_nao_negativos = (
        ("custo_hh_base", params.custo_hh_base),
        ("custo_hh_hora_extra", params.custo_hh_hora_extra),
        ("custo_hh_segundo_turno", params.custo_hh_segundo_turno),
        ("custo_fixo_fast_tracking", params.custo_fixo_fast_tracking),
        ("custo_fixo_diligenciamento", params.custo_fixo_diligenciamento),
    )
    for nome, val in campos_nao_negativos:
        if not math.isfinite(val) or val < 0.0:
            raise ValueError(f"Parâmetro de custo '{nome}' deve ser >= 0: {val}.")

    if not math.isfinite(params.fator_risco_retrabalho_ft) or not (
        0.0 <= params.fator_risco_retrabalho_ft <= 1.0
    ):
        raise ValueError(
            "Parâmetro 'fator_risco_retrabalho_ft' deve estar em [0.0, 1.0]: "
            f"{params.fator_risco_retrabalho_ft}."
        )
    if not math.isfinite(params.ganho_max_ft_pct) or not (
        0.0 < params.ganho_max_ft_pct <= 1.0
    ):
        raise ValueError(
            "Parâmetro 'ganho_max_ft_pct' deve estar em (0.0, 1.0]: "
            f"{params.ganho_max_ft_pct}."
        )
    return params


def carregar_parametros_custo(
    spec: str | Path | Mapping[str, object] | ParametrosCusto | None = None,
) -> ParametrosCusto:
    """Carrega parâmetros de custo de padrões BRL, dict, JSON inline ou arquivo `.json`."""
    if spec is None:
        return ParametrosCusto()
    if isinstance(spec, ParametrosCusto):
        return validar_parametros_custo(spec)

    if isinstance(spec, Mapping):
        dados: Mapping[str, object] = spec
    elif isinstance(spec, Path):
        if not spec.is_file():
            raise ValueError(f"Arquivo de parâmetros de custo não encontrado: {spec}.")
        try:
            bruto = json.loads(spec.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON inválido em '{spec}': {exc}") from exc
        if not isinstance(bruto, dict):
            raise ValueError("Arquivo --custos deve conter um objeto JSON.")
        dados = bruto
    elif isinstance(spec, str):
        texto = spec.strip()
        if not texto:
            return ParametrosCusto()
        if texto.startswith("{"):
            try:
                bruto = json.loads(texto)
            except json.JSONDecodeError as exc:
                raise ValueError(f"--custos JSON inválido: {exc}") from exc
        else:
            caminho = Path(texto)
            if not caminho.is_file():
                raise ValueError(
                    f"--custos inválido (informe objeto JSON ou caminho de arquivo existente): {spec}"
                )
            try:
                bruto = json.loads(caminho.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON inválido em '{caminho}': {exc}") from exc
        if not isinstance(bruto, dict):
            raise ValueError("--custos deve representar um objeto JSON {parametro: valor}.")
        dados = bruto
    else:
        raise ValueError(f"Tipo inválido para --custos: {type(spec).__name__}.")

    desconhecidas = sorted(set(dados.keys()) - _CAMPOS_CUSTO_VALIDOS)
    if desconhecidas:
        raise ValueError(
            f"Parâmetro(s) de custo desconhecido(s): {', '.join(desconhecidas)}."
        )

    base = {k: getattr(ParametrosCusto(), k) for k in _CAMPOS_CUSTO_VALIDOS}
    for k, v in dados.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"Parâmetro de custo '{k}' deve ser numérico: {v!r}.")
        base[k] = float(v)

    return validar_parametros_custo(ParametrosCusto(**base))


def _avaliar_opcoes_frente(
    f: AtividadeCronograma,
    *,
    tem_fs: bool,
    uk_hh: float,
    saturado: bool,
    dur_ref: float,
    precisa_resgate: bool,
    params: ParametrosCusto,
    eficiencia_crashing: float,
) -> list[OpcaoIntervencao]:
    if not (0.0 < eficiencia_crashing <= 1.0):
        raise ValueError(
            f"Eficiência de crashing deve estar em (0.0, 1.0]: {eficiencia_crashing}."
        )

    if f.concluida:
        return []

    if not f.eh_interna:
        custo_ext = round(params.custo_fixo_diligenciamento, 2)
        return [
            OpcaoIntervencao(
                atividade_id=f.id,
                nome=f.nome,
                tipo=f.tipo,
                estrategia="Diligenciamento Externo",
                descricao=(
                    "Diligenciamento de fornecedor / aprovação formal "
                    "(hora extra interna inócua)"
                ),
                hh_adicional=0.0,
                custo_estimado_brl=custo_ext,
                dias_recuperados_estimados=0.0,
                custo_por_dia_brl=None,
                saturado=False,
            )
        ]

    if uk_hh < 0.0:
        return []

    hh_ref = f.hh_restante if f.hh_restante > 0 else f.baseline_hh
    dur_base = (
        f.dur_m
        if (f.dur_m_explicita and f.dur_m > 0.0)
        else (dur_ref if dur_ref > 0.0 else (max(hh_ref / HORAS_POR_DIA, 1.0) if hh_ref > 0 else 0.0))
    )
    if dur_base <= 0.0:
        return []

    opcoes: list[OpcaoIntervencao] = []
    if tem_fs and (uk_hh > 0.0 or precisa_resgate):
        dias_ft = dur_base * params.ganho_max_ft_pct
        if dias_ft > 1e-9:
            custo_ft = (
                params.custo_fixo_fast_tracking
                + params.fator_risco_retrabalho_ft * hh_ref * params.custo_hh_base
            )
            opcoes.append(
                OpcaoIntervencao(
                    atividade_id=f.id,
                    nome=f.nome,
                    tipo=f.tipo,
                    estrategia="Fast-tracking (FS -> SS)",
                    descricao=(
                        f"Sobreposição parcial FS -> SS (~{params.ganho_max_ft_pct:.0%} da duração, "
                        f"risco retrabalho {params.fator_risco_retrabalho_ft:.0%})"
                    ),
                    hh_adicional=0.0,
                    custo_estimado_brl=round(custo_ft, 2),
                    dias_recuperados_estimados=round(dias_ft, 3),
                    custo_por_dia_brl=round(custo_ft / dias_ft, 2),
                    saturado=saturado,
                )
            )

    if uk_hh > 0.0 and hh_ref > 0.0:
        fator = 1.0 / (1.0 + eficiencia_crashing * (uk_hh / hh_ref))
        dias_crash = dur_base * (1.0 - fator)
        if dias_crash > 1e-9:
            custo_crash = uk_hh * params.custo_hh_hora_extra
            opcoes.append(
                OpcaoIntervencao(
                    atividade_id=f.id,
                    nome=f.nome,
                    tipo=f.tipo,
                    estrategia="Crashing (Hora Extra)",
                    descricao=f"+{uk_hh:.1f} HH em hora extra só nas tarefas críticas",
                    hh_adicional=round(uk_hh, 2),
                    custo_estimado_brl=round(custo_crash, 2),
                    dias_recuperados_estimados=round(dias_crash, 3),
                    custo_por_dia_brl=round(custo_crash / dias_crash, 2),
                    saturado=saturado,
                )
            )

    if saturado and hh_ref > 0.0:
        uk_turno = 0.50 * hh_ref
        fator_turno = 1.0 / (1.0 + eficiencia_crashing * 0.50)
        dias_turno = dur_base * (1.0 - fator_turno)
        if dias_turno > 1e-9:
            custo_turno = uk_turno * params.custo_hh_segundo_turno
            opcoes.append(
                OpcaoIntervencao(
                    atividade_id=f.id,
                    nome=f.nome,
                    tipo=f.tipo,
                    estrategia="2º Turno / Terceirização",
                    descricao=(
                        f"Abertura de 2º turno / terceirização (+{uk_turno:.0f} HH após "
                        "teto de +35% em hora extra)"
                    ),
                    hh_adicional=round(uk_turno, 2),
                    custo_estimado_brl=round(custo_turno, 2),
                    dias_recuperados_estimados=round(dias_turno, 3),
                    custo_por_dia_brl=round(custo_turno / dias_turno, 2),
                    saturado=True,
                )
            )

    return opcoes


def _ordenar_opcoes(opcoes: Sequence[OpcaoIntervencao]) -> list[OpcaoIntervencao]:
    return sorted(
        opcoes,
        key=lambda op: (
            op.custo_por_dia_brl is None,
            op.custo_por_dia_brl if op.custo_por_dia_brl is not None else float("inf"),
            -op.dias_recuperados_estimados,
            op.custo_estimado_brl,
            op.atividade_id,
            op.estrategia,
        ),
    )


def avaliar_plano_custo_cpm(
    atividades: Sequence[AtividadeCronograma],
    *,
    rede: RedeCPM | None = None,
    ciclo: int | None = None,
    prazo: float | None = None,
    parametros: ParametrosCusto | str | Path | Mapping[str, object] | None = None,
    eficiencia_crashing: float = 1.0,
) -> PlanoOtimizadoCusto:
    """Valora e ranqueia intervenções em R$/dia para o diagnóstico CPM + PID."""
    params = carregar_parametros_custo(parametros)
    grafo = rede or RedeCPM(atividades)
    if prazo is not None:
        aplicar_folgas_cpm(atividades, prazo=prazo, rede=grafo)

    opcoes: list[OpcaoIntervencao] = []
    criticas = sorted(
        (f for f in atividades if f.folga <= FOLGA_ALERTA),
        key=lambda item: (item.folga, item.id),
    )
    for f in criticas:
        if f.concluida:
            continue
        pid_res = calcular_pid(f, ciclo=ciclo)
        if pid_res.deadband and (f.eh_interna or f.folga > 0.0):
            continue
        opcoes.extend(
            _avaliar_opcoes_frente(
                f,
                tem_fs=grafo.tem_ligacao_fs(f.id),
                uk_hh=pid_res.uk_hh,
                saturado=pid_res.saturado,
                dur_ref=pid_res.dur_ref,
                precisa_resgate=True,
                params=params,
                eficiencia_crashing=eficiencia_crashing,
            )
        )

    return PlanoOtimizadoCusto(
        parametros=params,
        opcoes=_ordenar_opcoes(opcoes),
        custo_total_malha_fechada_brl=None,
        alocacoes_valoradas=[],
    )


def avaliar_plano_custo_mc(
    atividades: Sequence[AtividadeCronograma],
    resultado: ResultadoMonteCarlo,
    *,
    rede: RedeCPM | None = None,
    parametros: ParametrosCusto | str | Path | Mapping[str, object] | None = None,
    eficiencia_crashing: float = 1.0,
) -> PlanoOtimizadoCusto:
    """Valora intervenções em R$/dia e apura orçamento exato da malha fechada MC -> PID."""
    params = carregar_parametros_custo(parametros)
    grafo = rede or RedeCPM(atividades)
    por_id = {a.id: a for a in atividades}
    presc_por_id = {p.atividade.id: p for p in resultado.prescricoes}
    alloc_por_id = (
        {al.atividade_id: al for al in resultado.resgate.alocacoes}
        if resultado.resgate is not None
        else {}
    )

    ids_alvo = sorted(
        set(presc_por_id.keys()) | set(alloc_por_id.keys()),
        key=lambda tid: (-resultado.criticidade.get(tid, 0.0), tid),
    )

    opcoes: list[OpcaoIntervencao] = []
    precisa_resgate = resultado.p_alvo > 0.0
    for tid in ids_alvo:
        f = por_id.get(tid)
        if f is None or f.concluida:
            continue
        presc = presc_por_id.get(tid)
        alloc = alloc_por_id.get(tid)
        if alloc is not None:
            uk_eff = alloc.uk_hh_aplicado
            saturado_eff = alloc.saturado
        elif presc is not None:
            if presc.pid.deadband and (f.eh_interna or not precisa_resgate):
                continue
            uk_eff = presc.uk_hh_ajustado
            saturado_eff = presc.pid.saturado
        else:
            uk_eff = 0.0
            saturado_eff = False

        dur_ref = presc.pid.dur_ref if presc is not None else 0.0
        opcoes.extend(
            _avaliar_opcoes_frente(
                f,
                tem_fs=grafo.tem_ligacao_fs(f.id),
                uk_hh=uk_eff,
                saturado=saturado_eff,
                dur_ref=dur_ref,
                precisa_resgate=precisa_resgate,
                params=params,
                eficiencia_crashing=eficiencia_crashing,
            )
        )

    alocacoes_valoradas: list[AlocacaoValorada] = []
    custo_total_malha: float | None = None
    if resultado.resgate is not None:
        total_bruto = 0.0
        for al in resultado.resgate.alocacoes:
            custo_al = al.uk_hh_aplicado * params.custo_hh_hora_extra
            total_bruto += custo_al
            alocacoes_valoradas.append(
                AlocacaoValorada(
                    atividade_id=al.atividade_id,
                    nome=al.nome,
                    uk_hh_aplicado=round(al.uk_hh_aplicado, 2),
                    custo_brl=round(custo_al, 2),
                    dias_comprimidos_dur_m=round(al.dur_m_antes - al.dur_m_depois, 4),
                )
            )
        custo_total_malha = round(total_bruto, 2)

    return PlanoOtimizadoCusto(
        parametros=params,
        opcoes=_ordenar_opcoes(opcoes),
        custo_total_malha_fechada_brl=custo_total_malha,
        alocacoes_valoradas=alocacoes_valoradas,
    )


__all__ = [
    "avaliar_plano_custo_cpm",
    "avaliar_plano_custo_mc",
    "carregar_parametros_custo",
    "validar_parametros_custo",
]
