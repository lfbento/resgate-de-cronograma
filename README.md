# 🛟 Resgate de Cronograma — CPM + PID + Monte Carlo

Sistema **prescritivo** de recuperação de cronogramas de projeto: em vez de
impor hora-extra na frente errada, diz **onde agir**, **quanto acelerar** e
**o que fazer** — com leitura nativa de **Primavera P6 (.xer)** e
**MS Project (.xml MSPDI)** e runtime em **Python 3 stdlib pura**.

> Regra central: *injetar recursos em atividade com folga disponível eleva
> custo sem antecipar a entrega.* O algoritmo atua só onde governa o prazo.

## Arquitetura

O núcleo reside no pacote modular `resgate_cronograma/`, mantido compatível com
os entrypoints CLI da raiz (`ler_cronograma.py`, `ciclos.py`, `cpm_pid.py` e
`monte_carlo.py`):

| Camada | Responsabilidade | Módulo |
|---|---|---|
| **Domínio & Grafo** | Modelos canônicos, parser `FS/SS/FF/SF` (IDs alfanuméricos) e detecção de ciclos | `resgate_cronograma/models.py` |
| **CPM** (onde agir) | DAG único (`O(V+E)`), forward/backward `FS/SS/FF/SF`, âncora `t=0` p/ externas | `resgate_cronograma/cpm.py` |
| **PID discreto** (Modelo 1) | `HistoricoDesvios` (dias) → `u_dias` → `uk_hh`, anti-windup iterativo, prescrição | `resgate_cronograma/pid.py` |
| **Monte Carlo** (risco) | PERT modificado (`λ=4.0`), correlação Iman-Conover, tornado Spearman e malha fechada Antes×Depois | `resgate_cronograma/monte_carlo.py` |
| **Histórico SQLite** (Onda 3) | Snapshots semanais por projeto em SQLite local com derivação automática de `HistoricoDesvios` | `resgate_cronograma/cycle_store.py` |
| **Custo × Prazo** (Onda 3) | Parâmetros em R$, ranking `R$/dia recuperado` e orçamento exato da malha fechada | `resgate_cronograma/cost.py` |
| **Relatórios JSON/HTML** (Onda 3) | Exportação JSON estruturada e HTML5 executivo 100% offline com gráficos SVG inline | `resgate_cronograma/exporters.py` |
| **Leitores** | Primavera P6 `.xer`, MS Project `.xml` (MSPDI) e CSV (9 e 12 colunas) | `resgate_cronograma/parsers/` |
| **CLIs compatíveis** | Wrappers diretos para uso em linha de comando ou importação Python | `ler_cronograma.py`, `ciclos.py`, `cpm_pid.py`, `monte_carlo.py` |

### Modelo 1 (coerência dimensional `dias → HH`) e travas do PID

- **`HistoricoDesvios` em dias úteis:** desvios positivos (`> 0`) representam atraso por ciclo; o PID calcula `u_dias` e converte para esforço via `uk_hh = u_dias × taxa_hh_dia`, onde `taxa_hh_dia = HHRestante / DurM` (fallback `BaselineHH / DurM` ou `8.0 HH/dia` quando `DurM` não for explícito, como em CSVs de 9 colunas).
- **Saturação + anti-windup iterativo:** teto físico de `+35%` sobre `HHRestante` (fallback `BaselineHH`), com integração condicional ciclo a ciclo que congela o acumulador na saturação e permite descarregamento natural quando o erro inverte.
- **Deadband dimensional (`1,5%`):** variações residuais em relação à duração de referência (ou desvio zero) suprimem atuação.
- **Regras precisas:** *fast-tracking* (`FS → SS`) apenas quando há ligação `FS` real (predecessora ou sucessora); tratamento explícito para frentes adiantadas (`uk < 0`); hora-extra bloqueada em frentes `Externa` (prescreve diligenciamento).
- **Predecessoras externas/ausentes:** IDs não presentes na tabela (ex.: frentes já concluídas de um recorte WBS) são ancorados em `t = 0` com aviso não-fatal; ciclos internos reais disparam `ScheduleCycleError` listando os IDs envolvidos.

## Uso rápido

```bash
# 1. Converter (P6 .xer ou MS Project .xml MSPDI → CSV 12 colunas)
python ler_cronograma.py amostra.xer -o saida.csv
python ler_cronograma.py amostra_project.xml -o saida.csv

# 2. Motor determinístico CPM + PID (opcional: replay --ciclo N e recálculo --prazo)
python cpm_pid.py demo_cronograma.csv
python cpm_pid.py demo_cronograma.csv --ciclo 2
python cpm_pid.py demo_mc.csv --prazo 45

# 3. Diagnóstico completo Monte Carlo + Tornado + malha fechada Antes × Depois
python monte_carlo.py demo_mc.csv --sims 10000 --prazo 45 --alvo 80 --seed 42 --correlacao 0.5
python monte_carlo.py demo_mc.csv --prazo 45 --fatores-risco '{"Interna": 0.6}' --eficiencia-crashing 0.85
python monte_carlo.py demo_mc.csv --prazo 45 --sem-resgate   # apenas diagnóstico em malha aberta

# 4. Histórico semanal em SQLite, otimização R$/dia e relatórios JSON/HTML (Onda 3)
python ciclos.py registrar amostra.xer --db obra.db --projeto Obra-A --prazo 45 --rotulo "Semana 1"
python ciclos.py registrar amostra_project.xml --db obra.db --projeto Obra-A --prazo 45 --rotulo "Semana 2"
python ciclos.py listar --db obra.db --projeto Obra-A
python ciclos.py exportar --db obra.db --projeto Obra-A -o historico_c2.csv --ciclo 2

python cpm_pid.py --db obra.db --projeto Obra-A --prazo 45 --json out/cpm.json --html out/cpm.html
python monte_carlo.py --db obra.db --projeto Obra-A --prazo 45 --sims 2000 --custos '{"custo_hh_hora_extra": 140}' --json out/mc.json --html out/mc.html
```

### Malha fechada, correlação e Tornado (Onda 2)

- **Correlação Iman-Conover (`--correlacao`, `--fatores-risco`):** preserva exatamente as distribuições marginais PERT enquanto correlaciona atividades do mesmo `GrupoRisco` (ou `Tipo` como fallback quando `--correlacao > 0`). Aceita `0.0 <= rho < 1.0`.
- **Sensibilidade de Spearman (Tornado):** mede a correlação de postos entre a duração de cada atividade e o término total da obra (`0.0` em marcos constantes).
- **Malha fechada (`--eficiencia-crashing`, `--max-iter`, `--sem-resgate`):** quando `P(alvo) > prazo`, reparte o gap probabilístico proporcionalmente entre frentes críticas `Interna` (inclusive quando `HistoricoDesvios` está vazio), comprime durações por conservação de trabalho (`Dur' = Dur / (1 + η·uk/HH_ref)`) sob o teto `+35%` e re-simula com *Common Random Numbers* provando a recuperação (ou diagnosticando gargalos residuais).

### Histórico por projeto, custo em R$/dia e relatórios offline (Onda 3)

- **Banco SQLite de ciclos (`ciclos.py` / `--db --projeto`):** armazena snapshots `.xer`, `.xml` e `.csv` por projeto e deriva automaticamente `HistoricoDesvios` (`desvio_dias = -FolgaTotal`) em ordem cronológica entre ciclos, eliminando edição manual semanal.
- **Ranking `R$/dia recuperado` (`--custos`):** avalia e ordena `Fast-tracking (FS -> SS)`, `Crashing (Hora Extra)`, `2º Turno / Terceirização` (quando saturada) e `Diligenciamento Externo`, além do orçamento exato das alocações em malha fechada. Aceita JSON inline ou caminho de arquivo `.json`.
- **Relatórios JSON e HTML5 offline (`--json`, `--html`):** gera saída estruturada (`schema_version = "3.0"`) e relatório executivo autocontido (CSS inline + gráficos SVG inline de histograma Antes × Depois, Tornado Spearman e criticidade, sem CDN externo).

## Formato CSV e leitores reais

`ID, Nome, Tipo, Predecessoras, FolgaTotal, BaselineHH, HHRealizado, HHRestante, HistoricoDesvios, DurO, DurM, DurP` *(+ coluna opcional `GrupoRisco`)*

- **CSV 9 e 12 colunas:** aceita ambos os esquemas, coluna opcional `GrupoRisco` e o cabeçalho legado `HHRrestante`.
- **`Predecessoras`:** suporta IDs alfanuméricos e todos os tipos `FS`, `SS`, `FF`, `SF` com lags positivos ou negativos (ex.: `ENG-10FF-1.5d;41SS+3d`).
- **Primavera P6 (`.xer`):** lê `TASK`, `TASKPRED` e `TASKRSRC` opcional (HH de esforço); exclui `TT_WBS` e `TT_LOE`; preserva marcos (`0,0,0`) e trata `TK_Complete` com duração restante zero.
- **MS Project (`.xml` MSPDI):** exclui `UID=0` e `<Summary>1</Summary>`, lê `<Work>`/`<ActualWork>`/`<RemainingWork>` quando presentes e decodifica tanto ISO 8601 (`PT-64H`) quanto décimos de minuto (`4800 = 1 dia útil`) e `<LagFormat>` percentual.
- **Classificação `Interna` × `Externa`:** insensível a acentos (`Inspeção de Válvulas` → `Externa`) com preservação de overrides explícitos.

## Testes automatizados

```bash
python -m pytest tests -v
```

## Limitações conhecidas

- `.mpp` binário não é lido diretamente — exporte como **XML (MSPDI)** no MS Project.
- Execução local por projeto (CLI e biblioteca Python stdlib); não inclui servidor web nem exportação direta em PDF binário.

## Licença

MIT
