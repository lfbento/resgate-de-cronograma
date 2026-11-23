"""Motor único de rede CPM (Critical Path Method) sobre DAG."""
from __future__ import annotations

from collections import deque
from typing import Mapping, Sequence

from resgate_cronograma.models import (
    AtividadeCronograma,
    Dependencia,
    ResultadoCPM,
    ScheduleCycleError,
)


def _extrair_ciclo(
    nos_restantes: set[str],
    sucessores: Mapping[str, list[tuple[str, Dependencia]]],
    ordem_candidata: Sequence[str],
) -> list[str]:
    """Localiza um ciclo real dentre os nós que não puderam ser ordenados pelo Kahn."""
    estado: dict[str, int] = {}  # 0/ausente = não visitado, 1 = em pilha, 2 = fechado
    pilha: list[str] = []

    def dfs(u: str) -> list[str] | None:
        estado[u] = 1
        pilha.append(u)
        for v, _dep in sucessores.get(u, ()):
            if v not in nos_restantes:
                continue
            est_v = estado.get(v, 0)
            if est_v == 1:
                idx = pilha.index(v)
                return pilha[idx:] + [v]
            if est_v == 0:
                achado = dfs(v)
                if achado is not None:
                    return achado
        pilha.pop()
        estado[u] = 2
        return None

    for no in ordem_candidata:
        if no in nos_restantes and estado.get(no, 0) == 0:
            ciclo = dfs(no)
            if ciclo is not None:
                return ciclo
    return sorted(nos_restantes)


class RedeCPM:
    """Grafo acíclico direcionado (DAG) com ordenação topológica reutilizável."""

    def __init__(self, atividades: Sequence[AtividadeCronograma]) -> None:
        self.atividades_por_id: dict[str, AtividadeCronograma] = {}
        ids_ordem: list[str] = []
        for a in atividades:
            if a.id in self.atividades_por_id:
                raise ValueError(f"Atividade duplicada no cronograma: {a.id}")
            self.atividades_por_id[a.id] = a
            ids_ordem.append(a.id)

        self.internal_preds: dict[str, list[Dependencia]] = {k: [] for k in ids_ordem}
        self.external_preds: dict[str, list[Dependencia]] = {k: [] for k in ids_ordem}
        self.internal_succs: dict[str, list[tuple[str, Dependencia]]] = {k: [] for k in ids_ordem}
        missing_seen: set[str] = set()
        self.missing_predecessors: list[str] = []

        in_degree: dict[str, int] = {k: 0 for k in ids_ordem}

        for a in atividades:
            for dep in a.dependencias:
                if dep.pred_id in self.atividades_por_id:
                    self.internal_preds[a.id].append(dep)
                    self.internal_succs[dep.pred_id].append((a.id, dep))
                    in_degree[a.id] += 1
                else:
                    self.external_preds[a.id].append(dep)
                    if dep.pred_id not in missing_seen:
                        missing_seen.add(dep.pred_id)
                        self.missing_predecessors.append(dep.pred_id)

        fila = deque(k for k in ids_ordem if in_degree[k] == 0)
        ordem: list[str] = []
        while fila:
            u = fila.popleft()
            ordem.append(u)
            for v, _dep in self.internal_succs[u]:
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    fila.append(v)

        if len(ordem) < len(ids_ordem):
            restantes = set(ids_ordem) - set(ordem)
            ciclo = _extrair_ciclo(restantes, self.internal_succs, ids_ordem)
            raise ScheduleCycleError(ciclo)

        self.ordem_topologica = ordem

    def tem_ligacao_fs(self, task_id: str) -> bool:
        """Retorna True se a atividade possui alguma relação FS real (predecessora ou sucessora)."""
        if any(dep.tipo == "FS" for dep in self.internal_preds.get(task_id, ())):
            return True
        if any(dep.tipo == "FS" for dep in self.external_preds.get(task_id, ())):
            return True
        return any(dep.tipo == "FS" for _succ, dep in self.internal_succs.get(task_id, ()))

    def avaliar(
        self,
        duracoes: Mapping[str, float] | None = None,
        prazo: float | None = None,
    ) -> ResultadoCPM:
        """Executa forward e backward pass completos para FS/SS/FF/SF."""
        durs: dict[str, float] = {
            k: float(duracoes[k]) if duracoes is not None else float(self.atividades_por_id[k].dur_m)
            for k in self.ordem_topologica
        }

        es: dict[str, float] = {}
        ef: dict[str, float] = {}

        for k in self.ordem_topologica:
            d_k = durs[k]
            es_k = 0.0

            # Predecessoras externas/já concluídas ancoradas em ES=0, EF=0.
            for dep in self.external_preds[k]:
                if dep.tipo in ("FS", "SS"):
                    es_k = max(es_k, dep.lag_dias)
                else:  # FF / SF
                    es_k = max(es_k, dep.lag_dias - d_k)

            for dep in self.internal_preds[k]:
                u = dep.pred_id
                if dep.tipo == "FS":
                    es_k = max(es_k, ef[u] + dep.lag_dias)
                elif dep.tipo == "SS":
                    es_k = max(es_k, es[u] + dep.lag_dias)
                elif dep.tipo == "FF":
                    es_k = max(es_k, ef[u] + dep.lag_dias - d_k)
                elif dep.tipo == "SF":
                    es_k = max(es_k, es[u] + dep.lag_dias - d_k)

            es[k] = es_k
            ef[k] = es_k + d_k

        duracao_projeto = max(ef.values(), default=0.0)
        terminal_lf = float(prazo) if prazo is not None else duracao_projeto

        ls: dict[str, float] = {}
        lf: dict[str, float] = {}

        for k in reversed(self.ordem_topologica):
            d_k = durs[k]
            lf_k = terminal_lf
            for s, dep in self.internal_succs[k]:
                if dep.tipo == "FS":
                    lf_k = min(lf_k, ls[s] - dep.lag_dias)
                elif dep.tipo == "SS":
                    lf_k = min(lf_k, ls[s] - dep.lag_dias + d_k)
                elif dep.tipo == "FF":
                    lf_k = min(lf_k, lf[s] - dep.lag_dias)
                elif dep.tipo == "SF":
                    lf_k = min(lf_k, lf[s] - dep.lag_dias + d_k)

            lf[k] = lf_k
            ls[k] = lf_k - d_k

        folga_total = {k: ls[k] - es[k] for k in self.ordem_topologica}
        return ResultadoCPM(
            es=es,
            ef=ef,
            ls=ls,
            lf=lf,
            folga_total=folga_total,
            duracao_projeto=duracao_projeto,
            ordem_topologica=list(self.ordem_topologica),
            missing_predecessors=list(self.missing_predecessors),
            prazo=prazo,
        )


def aplicar_folgas_cpm(
    atividades: Sequence[AtividadeCronograma],
    prazo: float | None = None,
    rede: RedeCPM | None = None,
) -> ResultadoCPM:
    """Calcula CPM e atualiza `a.folga` quando `prazo` é informado ou `FolgaTotal` não foi importada."""
    grafo = rede or RedeCPM(atividades)
    resultado = grafo.avaliar(prazo=prazo)
    for a in atividades:
        if prazo is not None or not a.folga_importada:
            a.folga = round(resultado.folga_total[a.id], 4)
    return resultado
