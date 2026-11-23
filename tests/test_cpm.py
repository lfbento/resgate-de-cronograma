"""Testes unitários do motor CPM (resgate_cronograma.cpm)."""
from __future__ import annotations

import pytest

from resgate_cronograma.cpm import RedeCPM, aplicar_folgas_cpm
from resgate_cronograma.models import AtividadeCronograma, ScheduleCycleError


def test_topological_sort_diamond_reusable():
    atividades = [
        AtividadeCronograma(id="D", nome="Fim", predecessoras="B;C", dur_m=4),
        AtividadeCronograma(id="B", nome="Frente B", predecessoras="A", dur_m=6),
        AtividadeCronograma(id="C", nome="Frente C", predecessoras="A", dur_m=8),
        AtividadeCronograma(id="A", nome="Inicio", predecessoras="", dur_m=5),
    ]
    rede = RedeCPM(atividades)
    ordem = rede.ordem_topologica
    assert ordem[0] == "A"
    assert ordem[-1] == "D"
    assert set(ordem[1:3]) == {"B", "C"}

    res1 = rede.avaliar()
    res2 = rede.avaliar(duracoes={"A": 5, "B": 10, "C": 3, "D": 4})
    assert res1.duracao_projeto == pytest.approx(17.0)
    assert res2.duracao_projeto == pytest.approx(19.0)
    assert res1.ordem_topologica == res2.ordem_topologica == ordem


def test_mixed_ss_ff_sf_relationships_and_lags():
    atividades = [
        AtividadeCronograma(id="A", nome="Base", predecessoras="", dur_m=10),
        AtividadeCronograma(id="B", nome="Paralela SS", predecessoras="ASS+2d", dur_m=6),
        AtividadeCronograma(id="C", nome="Paralela FF", predecessoras="AFF+1d", dur_m=4),
        AtividadeCronograma(id="D", nome="Fechamento SF", predecessoras="CSF+8d", dur_m=5),
    ]
    rede = RedeCPM(atividades)
    res = rede.avaliar()

    assert res.es["A"] == pytest.approx(0.0)
    assert res.ef["A"] == pytest.approx(10.0)
    assert res.es["B"] == pytest.approx(2.0)
    assert res.ef["B"] == pytest.approx(8.0)
    assert res.es["C"] == pytest.approx(7.0)
    assert res.ef["C"] == pytest.approx(11.0)
    assert res.es["D"] == pytest.approx(10.0)
    assert res.ef["D"] == pytest.approx(15.0)
    assert res.duracao_projeto == pytest.approx(15.0)

    assert res.folga_total["A"] == pytest.approx(0.0)
    assert res.folga_total["C"] == pytest.approx(0.0)
    assert res.folga_total["D"] == pytest.approx(0.0)
    assert res.folga_total["B"] == pytest.approx(7.0)
    assert all(res.es[k] >= 0.0 for k in res.es)
    assert all(res.lf[k] <= res.duracao_projeto for k in res.lf)


def test_negative_lag_lead_fs():
    atividades = [
        AtividadeCronograma(id="A", nome="Etapa 1", dur_m=10),
        AtividadeCronograma(id="B", nome="Etapa 2", predecessoras="AFS-3d", dur_m=5),
    ]
    res = RedeCPM(atividades).avaliar()
    assert res.es["B"] == pytest.approx(7.0)
    assert res.ef["B"] == pytest.approx(12.0)
    assert res.duracao_projeto == pytest.approx(12.0)


def test_contractual_deadline_and_imported_slack_preservation():
    atividades = [
        AtividadeCronograma(id="1", nome="Solda", folga=-8.0, dur_m=30, folga_importada=True),
        AtividadeCronograma(id="2", nome="Montagem", predecessoras="1FS", folga=-8.0, dur_m=23, folga_importada=True),
    ]
    # Sem prazo: preserva FolgaTotal importada (-8)
    aplicar_folgas_cpm(atividades, prazo=None)
    assert atividades[0].folga == pytest.approx(-8.0)
    assert atividades[1].folga == pytest.approx(-8.0)

    # Com prazo mais apertado (53 vs 45): recalcula folga negativa (-8)
    res_prazo = aplicar_folgas_cpm(atividades, prazo=45.0)
    assert res_prazo.duracao_projeto == pytest.approx(53.0)
    assert res_prazo.folga_total["1"] == pytest.approx(-8.0)
    assert atividades[0].folga == pytest.approx(-8.0)


def test_missing_external_predecessors_anchored_at_zero():
    atividades = [
        AtividadeCronograma(id="42", nome="Soldagem", predecessoras="40FS", dur_m=22),
        AtividadeCronograma(id="45", nome="Montagem", predecessoras="42FS;41SS+3d", dur_m=18),
    ]
    rede = RedeCPM(atividades)
    res = rede.avaliar()

    assert rede.missing_predecessors == ["40", "41"]
    assert res.missing_predecessors == ["40", "41"]
    assert res.es["42"] == pytest.approx(0.0)
    assert res.es["45"] == pytest.approx(22.0)

    # Quando só existe a âncora externa SS+3d, aplica 0 + 3 = 3d
    somente_45 = [AtividadeCronograma(id="45", nome="Montagem", predecessoras="41SS+3d", dur_m=18)]
    res_45 = RedeCPM(somente_45).avaliar()
    assert res_45.es["45"] == pytest.approx(3.0)


def test_cycle_raises_explicit_error_with_node_ids():
    # Ciclo de 2 nós + nó dependente downstream (não deve contaminar os nós do ciclo)
    ciclo2 = [
        AtividadeCronograma(id="A", nome="A", predecessoras="BFS", dur_m=5),
        AtividadeCronograma(id="B", nome="B", predecessoras="AFS", dur_m=5),
        AtividadeCronograma(id="C", nome="Downstream", predecessoras="BFS", dur_m=5),
    ]
    with pytest.raises(ScheduleCycleError) as exc2:
        RedeCPM(ciclo2)
    assert "A" in exc2.value.cycle_nodes and "B" in exc2.value.cycle_nodes
    assert "C" not in exc2.value.cycle_nodes
    assert "A" in str(exc2.value) and "B" in str(exc2.value)

    # Ciclo de 3 nós
    ciclo3 = [
        AtividadeCronograma(id="T1", nome="T1", predecessoras="T3FS", dur_m=3),
        AtividadeCronograma(id="T2", nome="T2", predecessoras="T1FS", dur_m=3),
        AtividadeCronograma(id="T3", nome="T3", predecessoras="T2FS", dur_m=3),
    ]
    with pytest.raises(ScheduleCycleError) as exc3:
        RedeCPM(ciclo3)
    assert {"T1", "T2", "T3"}.issubset(set(exc3.value.cycle_nodes))
