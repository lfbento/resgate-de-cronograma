"""Sistema Prescritivo de Recuperação de Cronogramas — CPM + PID discreto.

Implementa a especificação técnica recebida (4 páginas):
  Etapa A — filtro de ONDE agir (caminho crítico: folga ≤ 0; alerta ≤ 5 dias)
  Etapa B — QUANTO acelerar: PID discreto por frente crítica
            uk = Kp·ek + Ki·Σej + Kd·(ek − ek−1)
            com saturação (teto físico) e anti-windup
  Etapa C — matriz de regras → ações práticas ordenadas por impacto

Entrada: CSV com as 8 colunas da especificação:
  ID,Nome,Tipo,Predecessoras,FolgaTotal,BaselineHH,HHRealizado,HHRrestante
Uso: python3 cpm_pid.py cronograma.csv [--ciclo N]
"""
from __future__ import annotations

import csv
import sys
from dataclasses import dataclass, field

# ── Calibração operacional (seção 4 da especificação) ─────────────────────
KP = 0.5          # ganho proporcional
KI = 0.15         # ganho integral (consumo do passivo)
KD = 0.25         # ganho derivativo (freio preditivo / anti-overshoot)
TETO_SATURACAO = 0.35     # teto físico: +35% de horas-homem por frente
DEADBAND_PCT = 0.015      # ignora variações residuais < 1,5%
FOLGA_CRITICA = 0         # ≤ 0 dias = caminho crítico
FOLGA_ALERTA = 5          # ≤ 5 dias = zona de risco iminente


@dataclass
class Frente:
    id: str
    nome: str
    tipo: str                 # Interna | Externa
    predecessoras: str
    folga: float              # dias úteis (negativo = atraso)
    baseline_hh: float
    hh_realizado: float
    hh_restante: float
    historico: list = field(default_factory=list)  # desvios por ciclo (ek)

    @property
    def criticidade(self) -> str:
        if self.folga <= FOLGA_CRITICA:
            return "CRÍTICA"
        if self.folga <= FOLGA_ALERTA:
            return "ALERTA"
        return "OK"

    @property
    def eh_interna(self) -> bool:
        return self.tipo.strip().lower().startswith("int")


def pid_por_frente(f: Frente) -> tuple[float, dict]:
    """PID discreto por frente crítica. Retorna (uk, detalhes)."""
    e = f.historico
    if not e:
        return 0.0, {}
    ek = e[-1]
    if abs(ek) / max(f.baseline_hh, 1) < DEADBAND_PCT and len(e) > 1:
        return 0.0, {"deadband": True}
    soma = sum(e)
    delta = ek - e[-2] if len(e) > 1 else 0.0
    p, i, d = KP * ek, KI * soma, KD * delta
    uk = p + i + d
    # saturação + anti-windup: congela o termo integral no teto
    if abs(uk) > TETO_SATURACAO * f.baseline_hh:
        uk = TETO_SATURACAO * f.baseline_hh * (1 if uk > 0 else -1)
        i = 0.0  # anti-windup: passivo congelado — gargalo físico, não de força
        windup = True
    else:
        windup = False
    return uk, {"P": round(p, 1), "I": round(i, 1), "D": round(d, 1),
                "uk_hh": round(uk, 1), "saturado": windup}


def prescrever(f: Frente, uk: float, det: dict) -> list[str]:
    """Etapa C — matriz de regras: ações ordenadas do menor para o maior custo."""
    acoes = []
    tem_predecessora_fs = "FS" in (f.predecessoras or "") or f.predecessoras.strip() != ""
    if f.criticidade == "OK":
        return acoes
    if det.get("deadband"):
        acoes.append("• Variação residual (deadband): nenhuma ação — monitorar.")
        return acoes
    if not det:
        acoes.append("• PID inativo: sem histórico de desvios ainda "
                     "(preencha HistoricoDesvios com as medições semanais).")
        return acoes
    if det.get("saturado") and f.eh_interna:
        acoes.append("• Teto físico atingido: abrir 2º turno ou terceirizar etapa "
                     "(ex.: jateamento externo) — hora extra no mesmo turno não resolve.")
    if f.eh_interna:
        hh_extra = max(0.0, uk)
        pessoas = round(hh_extra / 44, 1)  # ~44h/mês por profissional extra
        acoes.append(f"• Crashing calibrado: +{pessoas} profissional(is) ou "
                     f"{hh_extra/20:.1f}h extras/dia (~{hh_extra:.0f} HH) "
                     f"só nas tarefas críticas desta frente.")
        if tem_predecessora_fs:
            acoes.append("• Fast-tracking: fracionar lotes e sobrepor sucessora "
                         "(FS → SS com defasagem), liberando subconjuntos parciais.")
    else:
        acoes.append("• Externa/Espera: hora extra é INÓCUA — diligenciar fornecedor, "
                     "cobrar aprovação formal ou re-sequenciar montagem.")
    if f.folga <= FOLGA_ALERTA and not f.eh_interna:
        acoes.append(f"• ALERTA: folga de apenas {f.folga:.0f}d — prazo pode anular "
                     "o ganho obtido na fábrica.")
    return acoes


def relatorio(frentes: list[Frente]) -> str:
    linhas = ["═" * 62,
              "DIAGNÓSTICO DA MALHA CRÍTICA — CPM + PID",
              "═" * 62]
    criticas = [f for f in frentes if f.criticidade != "OK"]
    if not criticas:
        return "\n".join(linhas + ["Nenhuma frente crítica. Cronograma saudável."])
    for f in sorted(criticas, key=lambda x: x.folga):
        uk, det = pid_por_frente(f)
        linhas += ["",
                   f"▸ [{f.criticidade}] ID {f.id} — {f.nome}",
                   f"  Tipo: {f.tipo} | Folga: {f.folga:+.0f}d | "
                   f"HH: {f.hh_realizado:.0f}/{f.baseline_hh:.0f} "
                   f"(restam {f.hh_restante:.0f})",
                   f"  PID: P={det.get('P','—')} I={det.get('I','—')} "
                   f"D={det.get('D','—')} → uk={det.get('uk_hh','—')} HH"]
        for a in prescrever(f, uk, det):
            linhas.append(f"  {a}")
    linhas += ["", "─" * 62,
               "Ordem de impacto: 1) Fast-tracking (menor custo) → "
               "2) Crashing (HH) → 3) Turno/Terceirização → 4) Diligenciamento"]
    return "\n".join(linhas)


def carregar(caminho: str) -> list[Frente]:
    frentes = []
    with open(caminho, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            hist = [float(x) for x in r.get("HistoricoDesvios", "").split(";") if x.strip()]
            frentes.append(Frente(
                id=r["ID"], nome=r["Nome"], tipo=r["Tipo"],
                predecessoras=r.get("Predecessoras", ""),
                folga=float(r["FolgaTotal"].replace("d", "")),
                baseline_hh=float(r["BaselineHH"]),
                hh_realizado=float(r["HHRealizado"]),
                hh_restante=float(r.get("HHRestante") or r.get("HHRrestante") or 0),
                historico=hist))
    return frentes


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    print(relatorio(carregar(sys.argv[1])))
