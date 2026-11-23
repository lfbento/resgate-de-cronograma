"""Modelos canônicos de domínio para o sistema de resgate de cronogramas."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

HORAS_POR_DIA = 8.0

LinkType = Literal["FS", "SS", "FF", "SF"]

_DEP_EXPLICITA_RE = re.compile(
    r"^\s*([A-Za-z0-9_.-]+?)\s*(FS|SS|FF|SF)\s*([+-]\s*\d+(?:\.\d+)?)?\s*d?\s*$",
    re.IGNORECASE,
)
_DEP_SIMPLES_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*$")


class ScheduleCycleError(ValueError):
    """Erro levantado quando há ciclo lógico entre atividades internas do cronograma."""

    def __init__(self, cycle_nodes: list[str], message: str | None = None) -> None:
        self.cycle_nodes = list(cycle_nodes)
        if message is None:
            cadeia = " -> ".join(self.cycle_nodes) if self.cycle_nodes else "(desconhecido)"
            message = (
                "Ciclo lógico detectado no grafo de cronograma envolvendo as "
                f"atividades: {cadeia}"
            )
        super().__init__(message)


@dataclass(frozen=True)
class Dependencia:
    """Ligação lógica entre uma atividade predecessora e sua sucessora."""

    pred_id: str
    tipo: LinkType = "FS"
    lag_dias: float = 0.0

    def to_token(self) -> str:
        """Serializa a dependência no formato textual `<ID><TIPO><+/-LAG>d`."""
        return f"{self.pred_id}{self.tipo}{self.lag_dias:+g}d"


def parse_dependencias(texto: str) -> list[Dependencia]:
    """Interpreta lista de predecessoras separadas por `;` com suporte a IDs alfanuméricos."""
    deps: list[Dependencia] = []
    for token in (texto or "").split(";"):
        tok = token.strip()
        if not tok:
            continue
        match = _DEP_EXPLICITA_RE.match(tok)
        if match:
            pred_id = match.group(1)
            tipo = match.group(2).upper()
            lag_raw = (match.group(3) or "0").replace(" ", "")
            deps.append(Dependencia(pred_id=pred_id, tipo=tipo, lag_dias=float(lag_raw)))  # type: ignore[arg-type]
            continue
        simple = _DEP_SIMPLES_RE.match(tok)
        if simple:
            deps.append(Dependencia(pred_id=simple.group(1), tipo="FS", lag_dias=0.0))
    return deps


@dataclass
class AtividadeCronograma:
    """Representação canônica de uma atividade ou frente de trabalho do cronograma."""

    id: str
    nome: str
    tipo: str = "Interna"
    predecessoras: str = ""
    folga: float = 0.0
    baseline_hh: float = 0.0
    hh_realizado: float = 0.0
    hh_restante: float = 0.0
    historico: list[float] = field(default_factory=list)
    dur_o: float = 0.0
    dur_m: float = 0.0
    dur_p: float = 0.0
    dependencias: list[Dependencia] = field(default_factory=list)
    concluida: bool = False
    marco: bool = False
    dur_m_explicita: bool | None = None
    folga_importada: bool = True
    grupo_risco: str = ""

    def __post_init__(self) -> None:
        self.id = str(self.id).strip()
        self.historico = [float(x) for x in self.historico]
        if self.predecessoras and not self.dependencias:
            self.dependencias = parse_dependencias(self.predecessoras)
        elif self.dependencias and not self.predecessoras:
            self.predecessoras = ";".join(dep.to_token() for dep in self.dependencias)
        if self.dur_m_explicita is None:
            self.dur_m_explicita = self.dur_m > 0

    @property
    def criticidade(self) -> str:
        """Classifica a criticidade determinística pela folga total em dias úteis."""
        if self.folga <= 0:
            return "CRÍTICA"
        if self.folga <= 5:
            return "ALERTA"
        return "OK"

    @property
    def eh_interna(self) -> bool:
        """Retorna True se a frente é executada pela equipe interna."""
        return self.tipo.strip().lower().startswith("int")


Frente = AtividadeCronograma


@dataclass
class ResultadoCPM:
    """Resultado completo dos passes forward e backward do motor CPM."""

    es: dict[str, float]
    ef: dict[str, float]
    ls: dict[str, float]
    lf: dict[str, float]
    folga_total: dict[str, float]
    duracao_projeto: float
    ordem_topologica: list[str]
    missing_predecessors: list[str]
    prazo: float | None = None


@dataclass
class ResultadoPID:
    """Resultado dimensionalmente coerente (Modelo 1: dias -> HH) do controlador PID."""

    u_dias: float
    uk_hh: float
    taxa_hh_dia: float
    dur_ref: float
    hh_ref: float
    teto_hh: float
    p_dias: float
    i_dias: float
    d_dias: float
    saturado: bool = False
    deadband: bool = False
    inativo: bool = False

    def to_legacy_dict(self) -> dict[str, object]:
        """Converte o resultado para o dicionário detalhado usado nos wrappers legados."""
        if self.inativo:
            return {}
        if self.deadband:
            return {"deadband": True}
        return {
            "P": round(self.p_dias, 2),
            "I": round(self.i_dias, 2),
            "D": round(self.d_dias, 2),
            "u_dias": round(self.u_dias, 2),
            "taxa_hh_dia": round(self.taxa_hh_dia, 2),
            "uk_hh": round(self.uk_hh, 1),
            "teto_hh": round(self.teto_hh, 1),
            "saturado": self.saturado,
        }


@dataclass
class PrescricaoRisco:
    """Prescrição de recuperação integrada ao índice de criticidade Monte Carlo."""

    atividade: AtividadeCronograma
    criticidade: float
    pid: ResultadoPID
    uk_hh_ajustado: float
    acoes: list[str]


@dataclass
class AlocacaoResgate:
    """Esforço aplicado e compressão resultante em uma frente na malha fechada."""

    atividade_id: str
    nome: str
    tipo: str
    criticidade: float
    sensibilidade: float
    uk_hh_aplicado: float
    fator_compressao: float
    dur_m_antes: float
    dur_m_depois: float
    saturado: bool = False


@dataclass(frozen=True)
class FaixaHistograma:
    """Faixa agregada (bin) da distribuição de término do cronograma."""

    inicio: float
    fim: float
    contagem: int


@dataclass(frozen=True)
class InfoCiclo:
    """Metadados de um snapshot semanal registrado para um projeto."""

    ciclo_id: int
    projeto: str
    numero: int
    rotulo: str
    origem: str
    data_medicao: str
    prazo_dias: float | None
    criado_em: str
    total_atividades: int


@dataclass(frozen=True)
class ParametrosCusto:
    """Parâmetros financeiros em R$ para valoração de alavancas de recuperação."""

    custo_hh_base: float = 80.0
    custo_hh_hora_extra: float = 120.0
    custo_hh_segundo_turno: float = 150.0
    custo_fixo_fast_tracking: float = 500.0
    fator_risco_retrabalho_ft: float = 0.05
    ganho_max_ft_pct: float = 0.20
    custo_fixo_diligenciamento: float = 1000.0


@dataclass(frozen=True)
class OpcaoIntervencao:
    """Intervenção candidata valorada em R$ e dias recuperados estimados."""

    atividade_id: str
    nome: str
    tipo: str
    estrategia: str
    descricao: str
    hh_adicional: float
    custo_estimado_brl: float
    dias_recuperados_estimados: float
    custo_por_dia_brl: float | None
    saturado: bool = False


@dataclass(frozen=True)
class AlocacaoValorada:
    """Custo financeiro individual de uma alocação aplicada na malha fechada."""

    atividade_id: str
    nome: str
    uk_hh_aplicado: float
    custo_brl: float
    dias_comprimidos_dur_m: float


@dataclass
class PlanoOtimizadoCusto:
    """Plano de ações ranqueado por R$/dia recuperado e orçamento da malha fechada."""

    parametros: ParametrosCusto
    opcoes: list[OpcaoIntervencao]
    custo_total_malha_fechada_brl: float | None = None
    alocacoes_valoradas: list[AlocacaoValorada] = field(default_factory=list)


@dataclass
class ResultadoResgate:
    """Comparativo Antes × Depois e convergência da malha fechada MC -> PID."""

    p50_antes: float
    p80_antes: float
    p90_antes: float
    p_alvo_antes: float
    p50_depois: float
    p80_depois: float
    p90_depois: float
    p_alvo_depois: float
    gap_inicial: float
    gap_final: float
    dias_recuperados: float
    iteracoes: int
    convergiu: bool
    gargalos_residuais: list[str]
    alocacoes: list[AlocacaoResgate]
    histograma_antes: list[FaixaHistograma] = field(default_factory=list)
    histograma_depois: list[FaixaHistograma] = field(default_factory=list)


@dataclass
class ResultadoMonteCarlo:
    """Resultado agregado da simulação Monte Carlo e da integração MC x PID."""

    p50: float
    p80: float
    p90: float
    p_alvo: float
    alvo: float
    prazo: float
    n_sims: int
    seed: int
    criticidade: dict[str, float]
    missing_predecessors: list[str]
    prescricoes: list[PrescricaoRisco]
    sensibilidade: dict[str, float] = field(default_factory=dict)
    correlacao: float = 0.0
    resgate: ResultadoResgate | None = None
    histograma: list[FaixaHistograma] = field(default_factory=list)
