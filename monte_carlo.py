"""Monte Carlo + CPM + PID — camada probabilística sobre o motor prescritivo.

Como se combinam (sensor × atuador):
  • Monte Carlo = SENSOR: amostra durações (PERT o/m/p) por atividade, roda a
    rede N vezes e devolve:
      - P50/P80/P90 da data de término (probabilidade de prazo)
      - ÍNDICE DE CRITICIDADE = fração das simulações em que a atividade está
        no caminho crítico (quem é gargalo DE VERDADE, não só na planilha)
    Esse índice reordena o "ONDE agir" do PID: crashing só em atividade com
    criticidade alta (ex.: > 40%).
  • PID = ATUADOR: o alvo de recuperação deixa de ser "8 dias" e passa a ser
    a DISTÂNCIA até o percentil alvo (ex.: P80) — uk fecha a lacuna
    probabilística, não só o desvio pontual.

Entrada: CSV com colunas extras DurO,DurM,DurP (PERT, em dias) além do
         CSV base do cpm_pid (ID, Nome, Tipo, Predecessoras, FolgaTotal,
         BaselineHH, HHRealizado, HHRestante, HistoricoDesvios).
Uso:    python3 monte_carlo.py cronograma.csv [--sims 10000] [--prazo 45]
        [--alvo 80]  (percentil alvo do compromisso)
"""
from __future__ import annotations

import argparse
import csv
import random
import re
from dataclasses import dataclass

import math

from cpm_pid import prescrever, pid_por_frente, Frente


# ──────────────────────────────────────────────────────────────────────────
@dataclass
class Atividade:
    id: str
    nome: str
    tipo: str
    preds: list            # [(id_pred, tipo: 'FS'|'SS', lag_dias)]
    dur_o: float
    dur_m: float
    dur_p: float

    def amostrar(self, rng: random.Random) -> float:
        """PERT (beta) — moda puxada pelo mais provável."""
        if self.dur_p <= self.dur_o:
            return self.dur_m
        a, m, b = self.dur_o, self.dur_m, self.dur_p
        mu = (a + 4 * m + b) / 6
        alpha = max(((mu - a) * (2 * m - a - b)) / max((m - mu) * (b - a), 1e-9), 0.5)
        beta = max(alpha * (b - mu) / max(mu - a, 1e-9), 0.5)
        return a + (b - a) * rng.betavariate(alpha, beta)


def parse_preds(s: str) -> list:
    out = []
    for tok in (s or "").split(";"):
        tok = tok.strip()
        if not tok:
            continue
        m = re.match(r"(\d+)\s*(FS|SS)?\s*([+-]\s*\d+(?:\.\d+)?)?d?", tok, re.I)
        if m:
            out.append((m.group(1), (m.group(2) or "FS").upper(),
                        float((m.group(3) or "0").replace(" ", ""))))
    return out


def simular(ats: dict[str, Atividade], prazo: float, n_sims: int, alvo: float,
            seed: int = 42):
    rng = random.Random(seed)
    atrasos, critica_qtd = [], {k: 0 for k in ats}
    succs = {k: [] for k in ats}
    for k, a in ats.items():
        for p, tipo, lag in a.preds:
            if p in ats:
                succs[p].append((k, tipo, lag))
    for _ in range(n_sims):
        dur = {k: a.amostrar(rng) for k, a in ats.items()}
        ini, fim, ordem = {}, {}, []
        restantes = dict(ats)
        while restantes:
            progrediu = False
            for k, a in list(restantes.items()):
                if all(p in fim for p, _t, _l in a.preds):
                    ini[k] = 0.0
                    for p, tipo, lag in a.preds:
                        base = fim[p] if tipo == "FS" else ini[p]
                        ini[k] = max(ini[k], base + lag)
                    fim[k] = ini[k] + dur[k]
                    ordem.append(k)
                    del restantes[k]
                    progrediu = True
            if not progrediu:          # ciclo ou predecessor inexistente
                for k in restantes:
                    fim[k] = fim.get(k, 0.0) + dur[k]
                    ordem.append(k)
                break
        fim_obra = max(fim.values())
        atrasos.append(fim_obra - prazo)
        # CRITICIDADE real = folga zero (passe pra trás), não só "termina no fim"
        lf, ls = {}, {}
        for k in reversed(ordem):
            if not succs[k]:
                lf[k] = fim_obra
            else:
                cand = []
                for s, tipo, lag in succs[k]:
                    if tipo == "FS":
                        cand.append(ls[s] - lag)
                    else:  # SS: atrasa o início do sucessor limita o início
                        cand.append(ls[s] - lag + dur[k])
                lf[k] = min(cand)
            ls[k] = lf[k] - dur[k]
        for k in ats:
            if abs(ls[k] - ini[k]) < 1e-6:
                critica_qtd[k] += 1
    atrasos.sort()
    def pct(p):
        return atrasos[min(int(p / 100 * len(atrasos)), len(atrasos) - 1)]
    indices = {k: v / n_sims for k, v in critica_qtd.items()}
    return {"P50": pct(50), "P80": pct(80), "P90": pct(90),
            "P(alvo)": pct(alvo), "criticidade": indices}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--sims", type=int, default=10000)
    ap.add_argument("--prazo", type=float, required=True, help="prazo contratual (dias)")
    ap.add_argument("--alvo", type=float, default=80, help="percentil do compromisso")
    args = ap.parse_args()

    ats: dict[str, Atividade] = {}
    frentes: list[Frente] = []
    with open(args.csv, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            ats[r["ID"]] = Atividade(
                id=r["ID"], nome=r["Nome"], tipo=r["Tipo"],
                preds=parse_preds(r.get("Predecessoras", "")),
                dur_o=float(r["DurO"]), dur_m=float(r["DurM"]), dur_p=float(r["DurP"]))
            hist = [float(x) for x in r.get("HistoricoDesvios", "").split(";") if x.strip()]
            frentes.append(Frente(
                id=r["ID"], nome=r["Nome"], tipo=r["Tipo"],
                predecessoras=r.get("Predecessoras", ""),
                folga=float(r["FolgaTotal"].replace("d", "")),
                baseline_hh=float(r["BaselineHH"]),
                hh_realizado=float(r["HHRealizado"]),
                hh_restante=float(r.get("HHRestante") or 0),
                historico=hist))

    res = simular(ats, args.prazo, args.sims, args.alvo)
    print("═" * 62)
    print(f"MONTE CARLO — {args.sims:,} simulações | prazo contratual: {args.prazo:.0f}d")
    print("═" * 62)
    print(f"  Término P50: {args.prazo + res['P50']:.1f}d  (50% de chance até aqui)")
    print(f"  Término P80: {args.prazo + res['P80']:.1f}d  (compisso conservador)")
    print(f"  Término P90: {args.prazo + res['P90']:.1f}d")
    print(f"  Atraso esperado P{args.alvo:.0f}: {res['P(alvo)']:+.1f}d")
    print()
    print("ÍNDICE DE CRITICIDADE (fração das simulações no caminho crítico):")
    for k, v in sorted(res["criticidade"].items(), key=lambda kv: -kv[1]):
        barra = "█" * int(v * 20)
        print(f"  ID {k:>3} ({ats[k].nome[:28]:28s}) {v:5.0%} {barra}")
    print()
    print("─" * 62)
    print("PRESCRIÇÃO INTEGRADA (PID × criticidade Monte Carlo):")
    for f in sorted(frentes, key=lambda f: -res["criticidade"].get(f.id, 0)):
        ic = res["criticidade"].get(f.id, 0)
        if ic < 0.40:
            continue
        uk, det = pid_por_frente(f)
        uk_ajustado = uk * (0.5 + 0.5 * ic)   # pondera pelo risco de ser gargalo
        print(f"\n▸ ID {f.id} — {f.nome} | criticidade MC: {ic:.0%}")
        print(f"  PID: uk={det.get('uk_hh','—')} HH → ajustado pelo risco: "
              f"{uk_ajustado:.1f} HH")
        for a in prescrever(f, uk, det):
            print(f"  {a}")
    print()
    print("─" * 62)
    print("Lógica: só age em atividade com criticidade ≥ 40% — crashing em")
    print("frente que é gargalo em só 5% das simulações é dinheiro queimado.")


if __name__ == "__main__":
    main()
