# 🛟 Resgate de Cronograma — CPM + PID + Monte Carlo

Sistema **prescritivo** de recuperação de cronogramas de projeto: em vez de
impor hora-extra na frente errada, diz **onde agir**, **quanto acelerar** e
**o que fazer** — com leitura nativa de **Primavera P6 (.xer)** e
**MS Project (.xml MSPDI)**.

> Regra central: *injetar recursos em atividade com folga disponível eleva
> custo sem antecipar a entrega.* O algoritmo atua só onde governa o prazo.

## Arquitetura

| Camada | Responde | Módulo |
|---|---|---|
| **CPM** (onde agir) | Qual frente governa o prazo? (folga ≤0 = crítico; ≤5d = alerta) | `cpm_pid.py` |
| **PID discreto** (quanto) | `uk = Kp·ek + Ki·Σej + Kd·(ek−ek−1)` por frente crítica | `cpm_pid.py` |
| **Monte Carlo** (risco) | Probabilidade de prazo + **índice de criticidade** por atividade | `monte_carlo.py` |
| **Leitores** | Primavera P6 `.xer` e MS Project `.xml` → CSV unificado | `ler_cronograma.py` |

**Travas de segurança do PID:** saturação (teto físico +35% HH), *anti-windup*
(congela o integral no teto → gargalo físico, não de força), *deadband* (1,5%)
e separação **Interna × Externa** (hora extra é inócua em diligenciamento —
prescreve cobrança de fornecedor, não overtime).

**Integração MC × PID:** o Monte Carlo atua como *sensor* (índice de
criticidade = fração das simulações com folga zero) e o PID como *atuador*
(`uk` ponderado pelo risco). Crashing em frente que é gargalo em <40% das
simulações é dinheiro queimado.

## Uso rápido

```bash
# 1. Converter (P6 ou Project → CSV)
python3 ler_cronograma.py obra.xer -o saida.csv
python3 ler_cronograma.py cronograma.xml -o saida.csv   # Project: exportar como XML

# 2. Diagnóstico completo (10 mil simulações, prazo 45 dias úteis, alvo P80)
python3 monte_carlo.py saida.csv --sims 10000 --prazo 45 --alvo 80

# 3. Ou só o motor PID determinístico
python3 cpm_pid.py saida.csv
```

## Formato CSV (colunas)

`ID, Nome, Tipo, Predecessoras, FolgaTotal, BaselineHH, HHRealizado,
HHRestante, HistoricoDesvios, DurO, DurM, DurP`

- `Predecessoras`: `42FS+0d;41SS+3d` (FS/SS/FF/SF + lag em dias)
- `HistoricoDesvios`: `4;6;8` (desvio por ciclo de medição — alimenta o PID)
- `DurO/DurM/DurP`: PERT otimista/most-likely/pessimista (dias) — o MC amostra

## Heurísticas documentadas (corrija no CSV)

- **Tipo Interna/Externa** inferido por palavras-chave (compra, inspeção,
  fornecedor... → Externa)
- **PERT** estimado: `DurO = 0,75×m`, `DurP = 1,6×m` (substitua com histórico)

## Limitações conhecidas

- `.mpp` binário não é lido — exporte como **XML** no Project
- XER validado contra amostras sintéticas; aceite contribuições com `.xer` reais
- Monte Carlo não modela correlação entre atividades (clima/geral) — a cauda
  real é mais pesada que a simulada

## Licença

MIT
