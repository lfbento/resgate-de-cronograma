"""Leitor de cronogramas: Primavera P6 (.xer) e MS Project (.xml MSPDI).

Converte qualquer um dos formatos no CSV unificado que alimenta o motor
cpm_pid.py / monte_carlo.py (colunas: ID, Nome, Tipo, Predecessoras,
FolgaTotal, BaselineHH, HHRealizado, HHRestante, HistoricoDesvios,
DurO, DurM, DurP).

Formatos suportados:
  • .xer  — Primavera P6 (texto %T/%F/%R). Tabelas lidas: TASK, TASKPRED.
    Durações/folgas entram em HORAS (XER padrão); conversão: 8h = 1 dia útil.
  • .xml  — MS Project exportado como MSPDI (Arquivo → Salvar como → XML).
    O .mpp binário NÃO é lido: exporte primeiro como XML.
    Predecessors via PredecessorLink (Type: 0=FS, 1=SS, 2=FF, 3=SF).

Heurísticas documentadas (override depois):
  • Tipo Interna/Externa: por palavras-chave (compra, aprovação, entrega,
    inspeção, fornecedor, cliente, terceiro → Externa). Corrija no CSV depois.
  • PERT (DurO/DurM/DurP): DurM = duração restante (ou alvo);
    DurO = 0,75 × DurM; DurP = 1,6 × DurM. Ajuste com histórico real.
  • HistoricoDesvios: vazio (o motor aceita; preencha se tiver medições).

Uso:
  python3 ler_cronograma.py obra.xer  -o saida.csv
  python3 ler_cronograma.py cronograma.xml -o saida.csv
"""
from __future__ import annotations

import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HORAS_POR_DIA = 8.0

CHAVES_EXTERNAS = ("compra", "aprova", "entrega", "inspe", "fornecedor",
                   "cliente", "terceir", "espera", "documento", "válvula", "valvula")


# ── helpers ───────────────────────────────────────────────────────────────
def _para_dias(horas: float) -> float:
    return round(horas / HORAS_POR_DIA, 1)


def _tipo(nome: str) -> str:
    n = (nome or "").lower()
    return "Externa" if any(k in n for k in CHAVES_EXTERNAS) else "Interna"


def _pert(dur_m: float) -> tuple[float, float, float]:
    dur_m = max(dur_m, 0.5)
    return round(0.75 * dur_m, 1), round(dur_m, 1), round(1.6 * dur_m, 1)


def _parse_iso_dur(s: str) -> float:
    """ISO 8601 → DIAS ÚTEIS (PT320H = 40d úteis a 8h; aceita PT-64H)."""
    if not s:
        return 0.0
    m = re.match(r"P(?:(-?\d+(?:\.\d+)?)D)?T?(?:(-?\d+(?:\.\d+)?)H)?"
                 r"(?:(-?\d+(?:\.\d+)?)M)?(?:(-?\d+(?:\.\d+)?)S)?", s.strip())
    if not m or not any(m.groups()):
        try:
            return float(s)
        except ValueError:
            return 0.0
    d, h, mi, se = (float(x) if x else 0.0 for x in m.groups())
    return d + h / 8 + mi / 480 + se / 28800  # 8h = 1 dia útil


def _linhas_csv(linhas: list[dict], saida: str) -> None:
    campos = ["ID", "Nome", "Tipo", "Predecessoras", "FolgaTotal", "BaselineHH",
              "HHRealizado", "HHRestante", "HistoricoDesvios", "DurO", "DurM", "DurP"]
    with open(saida, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        w.writeheader()
        for r in linhas:
            w.writerow(r)
    print(f"✓ {len(linhas)} atividades → {saida}")


# ── Primavera P6 (.xer) ───────────────────────────────────────────────────
def parse_xer(caminho: str) -> list[dict]:
    tabelas: dict[str, tuple[list[str], list[list[str]]]] = {}
    tabela = None
    for linha in Path(caminho).read_text(encoding="utf-8", errors="replace").splitlines():
        partes = linha.split("\t")
        if partes[0] == "%T":
            tabela = partes[1].strip()
            tabelas[tabela] = [[], []]
        elif partes[0] == "%F" and tabela:
            tabelas[tabela][0] = [p.strip() for p in partes[1:]]
        elif partes[0] == "%R" and tabela:
            tabelas[tabela][1].append(partes[1:])
        elif partes[0] == "%E":
            tabela = None

    task = tabelas.get("TASK")
    if not task:
        raise SystemExit("ERRO: tabela TASK não encontrada — arquivo .xer válido?")
    campos, registros = task
    idx = {c: i for i, c in enumerate(campos)}

    def col(reg: list[str], *nomes: str) -> str:
        for n in nomes:
            i = idx.get(n)
            if i is not None and i < len(reg):
                return reg[i]
        return ""

    preds_por_tarefa: dict[str, list[str]] = {}
    tp = tabelas.get("TASKPRED")
    if tp:
        campos_p, registros_p = tp
        ip = {c: i for i, c in enumerate(campos_p)}
        id_por_taskid = {}
        for reg in registros:
            id_por_taskid[col(reg, "task_id")] = col(reg, "task_code") or col(reg, "task_id")
        tipo_map = {"PR_FS": "FS", "PR_SS": "SS", "PR_FF": "FF", "PR_SF": "SF"}
        for reg in registros_p:
            tid = id_por_taskid.get(reg[ip.get("task_id", -1)] if "task_id" in ip else "", "")
            pid = id_por_taskid.get(reg[ip.get("pred_task_id", -1)] if "pred_task_id" in ip else "", "")
            ptipo = tipo_map.get(col(reg, "pred_type") if False else
                                 (reg[ip["pred_type"]] if "pred_type" in ip and ip["pred_type"] < len(reg) else ""), "FS")
            lag_h = float(reg[ip["pred_lag_hr_cnt"]] or 0) if "pred_lag_hr_cnt" in ip and ip["pred_lag_hr_cnt"] < len(reg) else 0
            if tid and pid:
                preds_por_tarefa.setdefault(tid, []).append(f"{pid}{ptipo}{_para_dias(lag_h):+g}d")

    linhas = []
    for reg in registros:
        nome = col(reg, "task_name")
        tid = col(reg, "task_code") or col(reg, "task_id")
        status = (col(reg, "status_code") or "").lower()
        concluido = status in ("tk_complete",)
        alvo_h = float(col(reg, "target_drtn_hr_cnt") or 0)
        resta_h = float(col(reg, "remain_drtn_hr_cnt") or alvo_h)
        real_h = max(alvo_h - resta_h, 0.0)
        folga_h = float(col(reg, "total_float_hr_cnt") or 0)
        o, m, p = _pert(_para_dias(resta_h if not concluido else alvo_h))
        linhas.append({
            "ID": tid, "Nome": nome, "Tipo": _tipo(nome),
            "Predecessoras": ";".join(preds_por_tarefa.get(tid, [])),
            "FolgaTotal": f"{_para_dias(folga_h):g}d",
            "BaselineHH": f"{alvo_h:g}", "HHRealizado": f"{real_h:g}",
            "HHRestante": f"{resta_h:g}", "HistoricoDesvios": "",
            "DurO": o, "DurM": m, "DurP": p,
        })
    return linhas


# ── MS Project (.xml MSPDI) ───────────────────────────────────────────────
def parse_mspdi(caminho: str) -> list[dict]:
    ns = ""
    raiz = ET.parse(caminho).getroot()
    m = re.match(r"\{.*\}", raiz.tag)
    if m:
        ns = m.group(0)
    tarefas = raiz.iter(f"{ns}Task")
    mapa_uid, dados = {}, []
    for t in tarefas:
        def txt(tag):
            el = t.find(f"{ns}{tag}")
            return el.text if el is not None and el.text else ""
        uid = txt("UID")
        nome = txt("Name")
        dur_m = _parse_iso_dur(txt("RemainingDuration") or txt("Duration"))
        dur_alvo = _parse_iso_dur(txt("Duration"))
        folga_d = _parse_iso_dur(txt("TotalSlack"))
        concluido = (txt("PercentComplete") or "0").startswith("100")
        mapa_uid[uid] = txt("TaskID") or uid
        dados.append((uid, nome, dur_m, dur_alvo, folga_d, concluido,
                      [pl for pl in t.iter(f"{ns}PredecessorLink")] if False else
                      [(pl.findtext(f"{ns}PredecessorUID") or "",
                        pl.findtext(f"{ns}Type") or "0",
                        pl.findtext(f"{ns}Lag") or "0")
                       for pl in t.iter(f"{ns}PredecessorLink")]))
    linhas = []
    for uid, nome, dur_m, dur_alvo, folga_d, concluido, pls in dados:
        preds = []
        for puid, ptipo, plag in pls:
            pid = mapa_uid.get(puid, puid)
            tipo = {"0": "FS", "1": "SS", "2": "FF", "3": "SF"}.get(ptipo, "FS")
            lag_d = _parse_iso_dur(plag)
            preds.append(f"{pid}{tipo}{lag_d:+g}d")
        o, mm, p = _pert(dur_m if not concluido else dur_alvo)
        linhas.append({
            "ID": mapa_uid.get(uid, uid), "Nome": nome, "Tipo": _tipo(nome),
            "Predecessoras": ";".join(preds),
            "FolgaTotal": f"{folga_d:g}d",
            "BaselineHH": f"{dur_alvo * HORAS_POR_DIA:g}",
            "HHRealizado": f"{max(dur_alvo - dur_m, 0) * HORAS_POR_DIA:g}",
            "HHRestante": f"{dur_m * HORAS_POR_DIA:g}",
            "HistoricoDesvios": "", "DurO": o, "DurM": mm, "DurP": p,
        })
    return linhas


def main():
    if len(sys.argv) < 3 or "-o" not in sys.argv:
        print(__doc__)
        sys.exit(1)
    arquivo = sys.argv[1]
    saida = sys.argv[sys.argv.index("-o") + 1]
    if arquivo.lower().endswith(".xer"):
        linhas = parse_xer(arquivo)
    elif arquivo.lower().endswith(".xml"):
        linhas = parse_mspdi(arquivo)
    else:
        raise SystemExit("ERRO: formato não suportado (use .xer ou .xml MSPDI)")
    _linhas_csv(linhas, saida)


if __name__ == "__main__":
    main()
