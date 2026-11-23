"""Testes unitários dos leitores (.xer, .xml MSPDI, .csv) e classificação."""
from __future__ import annotations

from pathlib import Path

import pytest

from resgate_cronograma.parsers import (
    carregar_csv,
    classificar_tipo,
    escrever_csv,
    parse_mspdi,
    parse_xer,
)


def test_classification_accent_insensitive_and_overrides():
    assert classificar_tipo("Inspeção de Válvulas") == "Externa"
    assert classificar_tipo("Aprovação Documentação") == "Externa"
    assert classificar_tipo("Soldagem Lote B") == "Interna"
    assert classificar_tipo("Landlord approval of the fit-out") == "Externa"
    assert classificar_tipo("Lift manufacture and delivery") == "Externa"
    assert classificar_tipo("External cladding") == "Interna"
    assert classificar_tipo("Compra Interna Emergencial", tipo_explicito="Interna") == "Interna"
    assert classificar_tipo("Soldagem Lote B", overrides={"42": "Externa"}, task_id="42") == "Externa"


def test_csv_legacy_9col_typo_and_mixed_links(tmp_path: Path):
    csv_path = tmp_path / "legado.csv"
    csv_path.write_text(
        "ID,Nome,Tipo,Predecessoras,FolgaTotal,BaselineHH,HHRealizado,HHRrestante,HistoricoDesvios\n"
        "ENG-1,Projeto,Interna,,-8d,160,80,80,2;4\n"
        "FAB-2,Fabricacao,,ENG-1FF-1.5d;41SS+3d,-4,200,50,150,1;3\n"
        "M-0,Marco Zero,Interna,FAB-2FS,0,0,0,0,\n",
        encoding="utf-8",
    )

    atividades = carregar_csv(csv_path)
    assert [a.id for a in atividades] == ["ENG-1", "FAB-2", "M-0"]
    assert atividades[0].hh_restante == pytest.approx(80.0)
    assert atividades[0].dur_m_explicita is False
    assert [(d.pred_id, d.tipo, d.lag_dias) for d in atividades[1].dependencias] == [
        ("ENG-1", "FF", -1.5),
        ("41", "SS", 3.0),
    ]
    assert atividades[1].historico == [1.0, 3.0]
    assert atividades[2].marco is True
    assert (atividades[2].dur_o, atividades[2].dur_m, atividades[2].dur_p) == (0.0, 0.0, 0.0)

    out_csv = tmp_path / "saida_12col.csv"
    escrever_csv(atividades, out_csv)
    recarregado = carregar_csv(out_csv)
    assert len(recarregado) == 3
    assert recarregado[2].dur_o == 0.0 and recarregado[2].dur_p == 0.0


def test_xer_filtering_taskrsrc_complete_milestone_and_links(tmp_path: Path):
    xer_path = tmp_path / "real.xer"
    xer_path.write_text(
        "\n".join(
            [
                "%T\tTASK",
                "%F\ttask_id\ttask_code\ttask_name\ttask_type\tstatus_code\ttarget_drtn_hr_cnt\tremain_drtn_hr_cnt\ttotal_float_hr_cnt",
                "%R\t1\tWBS-01\tResumo WBS\tTT_WBS\tTK_Active\t400\t200\t0",
                "%R\t2\tLOE-01\tAcompanhamento\tTT_LOE\tTK_Active\t160\t80\t0",
                "%R\t3\tENG-10\tEngenharia\tTT_Task\tTK_Complete\t80\t80\t0",
                "%R\t4\tENG-101\tSolda Critica\tTT_Task\tTK_Active\t160\t120\t-32",
                "%R\t5\tM-100\tEntrega Final\tTT_FinMile\tTK_NotStart\t0\t0\t0",
                "%T\tTASKRSRC",
                "%F\ttaskrsrc_id\ttask_id\ttarget_qty\tact_reg_qty\tact_ot_qty\tremain_qty",
                "%R\t10\t4\t320\t160\t20\t220",
                "%T\tTASKPRED",
                "%F\ttask_pred_id\ttask_id\tpred_task_id\tpred_type\tpred_lag_hr_cnt",
                "%R\t20\t4\t3\tPR_FF\t-16",
                "%R\t21\t5\t3\tPR_SF\t8",
                "%E",
            ]
        ),
        encoding="utf-8",
    )

    atividades = parse_xer(xer_path)
    por_id = {a.id: a for a in atividades}
    assert set(por_id) == {"ENG-10", "ENG-101", "M-100"}

    # TK_Complete zera duração restante
    assert por_id["ENG-10"].concluida is True
    assert por_id["ENG-10"].dur_m == 0.0

    # TASKRSRC sobrepõe HH da duração e preserva PR_FF com lag negativo
    solda = por_id["ENG-101"]
    assert solda.baseline_hh == pytest.approx(320.0)
    assert solda.hh_realizado == pytest.approx(180.0)
    assert solda.hh_restante == pytest.approx(220.0)
    assert solda.folga == pytest.approx(-4.0)
    assert [(d.pred_id, d.tipo, d.lag_dias) for d in solda.dependencias] == [("ENG-10", "FF", -2.0)]

    # Marco preservado com PERT 0,0,0 e PR_SF
    marco = por_id["M-100"]
    assert marco.marco is True
    assert (marco.dur_o, marco.dur_m, marco.dur_p) == (0.0, 0.0, 0.0)
    assert [(d.pred_id, d.tipo, d.lag_dias) for d in marco.dependencias] == [("ENG-10", "SF", 1.0)]


def test_xer_missing_task_table_raises(tmp_path: Path):
    bad_xer = tmp_path / "sem_task.xer"
    bad_xer.write_text("%T\tHDR\n%F\texport_type\n%R\tBASELINE\n%E\n", encoding="utf-8")
    with pytest.raises(ValueError, match="TASK"):
        parse_xer(bad_xer)


def test_mspdi_filters_summary_uid0_work_tenths_and_lagformat(tmp_path: Path):
    xml_path = tmp_path / "real_msp.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<Project xmlns="http://schemas.microsoft.com/project">
  <MinutesPerDay>480</MinutesPerDay>
  <Tasks>
    <Task><UID>0</UID><TaskID>0</TaskID><Name>Projeto Raiz</Name><Summary>1</Summary></Task>
    <Task><UID>1</UID><TaskID>1</TaskID><Name>Fase 1</Name><Summary>1</Summary></Task>
    <Task>
      <UID>2</UID><TaskID>T-10</TaskID><Name>Fabricacao Modulo</Name><Summary>0</Summary>
      <Duration>PT80H</Duration><RemainingDuration>PT80H</RemainingDuration>
      <Work>PT240H</Work><ActualWork>PT80H</ActualWork><RemainingWork>PT160H</RemainingWork>
      <TotalSlack>-38400</TotalSlack>
    </Task>
    <Task>
      <UID>3</UID><TaskID>T-20</TaskID><Name>Montagem Campo</Name><Summary>0</Summary>
      <Duration>PT40H</Duration><RemainingDuration>PT40H</RemainingDuration>
      <TotalSlack>0</TotalSlack>
      <PredecessorLink><PredecessorUID>2</PredecessorUID><Type>3</Type><Lag>14400</Lag></PredecessorLink>
      <PredecessorLink><PredecessorUID>2</PredecessorUID><Type>0</Type><Lag>20</Lag><LagFormat>19</LagFormat></PredecessorLink>
    </Task>
    <Task>
      <UID>4</UID><TaskID>M-99</TaskID><Name>Aceite Final</Name><Summary>0</Summary><Milestone>1</Milestone>
      <Duration>PT0H</Duration><RemainingDuration>PT0H</RemainingDuration><TotalSlack>0</TotalSlack>
    </Task>
  </Tasks>
</Project>
""",
        encoding="utf-8",
    )

    atividades = parse_mspdi(xml_path)
    por_id = {a.id: a for a in atividades}
    assert set(por_id) == {"T-10", "T-20", "M-99"}

    t10 = por_id["T-10"]
    assert t10.folga == pytest.approx(-8.0)
    assert t10.baseline_hh == pytest.approx(240.0)
    assert t10.hh_realizado == pytest.approx(80.0)
    assert t10.hh_restante == pytest.approx(160.0)

    t20 = por_id["T-20"]
    assert [(d.pred_id, d.tipo, d.lag_dias) for d in t20.dependencias] == [
        ("T-10", "SS", 3.0),
        ("T-10", "FF", 2.0),  # 20% de 10d = 2d
    ]

    m99 = por_id["M-99"]
    assert m99.marco is True
    assert (m99.dur_o, m99.dur_m, m99.dur_p) == (0.0, 0.0, 0.0)


def test_mspdi_percent_complete_remaining_and_extended_pert(tmp_path: Path):
    xml_path = tmp_path / "real_pert.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<Project xmlns="http://schemas.microsoft.com/project">
  <ExtendedAttributes>
    <ExtendedAttribute><FieldID>188743783</FieldID><FieldName>Duration1</FieldName><Alias>Optimistic Duration</Alias></ExtendedAttribute>
    <ExtendedAttribute><FieldID>188743785</FieldID><FieldName>Duration3</FieldName><Alias>Pessimistic Duration</Alias></ExtendedAttribute>
  </ExtendedAttributes>
  <Tasks>
    <Task><UID>0</UID><Name>Root</Name><Summary>1</Summary></Task>
    <Task>
      <UID>10</UID><Name>Steel frame erection</Name><Summary>0</Summary>
      <Duration>PT80H0M0S</Duration><PercentComplete>25</PercentComplete>
      <Work>PT160H0M0S</Work><ActualWork>PT40H0M0S</ActualWork><RemainingWork>PT120H0M0S</RemainingWork>
      <ExtendedAttribute><FieldID>188743783</FieldID><Value>PT64H0M0S</Value></ExtendedAttribute>
      <ExtendedAttribute><FieldID>188743785</FieldID><Value>PT120H0M0S</Value></ExtendedAttribute>
    </Task>
    <Task>
      <UID>11</UID><Name>Follow-up task</Name><Summary>0</Summary>
      <Duration>PT40H0M0S</Duration><PercentComplete>0</PercentComplete>
      <PredecessorLink><PredecessorUID>10</PredecessorUID><Type>1</Type></PredecessorLink>
    </Task>
  </Tasks>
</Project>
""",
        encoding="utf-8",
    )

    ats = {a.id: a for a in parse_mspdi(xml_path)}
    t10 = ats["10"]
    # 10d * (1 - 0.25) = 7.5d; PERT proporcional: 8d*0.75=6.0d, 15d*0.75=11.2d
    assert t10.dur_m == pytest.approx(7.5)
    assert t10.dur_o == pytest.approx(6.0)
    assert t10.dur_p == pytest.approx(11.2)
    assert [(d.pred_id, d.tipo) for d in ats["11"].dependencias] == [("10", "FS")]


def test_sample_xer_and_xml_fixtures_parse(amostra_xer: Path, amostra_xml: Path):
    xer_ativs = parse_xer(amostra_xer)
    xml_ativs = parse_mspdi(amostra_xml)
    assert len(xer_ativs) == 5
    assert len(xml_ativs) == 5
    assert [a.id for a in xer_ativs] == ["42", "45", "48", "51", "55"]
    assert [a.id for a in xml_ativs] == ["42", "45", "48", "51", "55"]
    # amostra.xer e amostra_project.xml mantêm a dependência SS+3d externa da frente 45
    assert any(d.pred_id == "99" and d.tipo == "SS" and d.lag_dias == 3.0 for d in xer_ativs[1].dependencias)
    assert any(d.pred_id == "41" and d.tipo == "SS" and d.lag_dias == 3.0 for d in xml_ativs[1].dependencias)


def test_csv_grupo_risco_roundtrip_and_12col_preservation(
    tmp_path: Path,
    demo_cronograma_csv: Path,
    demo_mc_csv: Path,
):
    import csv as _csv

    com_grupo = tmp_path / "com_grupo.csv"
    com_grupo.write_text(
        "ID,Nome,Tipo,Predecessoras,FolgaTotal,BaselineHH,HHRealizado,HHRestante,HistoricoDesvios,DurO,DurM,DurP,GrupoRisco\n"
        "A1,Obra Civil,Interna,,-2d,100,0,100,,8,10,16,Clima\n"
        "A2,Montagem,Interna,A1FS,-2d,100,0,100,,8,10,14,\n",
        encoding="utf-8",
    )
    ativs = carregar_csv(com_grupo)
    assert ativs[0].grupo_risco == "Clima"
    assert ativs[1].grupo_risco == ""

    out_com_grupo = tmp_path / "out_com_grupo.csv"
    escrever_csv(ativs, out_com_grupo)
    with open(out_com_grupo, newline="", encoding="utf-8") as fh:
        header = next(_csv.reader(fh))
    assert "GrupoRisco" in header
    assert carregar_csv(out_com_grupo)[0].grupo_risco == "Clima"

    # Demos sem GrupoRisco mantêm exatamente 12 colunas na exportação
    for demo in (demo_cronograma_csv, demo_mc_csv):
        sem = carregar_csv(demo)
        assert all(a.grupo_risco == "" for a in sem)
        out_sem = tmp_path / f"out_{demo.name}"
        escrever_csv(sem, out_sem)
        with open(out_sem, newline="", encoding="utf-8") as fh:
            header_sem = next(_csv.reader(fh))
        assert len(header_sem) == 12
        assert "GrupoRisco" not in header_sem
