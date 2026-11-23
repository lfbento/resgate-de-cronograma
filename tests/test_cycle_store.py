"""Testes unitários do armazenamento SQLite de ciclos e derivação de HistoricoDesvios."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from resgate_cronograma.cycle_store import (
    carregar_ciclo,
    exportar_ciclo_csv,
    listar_ciclos,
    listar_projetos,
    registrar_ciclo,
    resolver_atividades_cli,
)
from resgate_cronograma.parsers import carregar_csv


def _escrever_csv(path: Path, linhas: list[str], *, com_grupo: bool = False) -> Path:
    cabecalho = (
        "ID,Nome,Tipo,Predecessoras,FolgaTotal,BaselineHH,HHRealizado,"
        "HHRestante,HistoricoDesvios,DurO,DurM,DurP"
    )
    if com_grupo:
        cabecalho += ",GrupoRisco"
    path.write_text("\n".join([cabecalho, *linhas]) + "\n", encoding="utf-8")
    return path


def _run_ciclos(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(repo_dir / "ciclos.py"), *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_registrar_ciclos_xer_xml_e_derivacao_modelo1(
    tmp_path: Path,
    amostra_xer: Path,
    amostra_xml: Path,
):
    db = tmp_path / "obras" / "historico.db"

    c1 = registrar_ciclo(db, "Obra-XER", amostra_xer, prazo=45.0, rotulo="Sem 1")
    c2 = registrar_ciclo(db, "Obra-XER", amostra_xml, prazo=45.0, rotulo="Sem 2")
    assert c1.numero == 1
    assert c2.numero == 2
    assert c1.total_atividades == 5
    assert c2.total_atividades == 5

    projetos = listar_projetos(db)
    assert len(projetos) == 1
    assert projetos[0]["projeto"] == "Obra-XER"
    assert projetos[0]["total_ciclos"] == 2
    assert projetos[0]["ultimo_ciclo"] == 2

    ciclos = listar_ciclos(db, "Obra-XER")
    assert [c.numero for c in ciclos] == [1, 2]
    assert [c.rotulo for c in ciclos] == ["Sem 1", "Sem 2"]


def test_multi_cycle_float_progression_and_mid_project_activity(tmp_path: Path):
    db = tmp_path / "prog.db"

    c1_csv = _escrever_csv(
        tmp_path / "c1.csv",
        ["A1,Frente A,Interna,,-4d,200,40,160,,16,20,28,Clima"],
        com_grupo=True,
    )
    c2_csv = _escrever_csv(
        tmp_path / "c2.csv",
        [
            "A1,Frente A,Interna,,-6d,200,80,120,,16,20,28,Clima",
            "A2,Frente Nova,Interna,A1FS,2d,100,0,100,,8,10,14,",
        ],
        com_grupo=True,
    )
    c3_csv = _escrever_csv(
        tmp_path / "c3.csv",
        [
            "A1,Frente A,Interna,,-8d,200,120,80,,16,20,28,Clima",
            "A2,Frente Nova,Interna,A1FS,-3d,100,20,80,,8,10,14,",
            "M0,Zero,Interna,,0d,40,40,0,,0,0,0,",
        ],
        com_grupo=True,
    )

    registrar_ciclo(db, "Obra-1", c1_csv)
    registrar_ciclo(db, "Obra-1", c2_csv)
    registrar_ciclo(db, "Obra-1", c3_csv)

    ats_c3, info_c3 = carregar_ciclo(db, "Obra-1")
    assert info_c3.numero == 3
    por_id = {a.id: a for a in ats_c3}
    assert por_id["A1"].historico == [4.0, 6.0, 8.0]
    assert por_id["A1"].grupo_risco == "Clima"
    assert por_id["A2"].historico == [-2.0, 3.0]
    # Normaliza -0.0 para +0.0
    assert repr(por_id["M0"].historico[0]) == "0.0"

    ats_c2, info_c2 = carregar_ciclo(db, "Obra-1", ciclo=2)
    assert info_c2.numero == 2
    por_id_c2 = {a.id: a for a in ats_c2}
    assert por_id_c2["A1"].historico == [4.0, 6.0]
    assert por_id_c2["A2"].historico == [-2.0]

    csv_exportado = tmp_path / "out" / "ciclo3.csv"
    exportar_ciclo_csv(db, "Obra-1", csv_exportado)
    recarregado = {a.id: a for a in carregar_csv(csv_exportado)}
    assert recarregado["A1"].historico == [4.0, 6.0, 8.0]
    assert recarregado["A1"].grupo_risco == "Clima"
    assert recarregado["A2"].historico == [-2.0, 3.0]


def test_validacoes_erros_cycle_store(tmp_path: Path):
    db = tmp_path / "err.db"
    csv_ok = _escrever_csv(
        tmp_path / "ok.csv",
        ["1,Tarefa,Interna,,-2d,80,0,80,,8,10,14"],
    )

    with pytest.raises(ValueError, match="vazio"):
        registrar_ciclo(db, "   ", csv_ok)

    bad_ext = tmp_path / "obra.mpp"
    bad_ext.write_text("dummy", encoding="utf-8")
    with pytest.raises(ValueError, match="não suportado"):
        registrar_ciclo(db, "Obra", bad_ext)

    registrar_ciclo(db, "Obra", csv_ok, numero=2)
    with pytest.raises(ValueError, match="já registrado"):
        registrar_ciclo(db, "Obra", csv_ok, numero=2)

    with pytest.raises(ValueError, match="não encontrado"):
        carregar_ciclo(db, "Inexistente")
    with pytest.raises(ValueError, match="não encontrado"):
        carregar_ciclo(db, "Obra", ciclo=99)
    with pytest.raises(FileNotFoundError):
        listar_projetos(tmp_path / "nao_existe.db")

    # Validação de fontes exclusivas CLI
    with pytest.raises(ValueError):
        resolver_atividades_cli(None, None, None)
    with pytest.raises(ValueError):
        resolver_atividades_cli(str(csv_ok), str(db), "Obra")
    with pytest.raises(ValueError):
        resolver_atividades_cli(None, str(db), None)


def test_cli_ciclos_registrar_listar_exportar(repo_dir: Path, tmp_path: Path):
    db = tmp_path / "cli.db"
    s1 = _escrever_csv(tmp_path / "s1.csv", ["T1,Montagem,Interna,,-3d,80,0,80,,8,10,12"])
    s2 = _escrever_csv(tmp_path / "s2.csv", ["T1,Montagem,Interna,,-5d,80,0,80,,8,10,12"])

    r1 = _run_ciclos(repo_dir, "registrar", str(s1), "--db", str(db), "--projeto", "Linha-1")
    r2 = _run_ciclos(repo_dir, "registrar", str(s2), "--db", str(db), "--projeto", "Linha-1")
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    assert "Ciclo 1" in r1.stdout
    assert "Ciclo 2" in r2.stdout

    rl_proj = _run_ciclos(repo_dir, "listar", "--db", str(db))
    rl_cic = _run_ciclos(repo_dir, "listar", "--db", str(db), "--projeto", "Linha-1")
    assert rl_proj.returncode == 0 and "Linha-1" in rl_proj.stdout
    assert rl_cic.returncode == 0 and "Ciclo   2" in rl_cic.stdout

    out_csv = tmp_path / "exp" / "linha1_c2.csv"
    r_exp = _run_ciclos(
        repo_dir,
        "exportar",
        "--db",
        str(db),
        "--projeto",
        "Linha-1",
        "-o",
        str(out_csv),
        "--ciclo",
        "2",
    )
    assert r_exp.returncode == 0, r_exp.stderr
    assert out_csv.exists()
    ats_exp = {a.id: a for a in carregar_csv(out_csv)}
    assert ats_exp["T1"].historico == [3.0, 5.0]

    r_miss = _run_ciclos(repo_dir, "listar", "--db", str(db), "--projeto", "Desconhecido")
    assert r_miss.returncode != 0
    r_miss_c = _run_ciclos(
        repo_dir,
        "exportar",
        "--db",
        str(db),
        "--projeto",
        "Linha-1",
        "-o",
        str(tmp_path / "nao.csv"),
        "--ciclo",
        "9",
    )
    assert r_miss_c.returncode != 0
