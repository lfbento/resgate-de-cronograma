"""Leitor endurecido de arquivos Primavera P6 (.xer)."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from resgate_cronograma.models import (
    HORAS_POR_DIA,
    AtividadeCronograma,
    Dependencia,
    LinkType,
)
from resgate_cronograma.parsers.classification import classificar_tipo
from resgate_cronograma.parsers.csv_io import calcular_pert_padrao

_TIPOS_EXCLUIDOS_P6 = {"TT_WBS", "TT_LOE"}
_TIPOS_MARCO_P6 = {"TT_MILE", "TT_FINMILE"}
_MAPA_LINK_P6: dict[str, LinkType] = {
    "PR_FS": "FS",
    "PR_SS": "SS",
    "PR_FF": "FF",
    "PR_SF": "SF",
}


def _para_float(valor: str) -> float:
    texto = (valor or "").strip()
    if not texto:
        return 0.0
    return float(texto)


def _para_dias(horas: float) -> float:
    return round(horas / HORAS_POR_DIA, 1)


def parse_xer(
    caminho: str | Path,
    overrides: Mapping[str, str] | None = None,
) -> list[AtividadeCronograma]:
    """Lê tabelas TASK, TASKPRED e TASKRSRC (opcional) de um arquivo `.xer`."""
    tabelas: dict[str, tuple[list[str], list[list[str]]]] = {}
    tabela_atual: str | None = None

    conteudo = Path(caminho).read_text(encoding="utf-8", errors="replace")
    for linha in conteudo.splitlines():
        partes = linha.split("\t")
        if not partes:
            continue
        marcador = partes[0].strip()
        if marcador == "%T" and len(partes) > 1:
            tabela_atual = partes[1].strip()
            tabelas[tabela_atual] = ([], [])
        elif marcador == "%F" and tabela_atual:
            tabelas[tabela_atual][0].extend(p.strip() for p in partes[1:])
        elif marcador == "%R" and tabela_atual:
            tabelas[tabela_atual][1].append(partes[1:])
        elif marcador == "%E":
            tabela_atual = None

    task_tbl = tabelas.get("TASK")
    if not task_tbl:
        raise ValueError("Tabela TASK não encontrada — arquivo .xer inválido.")

    campos_task, registros_task = task_tbl
    idx_task = {c: i for i, c in enumerate(campos_task)}

    def col(reg: list[str], idx_map: dict[str, int], *nomes: str) -> str:
        for nome in nomes:
            pos = idx_map.get(nome)
            if pos is not None and pos < len(reg):
                return reg[pos].strip()
        return ""

    # Filtra tarefas executáveis / marcos e mapeia task_id interno -> task_code preservado.
    registros_validos: list[list[str]] = []
    id_por_taskid: dict[str, str] = {}
    for reg in registros_task:
        task_type = col(reg, idx_task, "task_type").upper()
        if task_type in _TIPOS_EXCLUIDOS_P6:
            continue
        raw_task_id = col(reg, idx_task, "task_id")
        tid = col(reg, idx_task, "task_code") or raw_task_id
        if not tid:
            continue
        if raw_task_id:
            id_por_taskid[raw_task_id] = tid
        registros_validos.append(reg)

    # Agrega TASKRSRC opcional por task_id.
    rsrc_por_taskid: dict[str, tuple[float, float, float]] = {}
    rsrc_tbl = tabelas.get("TASKRSRC")
    if rsrc_tbl:
        campos_r, registros_r = rsrc_tbl
        idx_r = {c: i for i, c in enumerate(campos_r)}
        for reg in registros_r:
            raw_tid = col(reg, idx_r, "task_id")
            if not raw_tid:
                continue
            target_q = _para_float(col(reg, idx_r, "target_qty", "target_work_qty"))
            act_reg = _para_float(col(reg, idx_r, "act_reg_qty"))
            act_ot = _para_float(col(reg, idx_r, "act_ot_qty"))
            act_q = act_reg + act_ot
            if act_q == 0.0 and not col(reg, idx_r, "act_reg_qty", "act_ot_qty"):
                act_q = _para_float(col(reg, idx_r, "act_work_qty"))
            rem_q = _para_float(col(reg, idx_r, "remain_qty", "remain_work_qty"))
            prev_t, prev_a, prev_r = rsrc_por_taskid.get(raw_tid, (0.0, 0.0, 0.0))
            rsrc_por_taskid[raw_tid] = (prev_t + target_q, prev_a + act_q, prev_r + rem_q)

    # Lê relacionamentos TASKPRED.
    deps_por_tid: dict[str, list[Dependencia]] = {}
    pred_tbl = tabelas.get("TASKPRED")
    if pred_tbl:
        campos_p, registros_p = pred_tbl
        idx_p = {c: i for i, c in enumerate(campos_p)}
        for reg in registros_p:
            raw_tid = col(reg, idx_p, "task_id")
            tid = id_por_taskid.get(raw_tid, "")
            if not tid:
                continue
            raw_pid = col(reg, idx_p, "pred_task_id")
            if not raw_pid:
                continue
            pid = id_por_taskid.get(raw_pid, raw_pid)
            ptipo_raw = col(reg, idx_p, "pred_type").upper()
            ptipo = _MAPA_LINK_P6.get(ptipo_raw, "FS")
            lag_h = _para_float(col(reg, idx_p, "pred_lag_hr_cnt"))
            deps_por_tid.setdefault(tid, []).append(
                Dependencia(pred_id=pid, tipo=ptipo, lag_dias=_para_dias(lag_h))
            )

    atividades: list[AtividadeCronograma] = []
    for reg in registros_validos:
        raw_tid = col(reg, idx_task, "task_id")
        tid = col(reg, idx_task, "task_code") or raw_tid
        nome = col(reg, idx_task, "task_name")
        task_type = col(reg, idx_task, "task_type").upper()
        status = col(reg, idx_task, "status_code").lower()
        concluida = status == "tk_complete"

        alvo_h = _para_float(col(reg, idx_task, "target_drtn_hr_cnt"))
        remain_raw = col(reg, idx_task, "remain_drtn_hr_cnt")
        if concluida:
            resta_h = 0.0
        else:
            resta_h = _para_float(remain_raw) if remain_raw != "" else alvo_h

        marco = task_type in _TIPOS_MARCO_P6 or (alvo_h == 0.0 and resta_h == 0.0)
        if marco:
            alvo_h = 0.0
            resta_h = 0.0

        folga_h = _para_float(col(reg, idx_task, "total_float_hr_cnt"))
        dur_m_dias = 0.0 if (marco or concluida) else _para_dias(resta_h)
        dur_o, dur_m, dur_p = calcular_pert_padrao(dur_m_dias, marco=marco or concluida)

        r_target, r_act, r_rem = rsrc_por_taskid.get(raw_tid, (0.0, 0.0, 0.0))
        if (r_target + r_act + r_rem) > 0.0:
            baseline_hh = r_target if r_target > 0.0 else (r_act + r_rem)
            hh_realizado = r_act
            hh_restante = 0.0 if concluida else r_rem
        else:
            baseline_hh = alvo_h
            hh_restante = 0.0 if concluida else resta_h
            hh_realizado = max(alvo_h - hh_restante, 0.0)

        deps = deps_por_tid.get(tid, [])
        atividades.append(
            AtividadeCronograma(
                id=tid,
                nome=nome,
                tipo=classificar_tipo(nome, overrides=overrides, task_id=tid),
                predecessoras=";".join(d.to_token() for d in deps),
                folga=_para_dias(folga_h),
                baseline_hh=round(baseline_hh, 2),
                hh_realizado=round(hh_realizado, 2),
                hh_restante=round(hh_restante, 2),
                historico=[],
                dur_o=dur_o,
                dur_m=dur_m,
                dur_p=dur_p,
                dependencias=deps,
                concluida=concluida,
                marco=marco,
                dur_m_explicita=True,
                folga_importada=True,
            )
        )

    return atividades
