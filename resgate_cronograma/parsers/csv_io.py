"""Leitura e escrita do CSV unificado (9 e 12 colunas)."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Mapping, Sequence

from resgate_cronograma.models import (
    HORAS_POR_DIA,
    AtividadeCronograma,
    parse_dependencias,
)
from resgate_cronograma.parsers.classification import classificar_tipo

CAMPOS_CSV = [
    "ID",
    "Nome",
    "Tipo",
    "Predecessoras",
    "FolgaTotal",
    "BaselineHH",
    "HHRealizado",
    "HHRestante",
    "HistoricoDesvios",
    "DurO",
    "DurM",
    "DurP",
]


def calcular_pert_padrao(dur_m: float, marco: bool = False) -> tuple[float, float, float]:
    """Calcula tripla PERT heurística (0.75*m, m, 1.6*m), preservando 0,0,0 para marcos."""
    if marco or dur_m <= 0.0:
        return (0.0, 0.0, 0.0)
    casas = 1 if dur_m >= 0.5 else 2
    return (
        round(0.75 * dur_m, casas),
        round(dur_m, casas),
        round(1.6 * dur_m, casas),
    )


def _float_ou(valor: str | None, padrao: float = 0.0) -> float:
    texto = (valor or "").strip()
    if not texto:
        return padrao
    return float(texto)


def carregar_csv(
    caminho: str | Path,
    overrides: Mapping[str, str] | None = None,
) -> list[AtividadeCronograma]:
    """Carrega CSV de 9 ou 12 colunas (incluindo cabeçalho legado `HHRrestante`)."""
    atividades: list[AtividadeCronograma] = []
    with open(caminho, newline="", encoding="utf-8") as fh:
        leitor = csv.DictReader(fh)
        for linha in leitor:
            tid = str(linha["ID"]).strip()
            nome = str(linha.get("Nome") or "").strip()
            tipo = classificar_tipo(
                nome,
                tipo_explicito=linha.get("Tipo"),
                overrides=overrides,
                task_id=tid,
            )
            preds_raw = (linha.get("Predecessoras") or "").strip()
            deps = parse_dependencias(preds_raw)

            folga_raw = (linha.get("FolgaTotal") or "").strip()
            if folga_raw:
                folga = float(folga_raw.rstrip("dD "))
                folga_importada = True
            else:
                folga = 0.0
                folga_importada = False

            baseline_hh = _float_ou(linha.get("BaselineHH"), 0.0)
            hh_realizado = _float_ou(linha.get("HHRealizado"), 0.0)
            hh_rest_raw = linha.get("HHRestante")
            if hh_rest_raw is None or not str(hh_rest_raw).strip():
                hh_rest_raw = linha.get("HHRrestante")
            hh_restante = _float_ou(hh_rest_raw, 0.0)

            hist_raw = (linha.get("HistoricoDesvios") or "").strip()
            historico = [float(x.strip()) for x in hist_raw.split(";") if x.strip()]

            dur_m_raw = (linha.get("DurM") or "").strip()
            if dur_m_raw:
                dur_m = float(dur_m_raw)
                marco_heuristico = dur_m <= 0.0 and _float_ou(linha.get("DurO"), 0.0) == 0.0 and _float_ou(linha.get("DurP"), 0.0) == 0.0
                o_def, _m_def, p_def = calcular_pert_padrao(dur_m, marco=marco_heuristico)
                dur_o = _float_ou(linha.get("DurO"), o_def)
                dur_p = _float_ou(linha.get("DurP"), p_def)
                dur_m_explicita = True
                marco = dur_o == 0.0 and dur_m == 0.0 and dur_p == 0.0
            else:
                hh_ref = hh_restante if hh_restante > 0 else baseline_hh
                m_est = hh_ref / HORAS_POR_DIA if hh_ref > 0 else 0.0
                marco = m_est <= 0.0
                dur_o, dur_m, dur_p = calcular_pert_padrao(m_est, marco=marco)
                dur_m_explicita = False

            atividades.append(
                AtividadeCronograma(
                    id=tid,
                    nome=nome,
                    tipo=tipo,
                    predecessoras=preds_raw,
                    folga=folga,
                    baseline_hh=baseline_hh,
                    hh_realizado=hh_realizado,
                    hh_restante=hh_restante,
                    historico=historico,
                    dur_o=dur_o,
                    dur_m=dur_m,
                    dur_p=dur_p,
                    dependencias=deps,
                    concluida=(hh_restante == 0.0 and baseline_hh > 0.0 and hh_realizado >= baseline_hh),
                    marco=marco,
                    dur_m_explicita=dur_m_explicita,
                    folga_importada=folga_importada,
                    grupo_risco=(linha.get("GrupoRisco") or "").strip(),
                )
            )
    return atividades


def atividade_para_linha_csv(
    atividade: AtividadeCronograma,
    incluir_grupo_risco: bool = False,
) -> dict[str, object]:
    """Serializa uma `AtividadeCronograma` no dicionário do CSV unificado."""
    preds_str = (
        ";".join(d.to_token() for d in atividade.dependencias)
        if atividade.dependencias
        else atividade.predecessoras
    )
    hist_str = ";".join(f"{v:g}" for v in atividade.historico)
    linha: dict[str, object] = {
        "ID": atividade.id,
        "Nome": atividade.nome,
        "Tipo": atividade.tipo,
        "Predecessoras": preds_str,
        "FolgaTotal": f"{atividade.folga:g}d",
        "BaselineHH": f"{atividade.baseline_hh:g}",
        "HHRealizado": f"{atividade.hh_realizado:g}",
        "HHRestante": f"{atividade.hh_restante:g}",
        "HistoricoDesvios": hist_str,
        "DurO": f"{atividade.dur_o:g}",
        "DurM": f"{atividade.dur_m:g}",
        "DurP": f"{atividade.dur_p:g}",
    }
    if incluir_grupo_risco:
        linha["GrupoRisco"] = atividade.grupo_risco
    return linha


def escrever_csv(atividades: Sequence[AtividadeCronograma], saida: str | Path) -> None:
    """Escreve atividades normalizadas em arquivo CSV UTF-8 (12 colunas, ou 13 se houver `GrupoRisco`)."""
    tem_grupo = any(bool(a.grupo_risco.strip()) for a in atividades)
    campos = CAMPOS_CSV + ["GrupoRisco"] if tem_grupo else CAMPOS_CSV
    with open(saida, "w", newline="", encoding="utf-8") as fh:
        escritor = csv.DictWriter(fh, fieldnames=campos)
        escritor.writeheader()
        for atividade in atividades:
            escritor.writerow(atividade_para_linha_csv(atividade, incluir_grupo_risco=tem_grupo))
