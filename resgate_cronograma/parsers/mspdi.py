"""Leitor endurecido de MS Project XML (MSPDI)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Mapping

from resgate_cronograma.models import (
    AtividadeCronograma,
    Dependencia,
    LinkType,
)
from resgate_cronograma.parsers.classification import classificar_tipo
from resgate_cronograma.parsers.csv_io import calcular_pert_padrao

_ISO_DUR_RE = re.compile(
    r"^([+-])?P(?:([+-]?\d+(?:\.\d+)?)D)?T?(?:([+-]?\d+(?:\.\d+)?)H)?"
    r"(?:([+-]?\d+(?:\.\d+)?)M)?(?:([+-]?\d+(?:\.\d+)?)S)?$",
    re.IGNORECASE,
)

# Mapeamento oficial MS Project MSPDI PredecessorLink/Type (0=FF, 1=FS, 2=SF, 3=SS).
_MAPA_TIPO_MSPDI: dict[str, LinkType] = {
    "0": "FF",
    "1": "FS",
    "2": "SF",
    "3": "SS",
}
_FORMATOS_LAG_PERCENTUAL = {"19", "20", "51", "52"}


def _parse_duracao_dias(
    texto: str,
    horas_por_dia: float = 8.0,
    decimos_por_dia: float = 4800.0,
) -> float:
    """Converte duração ISO 8601 (inclusive negativa) ou décimos de minuto para dias úteis."""
    s = (texto or "").strip()
    if not s:
        return 0.0
    if "P" in s.upper():
        match = _ISO_DUR_RE.match(s)
        if match and any(match.groups()[1:]):
            sinal = -1.0 if match.group(1) == "-" else 1.0
            d, h, mi, se = (float(x) if x else 0.0 for x in match.groups()[1:])
            dias = d + (h / horas_por_dia) + (mi / (horas_por_dia * 60.0)) + (se / (horas_por_dia * 3600.0))
            return sinal * dias
    try:
        return float(s) / decimos_por_dia
    except ValueError:
        return 0.0


def parse_mspdi(
    caminho: str | Path,
    overrides: Mapping[str, str] | None = None,
) -> list[AtividadeCronograma]:
    """Lê tarefas executáveis e marcos de um XML MSPDI do MS Project."""
    raiz = ET.parse(caminho).getroot()
    ns_match = re.match(r"\{.*\}", raiz.tag)
    ns = ns_match.group(0) if ns_match else ""

    def txt_el(el: ET.Element, tag: str) -> str:
        filho = el.find(f"{ns}{tag}")
        return filho.text.strip() if filho is not None and filho.text else ""

    minutes_per_day = float(txt_el(raiz, "MinutesPerDay") or 480.0)
    if minutes_per_day <= 0:
        minutes_per_day = 480.0
    horas_por_dia = minutes_per_day / 60.0
    decimos_por_dia = minutes_per_day * 10.0

    # Descobre IDs de campos customizados PERT (Duration1/Optimistic e Duration3/Pessimistic).
    opt_field_ids: set[str] = set()
    pess_field_ids: set[str] = set()
    ext_defs = raiz.find(f"{ns}ExtendedAttributes")
    if ext_defs is not None:
        for ea_def in ext_defs.findall(f"{ns}ExtendedAttribute"):
            fid = txt_el(ea_def, "FieldID")
            fname = txt_el(ea_def, "FieldName").lower()
            falias = txt_el(ea_def, "Alias").lower()
            if not fid:
                continue
            if "optimistic" in falias or fname == "duration1":
                opt_field_ids.add(fid)
            elif "pessimistic" in falias or fname == "duration3":
                pess_field_ids.add(fid)

    # Primeira passada: coleta tarefas filtrando UID=0 e Summary=1, e guarda durações por UID.
    mapa_uid_para_id: dict[str, str] = {}
    duracao_por_uid: dict[str, float] = {}
    brutas: list[
        tuple[
            str,
            str,
            str,
            float,
            float,
            float,
            float,
            float,
            float,
            bool,
            bool,
            float | None,
            float | None,
            list[tuple[str, str, str, str]],
        ]
    ] = []

    for tarefa in raiz.iter(f"{ns}Task"):
        uid = txt_el(tarefa, "UID")
        if uid == "0":
            continue
        summary_flag = txt_el(tarefa, "Summary").lower()
        if summary_flag in ("1", "true"):
            continue

        nome = txt_el(tarefa, "Name")
        tid = txt_el(tarefa, "TaskID") or uid
        if not tid:
            continue

        dur_alvo = _parse_duracao_dias(txt_el(tarefa, "Duration"), horas_por_dia, decimos_por_dia)
        pct_raw = txt_el(tarefa, "PercentComplete") or "0"
        pct = min(max(float(pct_raw), 0.0), 100.0)
        concluida = pct >= 100.0

        rem_raw = txt_el(tarefa, "RemainingDuration")
        if rem_raw:
            dur_m = _parse_duracao_dias(rem_raw, horas_por_dia, decimos_por_dia)
        else:
            dur_m = dur_alvo * max(0.0, 1.0 - pct / 100.0)
        if concluida:
            dur_m = 0.0

        folga_d = _parse_duracao_dias(txt_el(tarefa, "TotalSlack"), horas_por_dia, decimos_por_dia)

        milestone_flag = txt_el(tarefa, "Milestone").lower() in ("1", "true")
        marco = milestone_flag or (dur_alvo == 0.0 and dur_m == 0.0)

        work_h = _parse_duracao_dias(txt_el(tarefa, "Work"), horas_por_dia, decimos_por_dia) * horas_por_dia
        actual_work_h = _parse_duracao_dias(txt_el(tarefa, "ActualWork"), horas_por_dia, decimos_por_dia) * horas_por_dia
        rem_work_h = _parse_duracao_dias(txt_el(tarefa, "RemainingWork"), horas_por_dia, decimos_por_dia) * horas_por_dia

        opt_dias: float | None = None
        pess_dias: float | None = None
        if opt_field_ids or pess_field_ids:
            for ea in tarefa.findall(f"{ns}ExtendedAttribute"):
                fid = txt_el(ea, "FieldID")
                val_txt = txt_el(ea, "Value")
                if not fid or not val_txt:
                    continue
                if fid in opt_field_ids:
                    opt_dias = _parse_duracao_dias(val_txt, horas_por_dia, decimos_por_dia)
                elif fid in pess_field_ids:
                    pess_dias = _parse_duracao_dias(val_txt, horas_por_dia, decimos_por_dia)

        links: list[tuple[str, str, str, str]] = []
        for pl in tarefa.findall(f"{ns}PredecessorLink"):
            puid = txt_el(pl, "PredecessorUID")
            if not puid:
                continue
            ptipo = txt_el(pl, "Type") or "1"
            plag = txt_el(pl, "Lag") or "0"
            plag_fmt = txt_el(pl, "LagFormat")
            links.append((puid, ptipo, plag, plag_fmt))

        if uid:
            mapa_uid_para_id[uid] = tid
            duracao_por_uid[uid] = dur_alvo if dur_alvo > 0 else dur_m

        brutas.append(
            (
                uid,
                tid,
                nome,
                dur_alvo,
                dur_m,
                folga_d,
                work_h,
                actual_work_h,
                rem_work_h,
                concluida,
                marco,
                opt_dias,
                pess_dias,
                links,
            )
        )

    atividades: list[AtividadeCronograma] = []
    for (
        _uid,
        tid,
        nome,
        dur_alvo,
        dur_m,
        folga_d,
        work_h,
        actual_work_h,
        rem_work_h,
        concluida,
        marco,
        opt_dias,
        pess_dias,
        links,
    ) in brutas:
        deps: list[Dependencia] = []
        for puid, ptipo, plag, plag_fmt in links:
            pid = mapa_uid_para_id.get(puid, puid)
            tipo = _MAPA_TIPO_MSPDI.get(ptipo, "FS")
            if plag_fmt in _FORMATOS_LAG_PERCENTUAL:
                pct_str = plag.strip().rstrip("%")
                pct_val = float(pct_str) if pct_str else 0.0
                dur_pred = duracao_por_uid.get(puid, 0.0)
                lag_d = round(dur_pred * pct_val / 100.0, 2)
            else:
                lag_d = round(_parse_duracao_dias(plag, horas_por_dia, decimos_por_dia), 2)
            deps.append(Dependencia(pred_id=pid, tipo=tipo, lag_dias=lag_d))

        if (work_h + actual_work_h + rem_work_h) > 0.0:
            baseline_hh = work_h if work_h > 0.0 else (actual_work_h + rem_work_h)
            hh_realizado = actual_work_h
            hh_restante = 0.0 if concluida else rem_work_h
        else:
            baseline_hh = dur_alvo * horas_por_dia
            hh_restante = 0.0 if concluida else dur_m * horas_por_dia
            hh_realizado = max(baseline_hh - hh_restante, 0.0)

        if marco or concluida:
            dur_o, dur_m_val, dur_p = (0.0, 0.0, 0.0)
        else:
            casas = 1 if dur_m >= 0.5 else 2
            dur_m_val = round(dur_m, casas)
            o_def, _m_def, p_def = calcular_pert_padrao(dur_m_val, marco=False)
            fator_rem = (dur_m / dur_alvo) if dur_alvo > 0.0 else 1.0
            if opt_dias is not None and opt_dias > 0.0:
                dur_o = round(max(0.0, min(opt_dias * fator_rem, dur_m_val)), casas)
                dur_o = min(dur_o, dur_m_val)
            else:
                dur_o = o_def
            if pess_dias is not None and pess_dias > 0.0:
                dur_p = round(max(pess_dias * fator_rem, dur_m_val), casas)
                dur_p = max(dur_p, dur_m_val)
            else:
                dur_p = p_def

        atividades.append(
            AtividadeCronograma(
                id=tid,
                nome=nome,
                tipo=classificar_tipo(nome, overrides=overrides, task_id=tid),
                predecessoras=";".join(d.to_token() for d in deps),
                folga=round(folga_d, 2),
                baseline_hh=round(baseline_hh, 2),
                hh_realizado=round(hh_realizado, 2),
                hh_restante=round(hh_restante, 2),
                historico=[],
                dur_o=dur_o,
                dur_m=dur_m_val,
                dur_p=dur_p,
                dependencias=deps,
                concluida=concluida,
                marco=marco,
                dur_m_explicita=True,
                folga_importada=True,
            )
        )

    return atividades
