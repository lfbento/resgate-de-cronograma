"""Persistência local de ciclos semanais em SQLite e derivação de `HistoricoDesvios`."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Sequence

from resgate_cronograma.cpm import aplicar_folgas_cpm
from resgate_cronograma.models import (
    AtividadeCronograma,
    InfoCiclo,
    parse_dependencias,
)
from resgate_cronograma.parsers import (
    carregar_csv,
    escrever_csv,
    parse_mspdi,
    parse_xer,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projetos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT UNIQUE NOT NULL,
    criado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ciclos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    projeto_id INTEGER NOT NULL REFERENCES projetos(id) ON DELETE CASCADE,
    numero INTEGER NOT NULL,
    rotulo TEXT NOT NULL,
    origem TEXT NOT NULL,
    data_medicao TEXT NOT NULL,
    prazo_dias REAL,
    criado_em TEXT NOT NULL,
    UNIQUE(projeto_id, numero)
);

CREATE TABLE IF NOT EXISTS medicoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ciclo_id INTEGER NOT NULL REFERENCES ciclos(id) ON DELETE CASCADE,
    ordem INTEGER NOT NULL,
    atividade_id TEXT NOT NULL,
    nome TEXT NOT NULL,
    tipo TEXT NOT NULL,
    predecessoras TEXT NOT NULL,
    folga_dias REAL NOT NULL,
    desvio_dias REAL NOT NULL,
    baseline_hh REAL NOT NULL,
    hh_realizado REAL NOT NULL,
    hh_restante REAL NOT NULL,
    dur_o REAL NOT NULL,
    dur_m REAL NOT NULL,
    dur_p REAL NOT NULL,
    dur_m_explicita INTEGER NOT NULL,
    concluida INTEGER NOT NULL,
    marco INTEGER NOT NULL,
    grupo_risco TEXT NOT NULL,
    UNIQUE(ciclo_id, atividade_id)
);
"""


def _normalizar_zero(valor: float) -> float:
    return 0.0 if abs(float(valor)) < 1e-12 else float(valor)


def _agora_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _validar_nome_projeto(projeto: str) -> str:
    nome = (projeto or "").strip()
    if not nome:
        raise ValueError("Nome do projeto não pode ser vazio.")
    return nome


def _conectar(db_path: str | Path, *, criar: bool = False) -> sqlite3.Connection:
    caminho = Path(db_path)
    if criar:
        caminho.parent.mkdir(parents=True, exist_ok=True)
    elif not caminho.exists():
        raise FileNotFoundError(f"Banco SQLite não encontrado: {caminho}")
    conn = sqlite3.connect(str(caminho))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if criar:
        conn.executescript(_SCHEMA_SQL)
    return conn


def _carregar_arquivo_cronograma(arquivo: str | Path) -> list[AtividadeCronograma]:
    caminho = Path(arquivo)
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo de cronograma não encontrado: {caminho}")
    ext = caminho.suffix.lower()
    if ext == ".xer":
        return parse_xer(caminho)
    if ext == ".xml":
        return parse_mspdi(caminho)
    if ext == ".csv":
        return carregar_csv(caminho)
    raise ValueError(
        f"Formato de arquivo não suportado '{caminho.suffix}' (use .xer, .xml ou .csv)."
    )


def _serializar_predecessoras(a: AtividadeCronograma) -> str:
    if a.dependencias:
        return ";".join(dep.to_token() for dep in a.dependencias)
    return (a.predecessoras or "").strip()


def _linha_para_info_ciclo(
    row: sqlite3.Row,
    projeto_nome: str,
    total_atividades: int,
) -> InfoCiclo:
    return InfoCiclo(
        ciclo_id=int(row["id"]),
        projeto=projeto_nome,
        numero=int(row["numero"]),
        rotulo=str(row["rotulo"]),
        origem=str(row["origem"]),
        data_medicao=str(row["data_medicao"]),
        prazo_dias=(float(row["prazo_dias"]) if row["prazo_dias"] is not None else None),
        criado_em=str(row["criado_em"]),
        total_atividades=int(total_atividades),
    )


def registrar_ciclo(
    db_path: str | Path,
    projeto: str,
    arquivo: str | Path,
    *,
    numero: int | None = None,
    rotulo: str | None = None,
    data_medicao: str | None = None,
    prazo: float | None = None,
) -> InfoCiclo:
    """Registra snapshot `.xer`/`.xml`/`.csv` de um projeto no SQLite local."""
    projeto_nome = _validar_nome_projeto(projeto)
    if numero is not None and numero < 1:
        raise ValueError(f"Número do ciclo deve ser >= 1 (recebido: {numero}).")
    if prazo is not None and float(prazo) <= 0.0:
        raise ValueError(f"Prazo do ciclo deve ser > 0 (recebido: {prazo}).")

    caminho_arq = Path(arquivo)
    atividades = _carregar_arquivo_cronograma(caminho_arq)
    seen_ids: set[str] = set()
    for a in atividades:
        if a.id in seen_ids:
            raise ValueError(f"Atividade duplicada no cronograma: {a.id}")
        seen_ids.add(a.id)

    if prazo is not None:
        aplicar_folgas_cpm(atividades, prazo=float(prazo))

    agora = _agora_utc_iso()
    data_str = (data_medicao or agora[:10]).strip() or agora[:10]

    with closing(_conectar(db_path, criar=True)) as conn:
        with conn:
            row_proj = conn.execute(
                "SELECT id FROM projetos WHERE nome = ?",
                (projeto_nome,),
            ).fetchone()
            if row_proj is None:
                cur = conn.execute(
                    "INSERT INTO projetos (nome, criado_em) VALUES (?, ?)",
                    (projeto_nome, agora),
                )
                projeto_id = int(cur.lastrowid)
            else:
                projeto_id = int(row_proj["id"])

            if numero is None:
                row_max = conn.execute(
                    "SELECT COALESCE(MAX(numero), 0) AS max_num FROM ciclos WHERE projeto_id = ?",
                    (projeto_id,),
                ).fetchone()
                numero_final = int(row_max["max_num"]) + 1
            else:
                numero_final = int(numero)
                dup = conn.execute(
                    "SELECT id FROM ciclos WHERE projeto_id = ? AND numero = ?",
                    (projeto_id, numero_final),
                ).fetchone()
                if dup is not None:
                    raise ValueError(
                        f"Ciclo {numero_final} já registrado para o projeto '{projeto_nome}'."
                    )

            rotulo_final = (rotulo or "").strip() or f"Ciclo {numero_final}"
            cur_ciclo = conn.execute(
                """
                INSERT INTO ciclos (
                    projeto_id, numero, rotulo, origem, data_medicao, prazo_dias, criado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    projeto_id,
                    numero_final,
                    rotulo_final,
                    str(caminho_arq.name),
                    data_str,
                    float(prazo) if prazo is not None else None,
                    agora,
                ),
            )
            ciclo_id = int(cur_ciclo.lastrowid)

            registros = []
            for ordem, a in enumerate(atividades):
                folga_norm = _normalizar_zero(round(float(a.folga), 4))
                desvio_norm = _normalizar_zero(round(-folga_norm, 4))
                registros.append(
                    (
                        ciclo_id,
                        ordem,
                        a.id,
                        a.nome,
                        a.tipo,
                        _serializar_predecessoras(a),
                        folga_norm,
                        desvio_norm,
                        float(a.baseline_hh),
                        float(a.hh_realizado),
                        float(a.hh_restante),
                        float(a.dur_o),
                        float(a.dur_m),
                        float(a.dur_p),
                        1 if a.dur_m_explicita else 0,
                        1 if a.concluida else 0,
                        1 if a.marco else 0,
                        (a.grupo_risco or "").strip(),
                    )
                )

            conn.executemany(
                """
                INSERT INTO medicoes (
                    ciclo_id, ordem, atividade_id, nome, tipo, predecessoras,
                    folga_dias, desvio_dias, baseline_hh, hh_realizado, hh_restante,
                    dur_o, dur_m, dur_p, dur_m_explicita, concluida, marco, grupo_risco
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                registros,
            )

        return InfoCiclo(
            ciclo_id=ciclo_id,
            projeto=projeto_nome,
            numero=numero_final,
            rotulo=rotulo_final,
            origem=str(caminho_arq.name),
            data_medicao=data_str,
            prazo_dias=(float(prazo) if prazo is not None else None),
            criado_em=agora,
            total_atividades=len(atividades),
        )


def listar_projetos(db_path: str | Path) -> list[dict[str, object]]:
    """Lista projetos cadastrados e total de ciclos armazenados no banco SQLite."""
    with closing(_conectar(db_path, criar=False)) as conn:
        rows = conn.execute(
            """
            SELECT p.nome, p.criado_em, COUNT(c.id) AS total_ciclos,
                   MAX(c.numero) AS ultimo_ciclo
            FROM projetos p
            LEFT JOIN ciclos c ON c.projeto_id = p.id
            GROUP BY p.id
            ORDER BY p.nome ASC
            """
        ).fetchall()
        return [
            {
                "projeto": str(r["nome"]),
                "criado_em": str(r["criado_em"]),
                "total_ciclos": int(r["total_ciclos"] or 0),
                "ultimo_ciclo": (int(r["ultimo_ciclo"]) if r["ultimo_ciclo"] is not None else None),
            }
            for r in rows
        ]


def listar_ciclos(db_path: str | Path, projeto: str) -> list[InfoCiclo]:
    """Lista os ciclos registrados em ordem cronológica para um projeto."""
    projeto_nome = _validar_nome_projeto(projeto)
    with closing(_conectar(db_path, criar=False)) as conn:
        row_proj = conn.execute(
            "SELECT id FROM projetos WHERE nome = ?",
            (projeto_nome,),
        ).fetchone()
        if row_proj is None:
            raise ValueError(f"Projeto '{projeto_nome}' não encontrado em {db_path}.")

        rows = conn.execute(
            """
            SELECT c.*, COUNT(m.id) AS total_atividades
            FROM ciclos c
            LEFT JOIN medicoes m ON m.ciclo_id = c.id
            WHERE c.projeto_id = ?
            GROUP BY c.id
            ORDER BY c.numero ASC
            """,
            (int(row_proj["id"]),),
        ).fetchall()
        return [
            _linha_para_info_ciclo(r, projeto_nome, int(r["total_atividades"] or 0))
            for r in rows
        ]


def carregar_ciclo(
    db_path: str | Path,
    projeto: str,
    ciclo: int | None = None,
) -> tuple[list[AtividadeCronograma], InfoCiclo]:
    """Carrega um snapshot do SQLite e deriva `HistoricoDesvios` (`-folga_dias`) até `ciclo`."""
    projeto_nome = _validar_nome_projeto(projeto)
    if ciclo is not None and ciclo < 1:
        raise ValueError(f"--ciclo deve ser >= 1 (recebido: {ciclo}).")

    with closing(_conectar(db_path, criar=False)) as conn:
        row_proj = conn.execute(
            "SELECT id FROM projetos WHERE nome = ?",
            (projeto_nome,),
        ).fetchone()
        if row_proj is None:
            raise ValueError(f"Projeto '{projeto_nome}' não encontrado em {db_path}.")
        projeto_id = int(row_proj["id"])

        if ciclo is None:
            row_ciclo = conn.execute(
                """
                SELECT * FROM ciclos
                WHERE projeto_id = ?
                ORDER BY numero DESC
                LIMIT 1
                """,
                (projeto_id,),
            ).fetchone()
            if row_ciclo is None:
                raise ValueError(
                    f"Projeto '{projeto_nome}' não possui ciclos registrados em {db_path}."
                )
        else:
            row_ciclo = conn.execute(
                """
                SELECT * FROM ciclos
                WHERE projeto_id = ? AND numero = ?
                """,
                (projeto_id, int(ciclo)),
            ).fetchone()
            if row_ciclo is None:
                raise ValueError(
                    f"Ciclo {ciclo} não encontrado para o projeto '{projeto_nome}'."
                )

        alvo_numero = int(row_ciclo["numero"])
        ciclo_id = int(row_ciclo["id"])

        rows_med = conn.execute(
            """
            SELECT * FROM medicoes
            WHERE ciclo_id = ?
            ORDER BY ordem ASC
            """,
            (ciclo_id,),
        ).fetchall()

        rows_hist = conn.execute(
            """
            SELECT m.atividade_id, m.desvio_dias
            FROM medicoes m
            JOIN ciclos c ON c.id = m.ciclo_id
            WHERE c.projeto_id = ? AND c.numero <= ?
            ORDER BY c.numero ASC
            """,
            (projeto_id, alvo_numero),
        ).fetchall()

        hist_por_id: dict[str, list[float]] = {}
        for rh in rows_hist:
            tid = str(rh["atividade_id"])
            val = _normalizar_zero(float(rh["desvio_dias"]))
            hist_por_id.setdefault(tid, []).append(val)

        atividades: list[AtividadeCronograma] = []
        for rm in rows_med:
            tid = str(rm["atividade_id"])
            preds_str = str(rm["predecessoras"] or "")
            atividades.append(
                AtividadeCronograma(
                    id=tid,
                    nome=str(rm["nome"]),
                    tipo=str(rm["tipo"]),
                    predecessoras=preds_str,
                    dependencias=parse_dependencias(preds_str),
                    folga=_normalizar_zero(float(rm["folga_dias"])),
                    baseline_hh=float(rm["baseline_hh"]),
                    hh_realizado=float(rm["hh_realizado"]),
                    hh_restante=float(rm["hh_restante"]),
                    historico=hist_por_id.get(tid, []),
                    dur_o=float(rm["dur_o"]),
                    dur_m=float(rm["dur_m"]),
                    dur_p=float(rm["dur_p"]),
                    dur_m_explicita=bool(rm["dur_m_explicita"]),
                    folga_importada=True,
                    concluida=bool(rm["concluida"]),
                    marco=bool(rm["marco"]),
                    grupo_risco=str(rm["grupo_risco"] or ""),
                )
            )

        info = _linha_para_info_ciclo(row_ciclo, projeto_nome, len(atividades))
        return atividades, info


def exportar_ciclo_csv(
    db_path: str | Path,
    projeto: str,
    saida_csv: str | Path,
    *,
    ciclo: int | None = None,
) -> tuple[list[AtividadeCronograma], InfoCiclo]:
    """Exporta um ciclo do SQLite para CSV unificado com `HistoricoDesvios` derivado."""
    atividades, info = carregar_ciclo(db_path, projeto, ciclo=ciclo)
    destino = Path(saida_csv)
    destino.parent.mkdir(parents=True, exist_ok=True)
    escrever_csv(atividades, destino)
    return atividades, info


def resolver_atividades_cli(
    csv_path: str | None,
    db_path: str | None,
    projeto: str | None,
    ciclo: int | None = None,
) -> tuple[list[AtividadeCronograma], dict[str, object]]:
    """Resolve entrada de atividades via CSV posicional OU `--db` + `--projeto`."""
    if ciclo is not None and ciclo < 1:
        raise ValueError(f"--ciclo deve ser >= 1 (recebido: {ciclo}).")

    tem_csv = bool(csv_path and str(csv_path).strip())
    tem_db = bool(db_path and str(db_path).strip())
    tem_proj = bool(projeto and str(projeto).strip())

    if tem_db != tem_proj:
        raise ValueError("Os parâmetros --db e --projeto devem ser informados em conjunto.")
    if tem_csv and tem_db:
        raise ValueError(
            "Fonte ambígua: informe o arquivo CSV posicional OU --db com --projeto, não ambos."
        )
    if not tem_csv and not tem_db:
        raise ValueError(
            "Informe um arquivo CSV posicional ou --db <sqlite> --projeto <nome>."
        )

    if tem_csv:
        assert csv_path is not None
        atividades = carregar_csv(csv_path)
        return atividades, {
            "fonte": "csv",
            "arquivo": str(csv_path),
            "ciclo": ciclo,
        }

    assert db_path is not None and projeto is not None
    atividades, info = carregar_ciclo(db_path, projeto, ciclo=ciclo)
    return atividades, {
        "fonte": "sqlite",
        "db": str(db_path),
        "projeto": info.projeto,
        "ciclo": info.numero,
        "rotulo": info.rotulo,
        "data_medicao": info.data_medicao,
    }


__all__ = [
    "carregar_ciclo",
    "exportar_ciclo_csv",
    "listar_ciclos",
    "listar_projetos",
    "registrar_ciclo",
    "resolver_atividades_cli",
]
