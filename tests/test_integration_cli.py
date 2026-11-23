"""Testes de integração ponta a ponta e smoke tests de CLI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_cli(repo_dir: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(repo_dir / script), *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_xer_and_xml_roundtrip_into_cpm_and_mc(
    repo_dir: Path,
    amostra_xer: Path,
    amostra_xml: Path,
    tmp_path: Path,
):
    xer_csv = tmp_path / "xer_out.csv"
    res_xer = _run_cli(repo_dir, "ler_cronograma.py", str(amostra_xer), "-o", str(xer_csv))
    assert res_xer.returncode == 0, res_xer.stderr
    assert "5 atividades" in res_xer.stdout
    assert xer_csv.exists()

    xml_csv = tmp_path / "xml_out.csv"
    res_xml = _run_cli(repo_dir, "ler_cronograma.py", str(amostra_xml), "-o", str(xml_csv))
    assert res_xml.returncode == 0, res_xml.stderr
    assert "5 atividades" in res_xml.stdout
    assert xml_csv.exists()

    # Ambos alimentam cpm_pid e monte_carlo sem erros
    for csv_file in (xer_csv, xml_csv):
        r_cpm = _run_cli(repo_dir, "cpm_pid.py", str(csv_file))
        assert r_cpm.returncode == 0, r_cpm.stderr
        r_mc = _run_cli(
            repo_dir,
            "monte_carlo.py",
            str(csv_file),
            "--prazo",
            "45",
            "--sims",
            "300",
            "--seed",
            "42",
        )
        assert r_mc.returncode == 0, r_mc.stderr
        assert "Término P80:" in r_mc.stdout


def test_cpm_pid_demo_modelo1_ciclo_and_prazo(repo_dir: Path, demo_cronograma_csv: Path):
    res_full = _run_cli(repo_dir, "cpm_pid.py", str(demo_cronograma_csv))
    assert res_full.returncode == 0, res_full.stderr
    # Modelo 1 (9 colunas, taxa 8 HH/d): u_dias=7.2d -> 57.6 HH (não 7.2 HH)
    assert "57.6 HH" in res_full.stdout
    assert "predecessoras externas/ausentes" in res_full.stdout
    assert "40" in res_full.stdout and "41" in res_full.stdout

    # --ciclo 2 deve avaliar só 2 desvios (ex.: ID 42 com 4;6 -> 5.0d * 8 = 40.0 HH)
    res_c2 = _run_cli(repo_dir, "cpm_pid.py", str(demo_cronograma_csv), "--ciclo", "2")
    assert res_c2.returncode == 0, res_c2.stderr
    assert "40.0 HH" in res_c2.stdout
    assert res_c2.stdout != res_full.stdout

    # --ciclo 0 deve falhar
    res_bad = _run_cli(repo_dir, "cpm_pid.py", str(demo_cronograma_csv), "--ciclo", "0")
    assert res_bad.returncode != 0


def test_monte_carlo_demo_mc_succeeds_with_warnings(repo_dir: Path, demo_mc_csv: Path):
    res = _run_cli(
        repo_dir,
        "monte_carlo.py",
        str(demo_mc_csv),
        "--prazo",
        "45",
        "--sims",
        "1000",
        "--seed",
        "42",
    )
    assert res.returncode == 0, res.stderr
    assert "KeyError" not in res.stderr
    assert "Término P50:" in res.stdout
    assert "Término P80:" in res.stdout
    assert "Término P90:" in res.stdout
    assert "predecessoras externas/ausentes" in res.stdout
    assert "40" in res.stdout and "41" in res.stdout
    assert "PRESCRIÇÃO INTEGRADA" in res.stdout


def test_cli_rejects_unsupported_extension_and_cycles(repo_dir: Path, tmp_path: Path):
    res_ext = _run_cli(
        repo_dir,
        "ler_cronograma.py",
        str(tmp_path / "cronograma.mpp"),
        "-o",
        str(tmp_path / "out.csv"),
    )
    assert res_ext.returncode != 0

    ciclo_csv = tmp_path / "ciclo.csv"
    ciclo_csv.write_text(
        "ID,Nome,Tipo,Predecessoras,FolgaTotal,BaselineHH,HHRealizado,HHRestante,HistoricoDesvios,DurO,DurM,DurP\n"
        "C1,Frente 1,Interna,C2FS,0,80,0,80,2,8,10,15\n"
        "C2,Frente 2,Interna,C1FS,0,80,0,80,2,8,10,15\n",
        encoding="utf-8",
    )
    res_mc_ciclo = _run_cli(
        repo_dir,
        "monte_carlo.py",
        str(ciclo_csv),
        "--prazo",
        "20",
        "--sims",
        "50",
    )
    assert res_mc_ciclo.returncode != 0
    assert "C1" in res_mc_ciclo.stderr and "C2" in res_mc_ciclo.stderr

    res_cpm_ciclo = _run_cli(repo_dir, "cpm_pid.py", str(ciclo_csv), "--prazo", "20")
    assert res_cpm_ciclo.returncode != 0
    assert "C1" in res_cpm_ciclo.stderr and "C2" in res_cpm_ciclo.stderr


def test_legacy_module_imports(repo_dir: Path):
    import cpm_pid
    import ler_cronograma
    import monte_carlo

    f = cpm_pid.Frente(
        id="99",
        nome="Teste",
        tipo="Interna",
        predecessoras="",
        folga=-2.0,
        baseline_hh=160.0,
        hh_realizado=0.0,
        hh_restante=160.0,
        historico=[2.0],
    )
    uk, det = cpm_pid.pid_por_frente(f)
    assert uk > 0
    assert "uk_hh" in det
    assert monte_carlo.parse_preds("ENG-1FF-1.5d;41SS+3d") == [
        ("ENG-1", "FF", -1.5),
        ("41", "SS", 3.0),
    ]
    assert hasattr(ler_cronograma, "parse_xer") and hasattr(ler_cronograma, "parse_mspdi")


def test_monte_carlo_cli_correlation_tornado_closed_loop_and_opt_out(
    repo_dir: Path,
    demo_mc_csv: Path,
):
    res_closed = _run_cli(
        repo_dir,
        "monte_carlo.py",
        str(demo_mc_csv),
        "--prazo",
        "45",
        "--sims",
        "500",
        "--seed",
        "42",
        "--correlacao",
        "0.5",
    )
    assert res_closed.returncode == 0, res_closed.stderr
    assert "corr: 0.50" in res_closed.stdout
    assert "SENSIBILIDADE" in res_closed.stdout
    assert "SIMULAÇÃO PÓS-RESGATE" in res_closed.stdout
    assert "Antes" in res_closed.stdout and "Depois" in res_closed.stdout

    res_fatores = _run_cli(
        repo_dir,
        "monte_carlo.py",
        str(demo_mc_csv),
        "--prazo",
        "45",
        "--sims",
        "300",
        "--fatores-risco",
        '{"Interna": 0.6}',
    )
    assert res_fatores.returncode == 0, res_fatores.stderr
    assert "SENSIBILIDADE" in res_fatores.stdout

    res_sem = _run_cli(
        repo_dir,
        "monte_carlo.py",
        str(demo_mc_csv),
        "--prazo",
        "45",
        "--sims",
        "500",
        "--sem-resgate",
    )
    assert res_sem.returncode == 0, res_sem.stderr
    assert "SENSIBILIDADE" in res_sem.stdout
    assert "SIMULAÇÃO PÓS-RESGATE" not in res_sem.stdout

    # Entradas inválidas devem falhar com código != 0
    assert (
        _run_cli(
            repo_dir,
            "monte_carlo.py",
            str(demo_mc_csv),
            "--prazo",
            "45",
            "--sims",
            "100",
            "--correlacao",
            "1.0",
        ).returncode
        != 0
    )
    assert (
        _run_cli(
            repo_dir,
            "monte_carlo.py",
            str(demo_mc_csv),
            "--prazo",
            "45",
            "--sims",
            "100",
            "--eficiencia-crashing",
            "0",
        ).returncode
        != 0
    )
    assert (
        _run_cli(
            repo_dir,
            "monte_carlo.py",
            str(demo_mc_csv),
            "--prazo",
            "45",
            "--sims",
            "100",
            "--fatores-risco",
            "{bad",
        ).returncode
        != 0
    )


def test_wave3_end_to_end_sqlite_cost_json_and_offline_html(
    repo_dir: Path,
    amostra_xer: Path,
    amostra_xml: Path,
    tmp_path: Path,
):
    db = tmp_path / "obra.db"

    r1 = _run_cli(
        repo_dir,
        "ciclos.py",
        "registrar",
        str(amostra_xer),
        "--db",
        str(db),
        "--projeto",
        "Obra-W3",
        "--prazo",
        "45",
        "--rotulo",
        "Semana 1",
    )
    r2 = _run_cli(
        repo_dir,
        "ciclos.py",
        "registrar",
        str(amostra_xml),
        "--db",
        str(db),
        "--projeto",
        "Obra-W3",
        "--prazo",
        "45",
        "--rotulo",
        "Semana 2",
    )
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr

    r_list = _run_cli(repo_dir, "ciclos.py", "listar", "--db", str(db), "--projeto", "Obra-W3")
    assert r_list.returncode == 0 and "Ciclo   2" in r_list.stdout

    exp_csv = tmp_path / "exp" / "ciclo2.csv"
    r_exp = _run_cli(
        repo_dir,
        "ciclos.py",
        "exportar",
        "--db",
        str(db),
        "--projeto",
        "Obra-W3",
        "-o",
        str(exp_csv),
        "--ciclo",
        "2",
    )
    assert r_exp.returncode == 0, r_exp.stderr
    assert exp_csv.exists()

    cpm_json = tmp_path / "relatorios" / "cpm.json"
    cpm_html = tmp_path / "relatorios" / "cpm.html"
    r_cpm = _run_cli(
        repo_dir,
        "cpm_pid.py",
        "--db",
        str(db),
        "--projeto",
        "Obra-W3",
        "--ciclo",
        "2",
        "--prazo",
        "45",
        "--custos",
        '{"custo_hh_hora_extra": 140}',
        "--json",
        str(cpm_json),
        "--html",
        str(cpm_html),
    )
    assert r_cpm.returncode == 0, r_cpm.stderr
    assert "RANKING CUSTO × PRAZO" in r_cpm.stdout
    assert "R$" in r_cpm.stdout
    cpm_data = json.loads(cpm_json.read_text(encoding="utf-8"))
    assert cpm_data["modo"] == "cpm_pid"
    assert cpm_data["origem"]["projeto"] == "Obra-W3"
    assert cpm_data["plano_otimizado_custo"]["parametros"]["custo_hh_hora_extra"] == 140.0
    cpm_html_txt = cpm_html.read_text(encoding="utf-8")
    assert cpm_html_txt.startswith("<!DOCTYPE html>") and "<svg " in cpm_html_txt
    assert "http://" not in cpm_html_txt.lower() and "https://" not in cpm_html_txt.lower()

    mc_json = tmp_path / "relatorios" / "mc.json"
    mc_html = tmp_path / "relatorios" / "mc.html"
    r_mc = _run_cli(
        repo_dir,
        "monte_carlo.py",
        "--db",
        str(db),
        "--projeto",
        "Obra-W3",
        "--ciclo",
        "2",
        "--prazo",
        "45",
        "--sims",
        "300",
        "--seed",
        "42",
        "--custos",
        '{"custo_hh_hora_extra": 140}',
        "--json",
        str(mc_json),
        "--html",
        str(mc_html),
    )
    assert r_mc.returncode == 0, r_mc.stderr
    assert "RANKING CUSTO × PRAZO" in r_mc.stdout
    assert "Orçamento malha fechada (crashing):" in r_mc.stdout
    mc_data = json.loads(mc_json.read_text(encoding="utf-8"))
    assert mc_data["modo"] == "monte_carlo"
    assert len(mc_data["histograma_antes"]) == 12
    assert len(mc_data["histograma_depois"]) == 12
    assert mc_data["resgate"]["custo_total_malha_fechada_brl"] > 0
    mc_html_txt = mc_html.read_text(encoding="utf-8")
    assert mc_html_txt.startswith("<!DOCTYPE html>") and mc_html_txt.count("<svg ") == 3
    assert "http://" not in mc_html_txt.lower() and "https://" not in mc_html_txt.lower()


def test_wave3_cli_rejects_missing_ambiguous_sources_and_bad_custos(
    repo_dir: Path,
    demo_mc_csv: Path,
    tmp_path: Path,
):
    db = tmp_path / "dummy.db"

    # cpm_pid.py fontes ausentes/conflitantes/custos inválidos
    assert _run_cli(repo_dir, "cpm_pid.py").returncode != 0
    assert _run_cli(repo_dir, "cpm_pid.py", "--db", str(db)).returncode != 0
    assert _run_cli(repo_dir, "cpm_pid.py", "--projeto", "X").returncode != 0
    assert (
        _run_cli(
            repo_dir, "cpm_pid.py", str(demo_mc_csv), "--db", str(db), "--projeto", "X"
        ).returncode
        != 0
    )
    assert (
        _run_cli(repo_dir, "cpm_pid.py", str(demo_mc_csv), "--custos", "{bad").returncode
        != 0
    )

    # monte_carlo.py fontes ausentes/conflitantes/custos inválidos
    assert _run_cli(repo_dir, "monte_carlo.py", "--prazo", "45").returncode != 0
    assert _run_cli(repo_dir, "monte_carlo.py", "--db", str(db), "--prazo", "45").returncode != 0
    assert _run_cli(repo_dir, "monte_carlo.py", "--projeto", "X", "--prazo", "45").returncode != 0
    assert (
        _run_cli(
            repo_dir,
            "monte_carlo.py",
            str(demo_mc_csv),
            "--db",
            str(db),
            "--projeto",
            "X",
            "--prazo",
            "45",
        ).returncode
        != 0
    )
    assert (
        _run_cli(
            repo_dir,
            "monte_carlo.py",
            str(demo_mc_csv),
            "--prazo",
            "45",
            "--sims",
            "50",
            "--custos",
            '{"custo_hh_base": -1}',
        ).returncode
        != 0
    )

