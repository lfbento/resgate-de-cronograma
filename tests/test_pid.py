"""Testes unitários do controlador PID Modelo 1 e prescritor."""
from __future__ import annotations

import pytest

from resgate_cronograma.cpm import RedeCPM
from resgate_cronograma.models import AtividadeCronograma
from resgate_cronograma.pid import (
    KI,
    alocar_gap_probabilistico,
    calcular_pid,
    calcular_taxa_e_ref,
    comprimir_pert_por_esforco,
    pid_por_frente,
    prescrever,
)


def test_modelo1_dimensional_coherence_with_explicit_dur_m():
    frente = AtividadeCronograma(
        id="42",
        nome="Soldagem Lote B",
        tipo="Interna",
        predecessoras="40FS",
        folga=-8.0,
        baseline_hh=320.0,
        hh_realizado=180.0,
        hh_restante=220.0,
        historico=[4.0, 6.0, 8.0],
        dur_m=22.0,
        dur_m_explicita=True,
    )
    res = calcular_pid(frente)
    assert res.taxa_hh_dia == pytest.approx(10.0)
    assert res.u_dias == pytest.approx(7.2)
    assert res.uk_hh == pytest.approx(72.0)
    assert res.saturado is False


def test_nine_column_csv_fallback_without_dur_m():
    frente = AtividadeCronograma(
        id="42",
        nome="Soldagem Lote B",
        tipo="Interna",
        folga=-8.0,
        baseline_hh=320.0,
        hh_realizado=180.0,
        hh_restante=220.0,
        historico=[4.0, 6.0, 8.0],
        dur_m=27.5,
        dur_m_explicita=False,
    )
    taxa, dur_ref, hh_ref = calcular_taxa_e_ref(frente)
    assert taxa == pytest.approx(8.0)
    assert dur_ref == pytest.approx(27.5)
    assert hh_ref == pytest.approx(220.0)

    res = calcular_pid(frente)
    assert res.uk_hh == pytest.approx(7.2 * 8.0)


def test_dimensional_deadband_suppression():
    frente_100d = AtividadeCronograma(
        id="10",
        nome="Montagem Longa",
        folga=0.0,
        baseline_hh=800.0,
        hh_restante=800.0,
        dur_m=100.0,
        dur_m_explicita=True,
        historico=[3.0, 1.0],  # |1| / 100 = 0.01 < 0.015
    )
    res = calcular_pid(frente_100d)
    assert res.deadband is True
    assert res.uk_hh == 0.0
    acoes = prescrever(frente_100d, res.uk_hh, res)
    assert len(acoes) == 1 and "deadband" in acoes[0].lower()

    frente_zero = AtividadeCronograma(
        id="55",
        nome="Inspecao",
        tipo="Externa",
        folga=0.0,
        baseline_hh=30.0,
        hh_restante=30.0,
        historico=[0.0],
    )
    res_zero = calcular_pid(frente_zero)
    assert res_zero.deadband is True


def test_iterative_anti_windup_freeze_and_unwind():
    # HHRestante=100 => teto_hh=35 => teto_dias=3.5 (com taxa=10 HH/d)
    frente_curta = AtividadeCronograma(
        id="S1",
        nome="Frente Saturada",
        folga=-5.0,
        baseline_hh=200.0,
        hh_restante=100.0,
        dur_m=10.0,
        dur_m_explicita=True,
        historico=[10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
    )
    res_sat = calcular_pid(frente_curta)
    assert res_sat.saturado is True
    assert res_sat.uk_hh == pytest.approx(35.0)
    # No ciclo 0 já satura (0.5*10 + 0.15*10 = 6.5 > 3.5), congelando integral em 0.0
    assert res_sat.i_dias == pytest.approx(0.0)

    # Satura após acumular integral e depois recebe erro negativo que descarrega
    frente_unwind = AtividadeCronograma(
        id="S2",
        nome="Frente Recuperacao",
        folga=-2.0,
        baseline_hh=200.0,
        hh_restante=100.0,
        dur_m=10.0,
        dur_m_explicita=True,
        historico=[2.0, 3.0, 10.0, 10.0],
    )
    res_pos = calcular_pid(frente_unwind)
    assert res_pos.saturado is True
    i_congelado = res_pos.i_dias
    assert i_congelado == pytest.approx(KI * (2.0 + 3.0))

    frente_unwind.historico.append(-2.0)
    res_descarregado = calcular_pid(frente_unwind)
    assert res_descarregado.i_dias < i_congelado


def test_prescrever_fs_vs_ss_negative_uk_external_and_risk_override():
    # SS puro NÃO deve recomendar fast-tracking FS -> SS
    frente_ss = AtividadeCronograma(
        id="45",
        nome="Montagem Estrutura",
        tipo="Interna",
        predecessoras="41SS+3d",
        folga=-8.0,
        baseline_hh=260.0,
        hh_restante=240.0,
        dur_m=30.0,
        dur_m_explicita=True,
        historico=[3.0, 5.0, 8.0],
    )
    rede = RedeCPM([frente_ss])
    uk, det = pid_por_frente(frente_ss)
    acoes_ss = prescrever(frente_ss, uk, det, tem_fs=rede.tem_ligacao_fs(frente_ss.id))
    assert all("Fast-tracking" not in a for a in acoes_ss)

    # Com sucessora FS real, tem_ligacao_fs passa a recomendar fast-tracking
    suc_fs = AtividadeCronograma(
        id="55",
        nome="Pintura",
        tipo="Interna",
        predecessoras="45FS",
        folga=0.0,
        baseline_hh=40.0,
        hh_restante=40.0,
        dur_m=5.0,
    )
    rede2 = RedeCPM([frente_ss, suc_fs])
    acoes_com_fs = prescrever(frente_ss, uk, det, tem_fs=rede2.tem_ligacao_fs("45"))
    assert any("Fast-tracking" in a for a in acoes_com_fs)

    # uk < 0: mensagem de adiantamento, sem '+0.0 profissional'
    frente_adiantada = AtividadeCronograma(
        id="A1",
        nome="Usinagem",
        folga=0.0,
        baseline_hh=160.0,
        hh_restante=160.0,
        dur_m=20.0,
        dur_m_explicita=True,
        historico=[2.0, -4.0],
    )
    res_neg = calcular_pid(frente_adiantada)
    assert res_neg.uk_hh < 0
    acoes_neg = prescrever(frente_adiantada, res_neg.uk_hh, res_neg)
    assert any("adiantada" in a.lower() for a in acoes_neg)
    assert all("+0.0 profissional" not in a for a in acoes_neg)

    # Externa nunca prescreve hora extra
    frente_ext = AtividadeCronograma(
        id="48",
        nome="Compra de Valvulas",
        tipo="Externa",
        folga=-2.0,
        baseline_hh=40.0,
        hh_restante=20.0,
        dur_m=16.0,
        dur_m_explicita=True,
        historico=[1.0, 2.0],
    )
    res_ext = calcular_pid(frente_ext)
    acoes_ext = prescrever(frente_ext, res_ext.uk_hh, res_ext)
    assert any("INÓCUA" in a for a in acoes_ext)
    assert any("ALERTA" in a for a in acoes_ext)

    # Frente OK só ativa com forcado_por_risco=True
    frente_ok = AtividadeCronograma(
        id="OK1",
        nome="Caldeiraria",
        folga=8.0,
        baseline_hh=100.0,
        hh_restante=100.0,
        dur_m=10.0,
        dur_m_explicita=True,
        historico=[2.0, 4.0],
    )
    res_ok = calcular_pid(frente_ok)
    assert prescrever(frente_ok, res_ok.uk_hh, res_ok, forcado_por_risco=False) == []
    assert len(prescrever(frente_ok, res_ok.uk_hh, res_ok, forcado_por_risco=True)) >= 1


def test_ciclo_replay_and_validation():
    frente = AtividadeCronograma(
        id="42",
        nome="Soldagem",
        folga=-8.0,
        baseline_hh=320.0,
        hh_restante=220.0,
        dur_m=22.0,
        dur_m_explicita=True,
        historico=[4.0, 6.0, 8.0],
    )
    res_c2 = calcular_pid(frente, ciclo=2)
    res_c3 = calcular_pid(frente, ciclo=3)
    assert res_c2.uk_hh == pytest.approx(50.0)  # (0.5*6 + 0.15*10 + 0.25*2) * 10 = 5.0 * 10 = 50
    assert res_c3.uk_hh == pytest.approx(72.0)

    with pytest.raises(ValueError, match="ciclo"):
        calcular_pid(frente, ciclo=0)


def test_alocar_gap_probabilistico_chain_and_excludes_external():
    i1 = AtividadeCronograma(
        id="I1",
        nome="Caldeiraria",
        tipo="Interna",
        dur_m=20.0,
        baseline_hh=200.0,
        hh_restante=200.0,
        dur_m_explicita=True,
        historico=[],  # Sem histórico prévio
    )
    i2 = AtividadeCronograma(
        id="I2",
        nome="Montagem",
        tipo="Interna",
        predecessoras="I1FS",
        dur_m=10.0,
        baseline_hh=100.0,
        hh_restante=100.0,
        dur_m_explicita=True,
        historico=[],
    )
    ext = AtividadeCronograma(
        id="E1",
        nome="Fornecedor",
        tipo="Externa",
        predecessoras="I2FS",
        dur_m=10.0,
        baseline_hh=40.0,
        hh_restante=40.0,
        dur_m_explicita=True,
        historico=[],
    )

    gaps = alocar_gap_probabilistico(
        [i1, i2, ext],
        criticidade={"I1": 1.0, "I2": 1.0, "E1": 1.0},
        sensibilidade={"I1": 0.6, "I2": 0.6, "E1": 0.6},
        gap_dias=6.0,
    )
    assert "E1" not in gaps
    assert gaps["I1"] + gaps["I2"] == pytest.approx(6.0)
    assert gaps["I1"] == pytest.approx(4.0)
    assert gaps["I2"] == pytest.approx(2.0)

    # Alimentando o PID sem HistoricoDesvios prévio gera atuação limitada a +35%
    i1_copia = AtividadeCronograma(
        id=i1.id,
        nome=i1.nome,
        tipo=i1.tipo,
        dur_m=i1.dur_m,
        baseline_hh=i1.baseline_hh,
        hh_restante=i1.hh_restante,
        dur_m_explicita=True,
        historico=[gaps["I1"]],
    )
    pid_i1 = calcular_pid(i1_copia, ignorar_deadband=True)
    assert 0.0 < pid_i1.uk_hh <= 0.35 * i1.hh_restante


def test_comprimir_pert_por_esforco_and_validation():
    frente = AtividadeCronograma(
        id="42",
        nome="Soldagem",
        baseline_hh=320.0,
        hh_restante=220.0,
        dur_o=18.0,
        dur_m=22.0,
        dur_p=32.0,
        dur_m_explicita=True,
    )
    o2, m2, p2, fator = comprimir_pert_por_esforco(frente, uk_hh=77.0, eficiencia=1.0)
    assert fator == pytest.approx(1.0 / 1.35)
    assert m2 == pytest.approx(22.0 / 1.35)
    assert 0.0 < o2 <= m2 <= p2

    with pytest.raises(ValueError, match="Eficiência"):
        comprimir_pert_por_esforco(frente, uk_hh=10.0, eficiencia=0.0)
    with pytest.raises(ValueError, match="Eficiência"):
        comprimir_pert_por_esforco(frente, uk_hh=10.0, eficiencia=1.2)
