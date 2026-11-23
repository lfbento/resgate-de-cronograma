"""Testes unitários da simulação Monte Carlo, Modified PERT e acoplamento MC x PID."""
from __future__ import annotations

import random

import pytest

from resgate_cronograma.models import AtividadeCronograma
from resgate_cronograma.monte_carlo import (
    _gerar_matriz_duracoes,
    amostrar_pert,
    construir_histograma,
    correlacao_spearman,
    parametros_pert,
    simular_cronograma,
)


def test_symmetric_pert_no_u_singularity():
    alpha, beta = parametros_pert(10.0, 20.0, 30.0)
    assert alpha == pytest.approx(3.0)
    assert beta == pytest.approx(3.0)

    rng = random.Random(123)
    amostras = [amostrar_pert(10.0, 20.0, 30.0, rng) for _ in range(4000)]
    assert all(10.0 <= x <= 30.0 for x in amostras)
    media = sum(amostras) / len(amostras)
    assert 19.5 <= media <= 20.5

    # Curva unimodal: centro [15, 25] tem mais massa que os extremos [10, 15) U (25, 30]
    centro = sum(1 for x in amostras if 15.0 <= x <= 25.0)
    extremos = len(amostras) - centro
    assert centro > extremos


def test_degenerate_and_inverted_pert_bounds():
    rng = random.Random(7)
    assert amostrar_pert(0.0, 0.0, 0.0, rng) == 0.0
    assert amostrar_pert(12.0, 12.0, 12.0, rng) == 12.0

    with pytest.raises(ValueError, match="PERT"):
        parametros_pert(15.0, 10.0, 20.0)
    with pytest.raises(ValueError, match="PERT"):
        parametros_pert(10.0, 25.0, 20.0)


def test_seed_reproducibility_and_alphanumeric_ff_sf_links():
    atividades = [
        AtividadeCronograma(
            id="ENG-10",
            nome="Projeto",
            dur_o=8.0,
            dur_m=10.0,
            dur_p=16.0,
            baseline_hh=80.0,
            hh_restante=80.0,
            dur_m_explicita=True,
        ),
        AtividadeCronograma(
            id="FAB-20",
            nome="Fabricacao",
            predecessoras="ENG-10FF+2d",
            dur_o=4.0,
            dur_m=6.0,
            dur_p=10.0,
            baseline_hh=60.0,
            hh_restante=60.0,
            dur_m_explicita=True,
        ),
        AtividadeCronograma(
            id="MON-30",
            nome="Montagem",
            predecessoras="FAB-20SF+8d",
            dur_o=4.0,
            dur_m=5.0,
            dur_p=8.0,
            baseline_hh=50.0,
            hh_restante=50.0,
            dur_m_explicita=True,
        ),
    ]

    r1 = simular_cronograma(atividades, prazo=15.0, n_sims=500, seed=99)
    r2 = simular_cronograma(atividades, prazo=15.0, n_sims=500, seed=99)
    assert r1.p50 == r2.p50
    assert r1.p80 == r2.p80
    assert r1.p90 == r2.p90
    assert r1.criticidade == r2.criticidade
    assert all(0.0 <= v <= 1.0 for v in r1.criticidade.values())


def test_risk_weighted_uk_drives_prescription_and_overrides_ok_slack():
    # Atividade com FolgaTotal determinística +8d ("OK"), mas alta variância (DurP=50)
    # torna-se crítica em >= 40% das simulações e recebe prescrição usando uk_hh_ajustado.
    base = AtividadeCronograma(
        id="A1",
        nome="Frente Base",
        folga=0.0,
        dur_o=18.0,
        dur_m=20.0,
        dur_p=22.0,
        baseline_hh=200.0,
        hh_restante=200.0,
        dur_m_explicita=True,
        historico=[2.0],
    )
    arriscada = AtividadeCronograma(
        id="A2",
        nome="Frente Variavel",
        folga=8.0,  # OK deterministicamente (> 5d)
        dur_o=8.0,
        dur_m=12.0,
        dur_p=65.0,
        baseline_hh=400.0,
        hh_restante=400.0,
        dur_m_explicita=True,
        historico=[3.0, 3.0],  # u_dias = 0.5*3 + 0.15*6 = 2.4 -> taxa=400/12 -> uk=80 HH
    )

    res = simular_cronograma([base, arriscada], prazo=20.0, n_sims=1000, seed=42)
    ic_a2 = res.criticidade["A2"]
    assert ic_a2 >= 0.40

    presc_a2 = next(p for p in res.prescricoes if p.atividade.id == "A2")
    assert presc_a2.atividade.criticidade == "OK"
    assert len(presc_a2.acoes) >= 1
    assert presc_a2.uk_hh_ajustado == pytest.approx(
        presc_a2.pid.uk_hh * (0.5 + 0.5 * ic_a2)
    )
    # Confirma que a prescrição usa uk_hh_ajustado e não o uk_hh bruto
    assert f"~{presc_a2.uk_hh_ajustado:.0f} HH" in "\n".join(presc_a2.acoes)
    assert f"~{presc_a2.pid.uk_hh:.0f} HH" not in "\n".join(presc_a2.acoes)


def test_iman_conover_correlation_widens_tail_and_preserves_marginals():
    atividades = [
        AtividadeCronograma(
            id="A1",
            nome="Solda 1",
            grupo_risco="Clima",
            dur_o=6.0,
            dur_m=10.0,
            dur_p=20.0,
            baseline_hh=100.0,
            hh_restante=100.0,
            dur_m_explicita=True,
        ),
        AtividadeCronograma(
            id="A2",
            nome="Solda 2",
            predecessoras="A1FS",
            grupo_risco="Clima",
            dur_o=6.0,
            dur_m=10.0,
            dur_p=20.0,
            baseline_hh=100.0,
            hh_restante=100.0,
            dur_m_explicita=True,
        ),
    ]

    mat_indep = _gerar_matriz_duracoes(atividades, n_sims=1500, seed=42, correlacao=0.0)
    mat_corr = _gerar_matriz_duracoes(atividades, n_sims=1500, seed=42, correlacao=0.7)

    # Preserva exatamente as amostras marginais individuais (multiconjunto idêntico)
    assert sorted(mat_corr["A1"]) == sorted(mat_indep["A1"])
    assert sorted(mat_corr["A2"]) == sorted(mat_indep["A2"])
    # Induz correlação de postos positiva próxima do rho configurado
    assert correlacao_spearman(mat_corr["A1"], mat_corr["A2"]) > 0.60

    r_indep = simular_cronograma(atividades, prazo=20.0, n_sims=1500, seed=42, correlacao=0.0)
    r_corr = simular_cronograma(atividades, prazo=20.0, n_sims=1500, seed=42, correlacao=0.7)
    assert r_corr.p90 > r_indep.p90
    assert r_corr.correlacao == pytest.approx(0.7)

    with pytest.raises(ValueError, match="correlação"):
        simular_cronograma(atividades, prazo=20.0, n_sims=100, correlacao=-0.1)
    with pytest.raises(ValueError, match="correlação"):
        simular_cronograma(atividades, prazo=20.0, n_sims=100, correlacao=1.0)
    with pytest.raises(ValueError, match="Clima"):
        simular_cronograma(atividades, prazo=20.0, n_sims=100, fatores_risco={"Clima": 1.0})


def test_spearman_sensitivity_tornado_and_zero_variance_milestone():
    atividades = [
        AtividadeCronograma(
            id="GARGALO",
            nome="Critica Alta Variancia",
            dur_o=10.0,
            dur_m=15.0,
            dur_p=40.0,
            baseline_hh=150.0,
            hh_restante=150.0,
            dur_m_explicita=True,
        ),
        AtividadeCronograma(
            id="PARALELA",
            nome="Paralela Baixa Variancia",
            dur_o=4.0,
            dur_m=5.0,
            dur_p=7.0,
            baseline_hh=50.0,
            hh_restante=50.0,
            dur_m_explicita=True,
        ),
        AtividadeCronograma(
            id="MARCO",
            nome="Marco Zero",
            predecessoras="GARGALOFS;PARALELAFS",
            dur_o=0.0,
            dur_m=0.0,
            dur_p=0.0,
            marco=True,
        ),
    ]
    res = simular_cronograma(atividades, prazo=15.0, n_sims=1000, seed=7)
    assert res.sensibilidade["GARGALO"] > 0.85
    assert res.sensibilidade["GARGALO"] > res.sensibilidade["PARALELA"]
    assert res.sensibilidade["MARCO"] == 0.0
    # Empates explícitos no Spearman
    assert correlacao_spearman([1.0, 1.0, 3.0], [2.0, 2.0, 4.0]) == pytest.approx(1.0)


def test_closed_loop_converges_for_controllable_internal_chain():
    # Cadeia interna sem HistoricoDesvios; P80 inicial (~31.5d) excede prazo (27d)
    # mas cabe facilmente dentro da redução de +35% HH (30/1.35 = 22.2d).
    atividades = [
        AtividadeCronograma(
            id="I1",
            nome="Fabricacao Pesada",
            tipo="Interna",
            dur_o=18.0,
            dur_m=20.0,
            dur_p=24.0,
            baseline_hh=200.0,
            hh_restante=200.0,
            dur_m_explicita=True,
        ),
        AtividadeCronograma(
            id="I2",
            nome="Montagem Interna",
            tipo="Interna",
            predecessoras="I1FS",
            dur_o=9.0,
            dur_m=10.0,
            dur_p=12.0,
            baseline_hh=100.0,
            hh_restante=100.0,
            dur_m_explicita=True,
        ),
    ]
    res = simular_cronograma(
        atividades,
        prazo=27.0,
        n_sims=800,
        alvo=80.0,
        seed=42,
        fechar_malha=True,
    )
    assert res.resgate is not None
    assert res.resgate.p_alvo_antes > 0.0
    assert res.resgate.convergiu is True
    assert res.resgate.p_alvo_depois <= 0.0
    assert res.resgate.p_alvo_depois < res.resgate.p_alvo_antes
    assert res.resgate.dias_recuperados > 0.0
    assert res.resgate.iteracoes >= 1
    assert res.resgate.gargalos_residuais == []
    assert len(res.resgate.alocacoes) == 2


def test_closed_loop_reports_unrecoverable_external_or_saturated_bottleneck():
    # Frente interna + frente externa crítica com prazo impossível (15d)
    atividades = [
        AtividadeCronograma(
            id="INT1",
            nome="Corte e Dobra",
            tipo="Interna",
            dur_o=18.0,
            dur_m=20.0,
            dur_p=24.0,
            baseline_hh=200.0,
            hh_restante=200.0,
            dur_m_explicita=True,
        ),
        AtividadeCronograma(
            id="EXT1",
            nome="Entrega Fornecedor",
            tipo="Externa",
            predecessoras="INT1FS",
            dur_o=8.0,
            dur_m=10.0,
            dur_p=14.0,
            baseline_hh=40.0,
            hh_restante=40.0,
            dur_m_explicita=True,
        ),
    ]
    res = simular_cronograma(
        atividades,
        prazo=15.0,
        n_sims=500,
        seed=42,
        fechar_malha=True,
    )
    assert res.resgate is not None
    assert res.resgate.convergiu is False
    assert res.resgate.gap_final > 0.0
    gargalos_txt = " ".join(res.resgate.gargalos_residuais)
    assert "EXT1" in gargalos_txt
    assert "INT1" in gargalos_txt


def test_closed_loop_already_on_time_zero_iterations():
    atividades = [
        AtividadeCronograma(
            id="A1",
            nome="Frente Unica",
            dur_o=8.0,
            dur_m=10.0,
            dur_p=12.0,
            baseline_hh=80.0,
            hh_restante=80.0,
            dur_m_explicita=True,
        ),
    ]
    res = simular_cronograma(
        atividades,
        prazo=20.0,
        n_sims=400,
        seed=42,
        fechar_malha=True,
    )
    assert res.resgate is not None
    assert res.resgate.convergiu is True
    assert res.resgate.iteracoes == 0
    assert res.resgate.alocacoes == []
    assert res.resgate.p_alvo_depois == res.resgate.p_alvo_antes


def test_binned_histogram_shared_before_after_axes_and_degenerate():
    atividades = [
        AtividadeCronograma(
            id="I1",
            nome="Frente Controlavel",
            tipo="Interna",
            dur_o=18.0,
            dur_m=22.0,
            dur_p=30.0,
            baseline_hh=220.0,
            hh_restante=220.0,
            dur_m_explicita=True,
        )
    ]
    res = simular_cronograma(
        atividades,
        prazo=19.0,
        n_sims=600,
        seed=42,
        fechar_malha=True,
    )
    assert res.resgate is not None
    assert len(res.histograma) == 12
    assert len(res.resgate.histograma_antes) == 12
    assert len(res.resgate.histograma_depois) == 12
    # Faixas idênticas (eixo X compartilhado Antes × Depois)
    for b_ant, b_dep in zip(res.resgate.histograma_antes, res.resgate.histograma_depois):
        assert b_ant.inicio == b_dep.inicio
        assert b_ant.fim == b_dep.fim
    assert sum(b.contagem for b in res.resgate.histograma_antes) == 600
    assert sum(b.contagem for b in res.resgate.histograma_depois) == 600
    assert not hasattr(res, "fins_obra")

    # Caso degenerado com variância zero (DurO == DurM == DurP) gera 1 faixa
    determ = [
        AtividadeCronograma(
            id="D1",
            nome="Fixa",
            dur_o=10.0,
            dur_m=10.0,
            dur_p=10.0,
            baseline_hh=80.0,
            hh_restante=80.0,
            dur_m_explicita=True,
        )
    ]
    res_deg = simular_cronograma(determ, prazo=12.0, n_sims=200, seed=7)
    assert len(res_deg.histograma) == 1
    assert res_deg.histograma[0].inicio == pytest.approx(10.0)
    assert res_deg.histograma[0].fim == pytest.approx(10.0)
    assert res_deg.histograma[0].contagem == 200

    assert construir_histograma([]) == []
