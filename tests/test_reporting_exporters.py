"""Testes dos exportadores JSON e HTML5 executivo offline com SVG inline e escape XSS."""
from __future__ import annotations

import json
from pathlib import Path

from resgate_cronograma.exporters import (
    SCHEMA_VERSION,
    construir_payload_cpm_pid,
    construir_payload_monte_carlo,
    exportar_html,
    exportar_json,
)
from resgate_cronograma.models import AtividadeCronograma, Dependencia
from resgate_cronograma.monte_carlo import simular_cronograma


def test_monte_carlo_json_and_offline_html_with_inline_svgs(tmp_path: Path):
    atividades = [
        AtividadeCronograma(
            id="I1",
            nome="Estrutura Metalica",
            tipo="Interna",
            folga=-4.0,
            baseline_hh=240.0,
            hh_restante=240.0,
            dur_o=15.0,
            dur_m=20.0,
            dur_p=30.0,
            dur_m_explicita=True,
        ),
        AtividadeCronograma(
            id="I2",
            nome="Montagem",
            tipo="Interna",
            predecessoras="I1FS",
            folga=-4.0,
            baseline_hh=120.0,
            hh_restante=120.0,
            dur_o=8.0,
            dur_m=10.0,
            dur_p=15.0,
            dur_m_explicita=True,
        ),
    ]
    res = simular_cronograma(
        atividades,
        prazo=24.0,
        n_sims=400,
        seed=42,
        fechar_malha=True,
    )
    payload = construir_payload_monte_carlo(
        atividades,
        res,
        origem={"fonte": "csv", "arquivo": "teste.csv"},
    )
    for chave in (
        "schema_version",
        "gerado_em",
        "modo",
        "parametros",
        "avisos",
        "resumo",
        "criticidade",
        "sensibilidade",
        "prescricoes",
        "histograma_antes",
        "histograma_depois",
        "resgate",
        "plano_otimizado_custo",
    ):
        assert chave in payload
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["modo"] == "monte_carlo"
    assert len(payload["histograma_antes"]) == 12
    assert len(payload["histograma_depois"]) == 12
    assert payload["resgate"] is not None

    json_path = tmp_path / "aninhado" / "mc" / "report.json"
    html_path = tmp_path / "aninhado" / "mc" / "exec.html"
    exportar_json(payload, json_path)
    exportar_html(payload, html_path)

    assert json.loads(json_path.read_text(encoding="utf-8"))["modo"] == "monte_carlo"
    html_txt = html_path.read_text(encoding="utf-8")
    assert html_txt.startswith("<!DOCTYPE html>")
    assert "<style>" in html_txt
    assert html_txt.count("<svg ") == 3
    assert "R$/dia" in html_txt
    assert "http://" not in html_txt.lower()
    assert "https://" not in html_txt.lower()


def test_cpm_pid_json_and_html_escaping_xss(tmp_path: Path):
    atividades = [
        AtividadeCronograma(
            id="XSS-1",
            nome='<script>alert(1)</script> & Valvula',
            tipo="Interna",
            dependencias=[Dependencia(pred_id="EXT<1>&2", tipo="FS")],
            folga=-5.0,
            baseline_hh=160.0,
            hh_restante=160.0,
            dur_o=12.0,
            dur_m=16.0,
            dur_p=24.0,
            dur_m_explicita=True,
            historico=[3.0, 5.0],
        ),
        AtividadeCronograma(
            id="SUC-2",
            nome="Montagem Final",
            tipo="Interna",
            predecessoras="XSS-1FS",
            folga=-5.0,
            baseline_hh=80.0,
            hh_restante=80.0,
            dur_o=8.0,
            dur_m=10.0,
            dur_p=14.0,
            dur_m_explicita=True,
            historico=[2.0],
        ),
    ]
    payload = construir_payload_cpm_pid(atividades, prazo=20.0)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["modo"] == "cpm_pid"
    for chave in ("avisos", "resumo", "frentes_criticas", "plano_otimizado_custo"):
        assert chave in payload
    assert "EXT<1>&2" in payload["avisos"]

    html_out = tmp_path / "sub" / "cpm.html"
    exportar_html(payload, html_out)
    html_txt = html_out.read_text(encoding="utf-8")
    assert html_txt.startswith("<!DOCTYPE html>")
    assert "<svg " in html_txt
    assert "<script>alert(1)</script>" not in html_txt
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_txt
    assert "EXT&lt;1&gt;&amp;2" in html_txt
    assert "http://" not in html_txt.lower()
    assert "https://" not in html_txt.lower()
