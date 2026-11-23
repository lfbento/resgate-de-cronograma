"""Simulador Monte Carlo com PERT modificado, Iman-Conover, Spearman e malha fechada."""
from __future__ import annotations

import math
import random
from typing import Mapping, Sequence

from resgate_cronograma.cpm import RedeCPM
from resgate_cronograma.models import (
    AlocacaoResgate,
    AtividadeCronograma,
    FaixaHistograma,
    PrescricaoRisco,
    ResultadoMonteCarlo,
    ResultadoResgate,
)
from resgate_cronograma.pid import (
    TETO_SATURACAO,
    alocar_gap_probabilistico,
    calcular_pid,
    comprimir_pert_por_esforco,
    prescrever,
)


def parametros_pert(
    dur_o: float,
    dur_m: float,
    dur_p: float,
    lam: float = 4.0,
) -> tuple[float, float]:
    """Retorna `(alpha, beta)` da distribuição Beta-PERT modificada sem singularidades."""
    if dur_o > dur_m or dur_m > dur_p:
        raise ValueError(
            f"Limites PERT inválidos (exige DurO <= DurM <= DurP): {dur_o}, {dur_m}, {dur_p}."
        )
    if dur_p == dur_o:
        return (0.0, 0.0)
    amplitude = dur_p - dur_o
    alpha = 1.0 + lam * (dur_m - dur_o) / amplitude
    beta = 1.0 + lam * (dur_p - dur_m) / amplitude
    return (alpha, beta)


def amostrar_pert(
    dur_o: float,
    dur_m: float,
    dur_p: float,
    rng: random.Random,
    lam: float = 4.0,
) -> float:
    """Amostra uma duração via Beta-PERT modificada (retorna `dur_m` quando degenerada)."""
    alpha, beta = parametros_pert(dur_o, dur_m, dur_p, lam=lam)
    if dur_p == dur_o:
        return float(dur_m)
    return dur_o + (dur_p - dur_o) * rng.betavariate(alpha, beta)


def _postos_medios(valores: Sequence[float]) -> list[float]:
    """Calcula postos 1-based atribuindo a média dos postos em caso de empates."""
    n = len(valores)
    if n == 0:
        return []
    indices = sorted(range(n), key=valores.__getitem__)
    postos = [0.0] * n
    i = 0
    while i < n:
        j = i + 1
        vi = valores[indices[i]]
        while j < n and valores[indices[j]] == vi:
            j += 1
        rank_medio = (i + j - 1) / 2.0 + 1.0
        for k in range(i, j):
            postos[indices[k]] = rank_medio
        i = j
    return postos


def correlacao_spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Calcula a correlação de postos de Spearman em `[-1.0, 1.0]` (retorna 0.0 sem variância)."""
    n = len(x)
    if n < 2 or n != len(y):
        return 0.0
    rx = _postos_medios(x)
    ry = _postos_medios(y)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = 0.0
    sxx = 0.0
    syy = 0.0
    for a, b in zip(rx, ry):
        da = a - mx
        db = b - my
        num += da * db
        sxx += da * da
        syy += db * db
    denom = math.sqrt(sxx * syy)
    if denom <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, num / denom))


def _validar_correlacoes(
    correlacao: float,
    fatores_risco: Mapping[str, float] | None,
) -> dict[str, float]:
    if not (0.0 <= correlacao < 1.0):
        raise ValueError(
            f"Coeficiente de correlação global deve estar em [0.0, 1.0): {correlacao}."
        )
    mapa: dict[str, float] = {}
    if fatores_risco:
        for grupo, rho in fatores_risco.items():
            rho_val = float(rho)
            if not (0.0 <= rho_val < 1.0):
                raise ValueError(
                    f"Correlação do grupo '{grupo}' deve estar em [0.0, 1.0): {rho_val}."
                )
            mapa[str(grupo).strip()] = rho_val
    return mapa


def _gerar_matriz_duracoes(
    atividades: Sequence[AtividadeCronograma],
    n_sims: int,
    seed: int,
    correlacao: float = 0.0,
    fatores_risco: Mapping[str, float] | None = None,
) -> dict[str, list[float]]:
    """Gera matriz de amostras PERT aplicando pareamento de postos Iman-Conover quando `rho > 0`."""
    fatores_map = _validar_correlacoes(correlacao, fatores_risco)
    rng = random.Random(seed)
    amostras: dict[str, list[float]] = {a.id: [0.0] * n_sims for a in atividades}

    for t in range(n_sims):
        for a in atividades:
            amostras[a.id][t] = amostrar_pert(a.dur_o, a.dur_m, a.dur_p, rng)

    grupos: dict[str, list[tuple[AtividadeCronograma, float]]] = {}
    for a in atividades:
        if a.dur_p <= a.dur_o:
            continue
        grupo = a.grupo_risco.strip() or a.tipo.strip() or "Interna"
        rho_a = fatores_map.get(grupo, correlacao)
        if rho_a > 0.0:
            grupos.setdefault(grupo, []).append((a, rho_a))

    grupos_ativos = {g: itens for g, itens in grupos.items() if len(itens) >= 2}
    if not grupos_ativos:
        return amostras

    rng_corr = random.Random(seed ^ 0x9E3779B9)
    for _grupo, itens in grupos_ativos.items():
        fator_comum = [rng_corr.gauss(0.0, 1.0) for _ in range(n_sims)]
        for a, rho_a in itens:
            wf = math.sqrt(rho_a)
            we = math.sqrt(1.0 - rho_a)
            escores_z = [
                wf * fator_comum[t] + we * rng_corr.gauss(0.0, 1.0)
                for t in range(n_sims)
            ]
            ordem_z = sorted(range(n_sims), key=escores_z.__getitem__)
            pert_ordenado = sorted(amostras[a.id])
            reordenado = [0.0] * n_sims
            for posto, idx_t in enumerate(ordem_z):
                reordenado[idx_t] = pert_ordenado[posto]
            amostras[a.id] = reordenado

    return amostras


def construir_histograma(
    valores: Sequence[float],
    limite_min: float | None = None,
    limite_max: float | None = None,
    n_bins: int = 12,
) -> list[FaixaHistograma]:
    """Agrupa tempos simulados de término em `n_bins` faixas uniformes (ou 1 faixa se degenerado)."""
    if n_bins < 1:
        raise ValueError(f"Número de faixas do histograma deve ser >= 1: {n_bins}.")
    if not valores:
        return []

    vmin = float(min(valores)) if limite_min is None else float(limite_min)
    vmax = float(max(valores)) if limite_max is None else float(limite_max)
    if vmax <= vmin + 1e-12:
        return [
            FaixaHistograma(
                inicio=round(vmin, 4),
                fim=round(vmax, 4),
                contagem=len(valores),
            )
        ]

    largura = (vmax - vmin) / n_bins
    contagens = [0] * n_bins
    for valor in valores:
        idx = int((float(valor) - vmin) / largura)
        idx = min(max(idx, 0), n_bins - 1)
        contagens[idx] += 1

    return [
        FaixaHistograma(
            inicio=round(vmin + i * largura, 4),
            fim=round(vmax if i == n_bins - 1 else vmin + (i + 1) * largura, 4),
            contagem=contagens[i],
        )
        for i in range(n_bins)
    ]


def _avaliar_matriz_cpm_detalhado(
    rede: RedeCPM,
    atividades: Sequence[AtividadeCronograma],
    amostras_base: Mapping[str, Sequence[float]],
    n_sims: int,
    prazo: float,
    alvo: float,
    fatores_escala: Mapping[str, float] | None = None,
) -> tuple[float, float, float, float, dict[str, float], dict[str, float], list[float]]:
    """Avalia `N` simulações e retorna percentis, criticidade, Spearman e vetor bruto local."""
    escalas = fatores_escala or {}
    fins_obra: list[float] = [0.0] * n_sims
    atrasos: list[float] = [0.0] * n_sims
    critica_qtd: dict[str, int] = {a.id: 0 for a in atividades}

    for t in range(n_sims):
        dur = {
            a.id: amostras_base[a.id][t] * escalas.get(a.id, 1.0)
            for a in atividades
        }
        res_cpm = rede.avaliar(duracoes=dur, prazo=None)
        fins_obra[t] = res_cpm.duracao_projeto
        atrasos[t] = res_cpm.duracao_projeto - prazo
        for tid, folga in res_cpm.folga_total.items():
            if abs(folga) < 1e-6:
                critica_qtd[tid] += 1

    atrasos_ord = sorted(atrasos)

    def pct(p: float) -> float:
        if not atrasos_ord:
            return 0.0
        idx = min(max(int(p / 100.0 * len(atrasos_ord)), 0), len(atrasos_ord) - 1)
        return atrasos_ord[idx]

    criticidade = {k: v / n_sims for k, v in critica_qtd.items()}
    sensibilidade: dict[str, float] = {}
    for a in atividades:
        escala = escalas.get(a.id, 1.0)
        serie = (
            [v * escala for v in amostras_base[a.id]]
            if escala != 1.0
            else amostras_base[a.id]
        )
        sensibilidade[a.id] = correlacao_spearman(serie, fins_obra)

    return pct(50.0), pct(80.0), pct(90.0), pct(alvo), criticidade, sensibilidade, fins_obra


def _avaliar_matriz_cpm(
    rede: RedeCPM,
    atividades: Sequence[AtividadeCronograma],
    amostras_base: Mapping[str, Sequence[float]],
    n_sims: int,
    prazo: float,
    alvo: float,
    fatores_escala: Mapping[str, float] | None = None,
) -> tuple[float, float, float, float, dict[str, float], dict[str, float]]:
    """Avalia `N` simulações sobre matriz pré-gerada, devolvendo percentis, criticidade e Spearman."""
    p50, p80, p90, p_alvo, criticidade, sensibilidade, _fins = _avaliar_matriz_cpm_detalhado(
        rede,
        atividades,
        amostras_base,
        n_sims=n_sims,
        prazo=prazo,
        alvo=alvo,
        fatores_escala=fatores_escala,
    )
    return p50, p80, p90, p_alvo, criticidade, sensibilidade


def executar_malha_fechada(
    rede: RedeCPM,
    atividades: Sequence[AtividadeCronograma],
    amostras_base: Mapping[str, Sequence[float]],
    n_sims: int,
    prazo: float,
    alvo: float,
    p50_antes: float,
    p80_antes: float,
    p90_antes: float,
    p_alvo_antes: float,
    ic_antes: Mapping[str, float],
    sr_antes: Mapping[str, float],
    *,
    limiar_criticidade: float = 0.40,
    eficiencia_crashing: float = 1.0,
    max_iter: int = 5,
    fins_antes: Sequence[float] | None = None,
) -> ResultadoResgate:
    """Fecha o laço MC -> Gap P_alvo -> PID Modelo 1 -> Compressão -> Re-simulação CRN."""
    if not (0.0 < eficiencia_crashing <= 1.0):
        raise ValueError(
            f"--eficiencia-crashing deve estar em (0.0, 1.0]: {eficiencia_crashing}."
        )
    if max_iter < 1:
        raise ValueError(f"--max-iter deve ser >= 1: {max_iter}.")

    if p_alvo_antes <= 0.0:
        hist_zero = construir_histograma(fins_antes or (), n_bins=12)
        return ResultadoResgate(
            p50_antes=p50_antes,
            p80_antes=p80_antes,
            p90_antes=p90_antes,
            p_alvo_antes=p_alvo_antes,
            p50_depois=p50_antes,
            p80_depois=p80_antes,
            p90_depois=p90_antes,
            p_alvo_depois=p_alvo_antes,
            gap_inicial=0.0,
            gap_final=0.0,
            dias_recuperados=0.0,
            iteracoes=0,
            convergiu=True,
            gargalos_residuais=[],
            alocacoes=[],
            histograma_antes=list(hist_zero),
            histograma_depois=list(hist_zero),
        )

    teto_por_id: dict[str, float] = {}
    for a in atividades:
        hh_ref = a.hh_restante if a.hh_restante > 0 else a.baseline_hh
        teto_por_id[a.id] = TETO_SATURACAO * hh_ref

    uk_cumulativo: dict[str, float] = {a.id: 0.0 for a in atividades}
    historico_malha: dict[str, list[float]] = {a.id: [] for a in atividades}
    saturado_id: dict[str, bool] = {a.id: False for a in atividades}
    fatores_escala: dict[str, float] = {a.id: 1.0 for a in atividades}

    p50_atual, p80_atual, p90_atual, p_alvo_atual = (
        p50_antes,
        p80_antes,
        p90_antes,
        p_alvo_antes,
    )
    ic_atual: dict[str, float] = dict(ic_antes)
    sr_atual: dict[str, float] = dict(sr_antes)
    fins_depois: list[float] = list(fins_antes) if fins_antes is not None else []
    iteracoes = 0

    for _it in range(max_iter):
        gap_atual = max(0.0, p_alvo_atual)
        if gap_atual <= 1e-6:
            break

        elegiveis = [
            a
            for a in atividades
            if a.eh_interna
            and a.dur_m > 0.0
            and ic_atual.get(a.id, 0.0) >= limiar_criticidade
            and teto_por_id[a.id] > 0.0
            and uk_cumulativo[a.id] < teto_por_id[a.id] - 1e-6
        ]
        if not elegiveis:
            break

        gaps = alocar_gap_probabilistico(
            elegiveis,
            ic_atual,
            sr_atual,
            gap_atual,
            limiar_criticidade=limiar_criticidade,
        )
        if not gaps:
            break

        progresso = False
        for a in elegiveis:
            gap_i = gaps.get(a.id, 0.0)
            if gap_i <= 0.0:
                continue
            historico_malha[a.id].append(gap_i)
            copia = AtividadeCronograma(
                id=a.id,
                nome=a.nome,
                tipo=a.tipo,
                baseline_hh=a.baseline_hh,
                hh_restante=a.hh_restante,
                dur_o=a.dur_o,
                dur_m=a.dur_m,
                dur_p=a.dur_p,
                dur_m_explicita=a.dur_m_explicita,
                historico=list(historico_malha[a.id]),
            )
            pid_step = calcular_pid(copia, ignorar_deadband=True)
            delta_uk = max(0.0, pid_step.uk_hh)
            teto_hh = teto_por_id[a.id]
            novo_uk = min(teto_hh, uk_cumulativo[a.id] + delta_uk)
            if novo_uk > uk_cumulativo[a.id] + 1e-6:
                progresso = True
            uk_cumulativo[a.id] = novo_uk
            saturado_id[a.id] = teto_hh > 0.0 and novo_uk >= teto_hh - 1e-6
            _o, _m, _p, fator = comprimir_pert_por_esforco(
                a,
                novo_uk,
                eficiencia=eficiencia_crashing,
            )
            fatores_escala[a.id] = fator

        if not progresso:
            break

        iteracoes += 1
        p50_atual, p80_atual, p90_atual, p_alvo_atual, ic_atual, sr_atual, fins_depois = (
            _avaliar_matriz_cpm_detalhado(
                rede,
                atividades,
                amostras_base,
                n_sims=n_sims,
                prazo=prazo,
                alvo=alvo,
                fatores_escala=fatores_escala,
            )
        )
        if p_alvo_atual <= 0.0:
            break

    alocacoes: list[AlocacaoResgate] = []
    for a in sorted(atividades, key=lambda item: -uk_cumulativo.get(item.id, 0.0)):
        uk_ap = uk_cumulativo[a.id]
        if uk_ap <= 0.0:
            continue
        fator = fatores_escala[a.id]
        alocacoes.append(
            AlocacaoResgate(
                atividade_id=a.id,
                nome=a.nome,
                tipo=a.tipo,
                criticidade=ic_atual.get(a.id, ic_antes.get(a.id, 0.0)),
                sensibilidade=sr_atual.get(a.id, sr_antes.get(a.id, 0.0)),
                uk_hh_aplicado=uk_ap,
                fator_compressao=fator,
                dur_m_antes=a.dur_m,
                dur_m_depois=a.dur_m * fator,
                saturado=saturado_id[a.id],
            )
        )

    convergiu = p_alvo_atual <= 0.0
    gargalos_residuais: list[str] = []
    if not convergiu:
        criticas_ordenadas = sorted(
            (
                a
                for a in atividades
                if ic_atual.get(a.id, 0.0) >= limiar_criticidade
                and not a.concluida
                and a.dur_m > 0.0
            ),
            key=lambda item: -ic_atual.get(item.id, 0.0),
        )
        for a in criticas_ordenadas:
            if not a.eh_interna:
                gargalos_residuais.append(f"{a.id} ({a.nome} — Externa)")
            elif saturado_id[a.id] or teto_por_id[a.id] <= 0.0:
                gargalos_residuais.append(f"{a.id} ({a.nome} — Saturada +35%)")
        if not gargalos_residuais:
            gargalos_residuais = [
                f"{a.id} ({a.nome} — limite max_iter)" for a in criticas_ordenadas
            ]

    hist_antes: list[FaixaHistograma] = []
    hist_depois: list[FaixaHistograma] = []
    if fins_antes:
        serie_depois = fins_depois if fins_depois else list(fins_antes)
        vmin = min(min(fins_antes), min(serie_depois))
        vmax = max(max(fins_antes), max(serie_depois))
        hist_antes = construir_histograma(
            fins_antes, limite_min=vmin, limite_max=vmax, n_bins=12
        )
        hist_depois = construir_histograma(
            serie_depois, limite_min=vmin, limite_max=vmax, n_bins=12
        )
    elif fins_depois:
        hist_depois = construir_histograma(fins_depois, n_bins=12)

    return ResultadoResgate(
        p50_antes=p50_antes,
        p80_antes=p80_antes,
        p90_antes=p90_antes,
        p_alvo_antes=p_alvo_antes,
        p50_depois=p50_atual,
        p80_depois=p80_atual,
        p90_depois=p90_atual,
        p_alvo_depois=p_alvo_atual,
        gap_inicial=max(0.0, p_alvo_antes),
        gap_final=max(0.0, p_alvo_atual),
        dias_recuperados=max(0.0, p_alvo_antes - p_alvo_atual),
        iteracoes=iteracoes,
        convergiu=convergiu,
        gargalos_residuais=gargalos_residuais,
        alocacoes=alocacoes,
        histograma_antes=hist_antes,
        histograma_depois=hist_depois,
    )


def simular_cronograma(
    atividades: Sequence[AtividadeCronograma],
    prazo: float,
    n_sims: int = 10000,
    alvo: float = 80.0,
    seed: int = 42,
    limiar_criticidade: float = 0.40,
    ciclo: int | None = None,
    correlacao: float = 0.0,
    fatores_risco: Mapping[str, float] | None = None,
    fechar_malha: bool = False,
    eficiencia_crashing: float = 1.0,
    max_iter: int = 5,
) -> ResultadoMonteCarlo:
    """Executa N rodadas Monte Carlo reutilizando o DAG topológico da `RedeCPM`."""
    if n_sims < 1:
        raise ValueError(f"--sims deve ser >= 1 (recebido: {n_sims}).")
    if not (0.0 < eficiencia_crashing <= 1.0):
        raise ValueError(
            f"--eficiencia-crashing deve estar em (0.0, 1.0]: {eficiencia_crashing}."
        )
    if max_iter < 1:
        raise ValueError(f"--max-iter deve ser >= 1: {max_iter}.")

    for a in atividades:
        parametros_pert(a.dur_o, a.dur_m, a.dur_p)

    fatores_map = _validar_correlacoes(correlacao, fatores_risco)
    rede = RedeCPM(atividades)
    amostras = _gerar_matriz_duracoes(
        atividades,
        n_sims=n_sims,
        seed=seed,
        correlacao=correlacao,
        fatores_risco=fatores_map,
    )
    p50, p80, p90, p_alvo, criticidade, sensibilidade, fins_antes = (
        _avaliar_matriz_cpm_detalhado(
            rede,
            atividades,
            amostras,
            n_sims=n_sims,
            prazo=prazo,
            alvo=alvo,
        )
    )

    prescricoes: list[PrescricaoRisco] = []
    for a in sorted(atividades, key=lambda item: -criticidade.get(item.id, 0.0)):
        ic = criticidade.get(a.id, 0.0)
        if ic < limiar_criticidade:
            continue
        pid_res = calcular_pid(a, ciclo=ciclo)
        uk_ajustado = pid_res.uk_hh * (0.5 + 0.5 * ic)
        acoes = prescrever(
            a,
            uk_ajustado,
            pid_res,
            tem_fs=rede.tem_ligacao_fs(a.id),
            forcado_por_risco=True,
        )
        prescricoes.append(
            PrescricaoRisco(
                atividade=a,
                criticidade=ic,
                pid=pid_res,
                uk_hh_ajustado=uk_ajustado,
                acoes=acoes,
            )
        )

    resgate = None
    if fechar_malha:
        resgate = executar_malha_fechada(
            rede=rede,
            atividades=atividades,
            amostras_base=amostras,
            n_sims=n_sims,
            prazo=prazo,
            alvo=alvo,
            p50_antes=p50,
            p80_antes=p80,
            p90_antes=p90,
            p_alvo_antes=p_alvo,
            ic_antes=criticidade,
            sr_antes=sensibilidade,
            limiar_criticidade=limiar_criticidade,
            eficiencia_crashing=eficiencia_crashing,
            max_iter=max_iter,
            fins_antes=fins_antes,
        )

    histograma = (
        list(resgate.histograma_antes)
        if (resgate is not None and resgate.histograma_antes)
        else construir_histograma(fins_antes, n_bins=12)
    )
    correlacao_efetiva = (
        max([correlacao, *fatores_map.values()])
        if (correlacao > 0.0 or fatores_map)
        else 0.0
    )
    return ResultadoMonteCarlo(
        p50=p50,
        p80=p80,
        p90=p90,
        p_alvo=p_alvo,
        alvo=alvo,
        prazo=prazo,
        n_sims=n_sims,
        seed=seed,
        criticidade=criticidade,
        missing_predecessors=list(rede.missing_predecessors),
        prescricoes=prescricoes,
        sensibilidade=sensibilidade,
        correlacao=correlacao_efetiva,
        resgate=resgate,
        histograma=histograma,
    )
