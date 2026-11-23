"""Testes unitários da otimização custo x prazo em R$/dia e validação de parâmetros."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from resgate_cronograma.cost import (
    avaliar_plano_custo_cpm,
    avaliar_plano_custo_mc,
    carregar_parametros_custo,
)
from resgate_cronograma.models import (
    AlocacaoResgate,
    AtividadeCronograma,
    PrescricaoRisco,
    ResultadoMonteCarlo,
    ResultadoPID,
    ResultadoResgate,
)
from resgate_cronograma.reporting import formatar_relatorio_cpm_pid


def test_carregar_parametros_custo_overrides_e_validacao(tmp_path: Path):
    p_def = carregar_parametros_custo(None)
    assert p_def.custo_hh_hora_extra == 120.0
    assert p_def.ganho_max_ft_pct == 0.20

    # JSON inline parcial preserva padrões restantes
    p_inline = carregar_parametros_custo(
        '{"custo_hh_hora_extra": 150.0, "custo_fixo_fast_tracking": 800.0}'
    )
    assert p_inline.custo_hh_hora_extra == 150.0
    assert p_inline.custo_fixo_fast_tracking == 800.0
    assert p_inline.custo_hh_base == 80.0

    # Arquivo .json parcial
    cfg = tmp_path / "custos.json"
    cfg.write_text(json.dumps({"custo_hh_segundo_turno": 200.0}), encoding="utf-8")
    p_arq = carregar_parametros_custo(str(cfg))
    assert p_arq.custo_hh_segundo_turno == 200.0
    assert p_arq.custo_hh_hora_extra == 120.0

    # Erros de validação
    with pytest.raises(ValueError, match=">= 0"):
        carregar_parametros_custo('{"custo_hh_hora_extra": -10}')
    with pytest.raises(ValueError, match="desconhecido"):
        carregar_parametros_custo('{"taxa_inventada": 10}')
    with pytest.raises(ValueError, match="fator_risco_retrabalho_ft"):
        carregar_parametros_custo('{"fator_risco_retrabalho_ft": 1.5}')
    with pytest.raises(ValueError, match="ganho_max_ft_pct"):
        carregar_parametros_custo('{"ganho_max_ft_pct": 0.0}')
    with pytest.raises(ValueError, match="numérico"):
        carregar_parametros_custo('{"custo_hh_base": true}')
    with pytest.raises(ValueError):
        carregar_parametros_custo("arquivo_inexistente.json")


def test_ranking_custo_por_dia_e_restricoes_fs_e_externa():
    # A1 saturada com ligação FS real entre A1 e A2 -> avalia FT, HE e 2º Turno
    # S1 possui apenas SS -> exclui Fast-tracking (FS -> SS)
    # E1 externa crítica -> Diligenciamento Externo com custo_por_dia_brl = None por último
    a1 = AtividadeCronograma(
        id="A1",
        nome="Fabricacao Pesada",
        tipo="Interna",
        predecessoras="",
        folga=-10.0,
        baseline_hh=200.0,
        hh_restante=200.0,
        dur_o=16.0,
        dur_m=20.0,
        dur_p=28.0,
        dur_m_explicita=True,
        historico=[10.0, 12.0, 15.0],  # satura teto +35% = 70 HH
    )
    a2 = AtividadeCronograma(
        id="A2",
        nome="Sucessora FS",
        tipo="Interna",
        predecessoras="A1FS",
        folga=-10.0,
        baseline_hh=80.0,
        hh_restante=80.0,
        dur_o=8.0,
        dur_m=10.0,
        dur_p=14.0,
        dur_m_explicita=True,
        historico=[5.0],
    )
    s1 = AtividadeCronograma(
        id="S1",
        nome="Montagem SS",
        tipo="Interna",
        predecessoras="A2SS+2d",
        folga=-3.0,
        baseline_hh=120.0,
        hh_restante=120.0,
        dur_o=8.0,
        dur_m=12.0,
        dur_p=16.0,
        dur_m_explicita=True,
        historico=[4.0],
    )
    e1 = AtividadeCronograma(
        id="E1",
        nome="Inspecao Valvulas",
        tipo="Externa",
        predecessoras="S1FF",
        folga=-4.0,
        baseline_hh=40.0,
        hh_restante=40.0,
        dur_o=4.0,
        dur_m=5.0,
        dur_p=8.0,
        dur_m_explicita=True,
        historico=[4.0],
    )

    plano = avaliar_plano_custo_cpm([a1, a2, s1, e1])
    opcoes_a1 = [o for o in plano.opcoes if o.atividade_id == "A1"]
    assert {o.estrategia for o in opcoes_a1} == {
        "Fast-tracking (FS -> SS)",
        "Crashing (Hora Extra)",
        "2º Turno / Terceirização",
    }

    opcoes_s1 = [o for o in plano.opcoes if o.atividade_id == "S1"]
    assert all("Fast-tracking" not in o.estrategia for o in opcoes_s1)

    # Ordenação ascendente por custo_por_dia_brl entre quantificadas, externas ao final
    quantificadas = [o for o in plano.opcoes if o.custo_por_dia_brl is not None]
    externas = [o for o in plano.opcoes if o.custo_por_dia_brl is None]
    assert quantificadas == sorted(quantificadas, key=lambda o: o.custo_por_dia_brl or 0.0)
    assert len(externas) == 1
    assert externas[0].atividade_id == "E1"
    assert plano.opcoes[-1] == externas[0]

    texto = formatar_relatorio_cpm_pid([a1, a2, s1, e1], plano_custo=plano)
    assert "RANKING CUSTO × PRAZO" in texto
    assert "Fast-tracking (FS -> SS)" in texto


def test_orcamento_exato_malha_fechada_monte_carlo():
    a1 = AtividadeCronograma(
        id="I1",
        nome="Caldeiraria",
        tipo="Interna",
        folga=-4.0,
        baseline_hh=300.0,
        hh_restante=300.0,
        dur_o=15.0,
        dur_m=20.0,
        dur_p=30.0,
        dur_m_explicita=True,
    )
    a2 = AtividadeCronograma(
        id="I2",
        nome="Acabamento",
        tipo="Interna",
        predecessoras="I1FS",
        folga=-4.0,
        baseline_hh=200.0,
        hh_restante=200.0,
        dur_o=8.0,
        dur_m=10.0,
        dur_p=15.0,
        dur_m_explicita=True,
    )

    pid_inativo = ResultadoPID(
        u_dias=0.0,
        uk_hh=0.0,
        taxa_hh_dia=15.0,
        dur_ref=20.0,
        hh_ref=300.0,
        teto_hh=105.0,
        p_dias=0.0,
        i_dias=0.0,
        d_dias=0.0,
        inativo=True,
    )
    res_mc = ResultadoMonteCarlo(
        p50=2.0,
        p80=5.0,
        p90=7.0,
        p_alvo=5.0,
        alvo=80.0,
        prazo=25.0,
        n_sims=500,
        seed=42,
        criticidade={"I1": 0.9, "I2": 0.8},
        missing_predecessors=[],
        prescricoes=[
            PrescricaoRisco(a1, 0.9, pid_inativo, 0.0, []),
            PrescricaoRisco(a2, 0.8, pid_inativo, 0.0, []),
        ],
        sensibilidade={"I1": 0.7, "I2": 0.5},
        resgate=ResultadoResgate(
            p50_antes=2.0,
            p80_antes=5.0,
            p90_antes=7.0,
            p_alvo_antes=5.0,
            p50_depois=-1.0,
            p80_depois=0.0,
            p90_depois=1.5,
            p_alvo_depois=0.0,
            gap_inicial=5.0,
            gap_final=0.0,
            dias_recuperados=5.0,
            iteracoes=2,
            convergiu=True,
            gargalos_residuais=[],
            alocacoes=[
                AlocacaoResgate("I1", "Caldeiraria", "Interna", 0.9, 0.7, 60.0, 0.8333, 20.0, 16.67),
                AlocacaoResgate("I2", "Acabamento", "Interna", 0.8, 0.5, 40.0, 0.8333, 10.0, 8.33),
            ],
        ),
    )

    plano = avaliar_plano_custo_mc(
        [a1, a2],
        res_mc,
        parametros='{"custo_hh_hora_extra": 120.0}',
    )
    assert plano.custo_total_malha_fechada_brl == pytest.approx(12000.0)
    assert sum(av.custo_brl for av in plano.alocacoes_valoradas) == pytest.approx(12000.0)
