"""Exportadores estruturados (JSON e HTML5 executivo offline com gráficos SVG inline)."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import html
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from resgate_cronograma.cost import avaliar_plano_custo_cpm, avaliar_plano_custo_mc
from resgate_cronograma.cpm import RedeCPM, aplicar_folgas_cpm
from resgate_cronograma.models import (
    AtividadeCronograma,
    ParametrosCusto,
    PlanoOtimizadoCusto,
    ResultadoMonteCarlo,
)
from resgate_cronograma.pid import FOLGA_ALERTA, calcular_pid, prescrever

SCHEMA_VERSION = "3.0"


def _agora_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _esc(valor: object) -> str:
    return html.escape(str(valor), quote=True)


def _truncar(texto: str, limite: int = 28) -> str:
    limpo = " ".join((texto or "").split())
    if len(limpo) <= limite:
        return limpo
    return limpo[: max(limite - 1, 1)] + "…"


def _fmt_brl(valor: float | None) -> str:
    if valor is None:
        return "N/A — externo"
    texto = f"{valor:,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_brl_dia(valor: float | None, *, fallback: str = "N/A — externo") -> str:
    if valor is None:
        return fallback
    return f"{_fmt_brl(valor)}/dia"


def _serializar_plano_custo(plano: PlanoOtimizadoCusto) -> dict[str, object]:
    opcoes_dict = [asdict(op) for op in plano.opcoes]
    alocacoes_dict = [asdict(al) for al in plano.alocacoes_valoradas]
    return {
        "parametros": asdict(plano.parametros),
        "custo_total_malha_fechada_brl": plano.custo_total_malha_fechada_brl,
        "alocacoes_valoradas": alocacoes_dict,
        "opcoes": opcoes_dict,
    }


def _melhor_custo_dia(plano: PlanoOtimizadoCusto) -> float | None:
    for op in plano.opcoes:
        if op.custo_por_dia_brl is not None:
            return op.custo_por_dia_brl
    return None


def construir_payload_cpm_pid(
    atividades: Sequence[AtividadeCronograma],
    *,
    ciclo: int | None = None,
    prazo: float | None = None,
    parametros_custo: ParametrosCusto | str | Path | Mapping[str, object] | None = None,
    eficiencia_crashing: float = 1.0,
    origem: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Monta payload JSON determinístico completo do diagnóstico CPM + PID + Custo."""
    rede = RedeCPM(atividades)
    res_cpm = aplicar_folgas_cpm(atividades, prazo=prazo, rede=rede)
    plano_custo = avaliar_plano_custo_cpm(
        atividades,
        rede=rede,
        ciclo=ciclo,
        prazo=None,
        parametros=parametros_custo,
        eficiencia_crashing=eficiencia_crashing,
    )

    criticas = sorted(
        (f for f in atividades if f.folga <= FOLGA_ALERTA),
        key=lambda x: (x.folga, x.id),
    )
    frentes_criticas: list[dict[str, object]] = []
    for f in criticas:
        pid_res = calcular_pid(f, ciclo=ciclo)
        acoes = prescrever(
            f,
            pid_res.uk_hh,
            pid_res,
            tem_fs=rede.tem_ligacao_fs(f.id),
        )
        frentes_criticas.append(
            {
                "id": f.id,
                "nome": f.nome,
                "tipo": f.tipo,
                "criticidade": f.criticidade,
                "folga_dias": round(f.folga, 4),
                "baseline_hh": round(f.baseline_hh, 2),
                "hh_realizado": round(f.hh_realizado, 2),
                "hh_restante": round(f.hh_restante, 2),
                "historico": list(f.historico[:ciclo] if ciclo is not None else f.historico),
                "pid": {
                    "u_dias": round(pid_res.u_dias, 4),
                    "uk_hh": round(pid_res.uk_hh, 2),
                    "taxa_hh_dia": round(pid_res.taxa_hh_dia, 4),
                    "teto_hh": round(pid_res.teto_hh, 2),
                    "saturado": pid_res.saturado,
                    "deadband": pid_res.deadband,
                    "inativo": pid_res.inativo,
                    "detalhes": pid_res.to_legacy_dict(),
                },
                "acoes": acoes,
            }
        )

    frentes_totais = [
        {
            "id": a.id,
            "nome": a.nome,
            "tipo": a.tipo,
            "criticidade": a.criticidade,
            "folga_dias": round(a.folga, 4),
            "dur_m": round(a.dur_m, 4),
            "baseline_hh": round(a.baseline_hh, 2),
            "hh_restante": round(a.hh_restante, 2),
        }
        for a in atividades
    ]

    folgas = [a.folga for a in atividades]
    total_criticas = sum(1 for a in atividades if a.folga <= 0.0)
    total_alerta = sum(1 for a in atividades if 0.0 < a.folga <= FOLGA_ALERTA)

    return {
        "schema_version": SCHEMA_VERSION,
        "gerado_em": _agora_utc_iso(),
        "modo": "cpm_pid",
        "parametros": {
            "ciclo": ciclo,
            "prazo": prazo,
            "eficiencia_crashing": eficiencia_crashing,
        },
        "origem": dict(origem) if origem is not None else None,
        "avisos": list(rede.missing_predecessors),
        "resumo": {
            "duracao_cpm_dias": round(res_cpm.duracao_projeto, 4),
            "prazo_contratual": prazo,
            "total_atividades": len(atividades),
            "total_criticas": total_criticas,
            "total_alerta": total_alerta,
            "folga_minima_dias": round(min(folgas), 4) if folgas else 0.0,
            "melhor_opcao_custo_por_dia_brl": _melhor_custo_dia(plano_custo),
        },
        "frentes_criticas": frentes_criticas,
        "frentes_totais": frentes_totais,
        "plano_otimizado_custo": _serializar_plano_custo(plano_custo),
    }


def construir_payload_monte_carlo(
    atividades: Sequence[AtividadeCronograma],
    resultado: ResultadoMonteCarlo,
    *,
    parametros_custo: ParametrosCusto | str | Path | Mapping[str, object] | None = None,
    eficiencia_crashing: float = 1.0,
    origem: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Monta payload JSON determinístico completo da simulação Monte Carlo + Resgate + Custo."""
    rede = RedeCPM(atividades)
    por_id = {a.id: a for a in atividades}
    plano_custo = avaliar_plano_custo_mc(
        atividades,
        resultado,
        rede=rede,
        parametros=parametros_custo,
        eficiencia_crashing=eficiencia_crashing,
    )

    bins_antes = (
        resultado.resgate.histograma_antes
        if (resultado.resgate is not None and resultado.resgate.histograma_antes)
        else resultado.histograma
    )
    bins_depois = resultado.resgate.histograma_depois if resultado.resgate is not None else []

    hist_antes = [asdict(b) for b in bins_antes]
    hist_depois = [asdict(b) for b in bins_depois]

    resgate_dict: dict[str, object] | None = None
    if resultado.resgate is not None:
        r = resultado.resgate
        prazo = resultado.prazo
        resgate_dict = {
            "p50_antes": round(r.p50_antes, 4),
            "p80_antes": round(r.p80_antes, 4),
            "p90_antes": round(r.p90_antes, 4),
            "p_alvo_antes": round(r.p_alvo_antes, 4),
            "p50_depois": round(r.p50_depois, 4),
            "p80_depois": round(r.p80_depois, 4),
            "p90_depois": round(r.p90_depois, 4),
            "p_alvo_depois": round(r.p_alvo_depois, 4),
            "termino_p80_antes": round(prazo + r.p80_antes, 4),
            "termino_p80_depois": round(prazo + r.p80_depois, 4),
            "gap_inicial": round(r.gap_inicial, 4),
            "gap_final": round(r.gap_final, 4),
            "dias_recuperados": round(r.dias_recuperados, 4),
            "iteracoes": r.iteracoes,
            "convergiu": r.convergiu,
            "gargalos_residuais": list(r.gargalos_residuais),
            "custo_total_malha_fechada_brl": plano_custo.custo_total_malha_fechada_brl,
            "alocacoes": [asdict(al) for al in r.alocacoes],
            "histograma_depois": hist_depois,
        }

    criticidade_lista = [
        {
            "id": tid,
            "nome": por_id[tid].nome if tid in por_id else tid,
            "tipo": por_id[tid].tipo if tid in por_id else "",
            "indice": round(val, 6),
        }
        for tid, val in sorted(resultado.criticidade.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    sensibilidade_lista = [
        {
            "id": tid,
            "nome": por_id[tid].nome if tid in por_id else tid,
            "tipo": por_id[tid].tipo if tid in por_id else "",
            "spearman": round(val, 6),
        }
        for tid, val in sorted(
            resultado.sensibilidade.items(), key=lambda kv: (-abs(kv[1]), kv[0])
        )
    ]

    prescricoes_lista = [
        {
            "id": p.atividade.id,
            "nome": p.atividade.nome,
            "tipo": p.atividade.tipo,
            "criticidade": round(p.criticidade, 6),
            "uk_hh_bruto": round(p.pid.uk_hh, 2),
            "uk_hh_ajustado": round(p.uk_hh_ajustado, 2),
            "saturado": p.pid.saturado,
            "deadband": p.pid.deadband,
            "inativo": p.pid.inativo,
            "acoes": list(p.acoes),
        }
        for p in resultado.prescricoes
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "gerado_em": _agora_utc_iso(),
        "modo": "monte_carlo",
        "parametros": {
            "prazo": resultado.prazo,
            "alvo": resultado.alvo,
            "n_sims": resultado.n_sims,
            "seed": resultado.seed,
            "correlacao": round(resultado.correlacao, 4),
            "eficiencia_crashing": eficiencia_crashing,
        },
        "origem": dict(origem) if origem is not None else None,
        "avisos": list(resultado.missing_predecessors),
        "resumo": {
            "prazo_contratual": resultado.prazo,
            "alvo_percentil": resultado.alvo,
            "termino_p50": round(resultado.prazo + resultado.p50, 4),
            "termino_p80": round(resultado.prazo + resultado.p80, 4),
            "termino_p90": round(resultado.prazo + resultado.p90, 4),
            "atraso_p_alvo": round(resultado.p_alvo, 4),
            "custo_total_malha_fechada_brl": plano_custo.custo_total_malha_fechada_brl,
            "melhor_opcao_custo_por_dia_brl": _melhor_custo_dia(plano_custo),
        },
        "criticidade": criticidade_lista,
        "sensibilidade": sensibilidade_lista,
        "prescricoes": prescricoes_lista,
        "histograma_antes": hist_antes,
        "histograma_depois": hist_depois,
        "resgate": resgate_dict,
        "plano_otimizado_custo": _serializar_plano_custo(plano_custo),
    }


def exportar_json(payload: Mapping[str, Any], caminho: str | Path) -> Path:
    """Grava payload estruturado em JSON UTF-8 criando diretórios pai automaticamente."""
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return destino


def _svg_histograma_mc(
    hist_antes: Sequence[Mapping[str, Any]],
    hist_depois: Sequence[Mapping[str, Any]],
    prazo: float,
    termino_p80_antes: float,
    termino_p80_depois: float | None,
) -> str:
    w, h = 680, 250
    ml, mr, mt, mb = 44, 20, 28, 42
    pw, ph = w - ml - mr, h - mt - mb
    if not hist_antes:
        return (
            f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Histograma">'
            '<text x="20" y="40" fill="#475569" font-size="13">Sem dados de histograma.</text>'
            "</svg>"
        )

    vmin = float(hist_antes[0]["inicio"])
    vmax = float(hist_antes[-1]["fim"])
    max_cnt = max(
        [int(b["contagem"]) for b in hist_antes]
        + [int(b["contagem"]) for b in hist_depois]
        + [1]
    )
    n_bins = len(hist_antes)
    slot = pw / n_bins

    elementos: list[str] = [
        f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="#f8fafc" stroke="#cbd5e1"/>'
    ]

    for i, bin_a in enumerate(hist_antes):
        cnt_a = int(bin_a["contagem"])
        bh_a = (cnt_a / max_cnt) * (ph - 8)
        bx = ml + i * slot + 2
        bw = max(slot - 4, 4.0)
        by_a = mt + ph - bh_a
        elementos.append(
            f'<rect x="{bx:.1f}" y="{by_a:.1f}" width="{bw:.1f}" height="{bh_a:.1f}" '
            f'fill="#ef4444" fill-opacity="0.42" stroke="#dc2626" stroke-width="0.8"/>'
        )
        if i < len(hist_depois):
            cnt_d = int(hist_depois[i]["contagem"])
            bh_d = (cnt_d / max_cnt) * (ph - 8)
            by_d = mt + ph - bh_d
            bw_d = max(bw * 0.65, 3.0)
            bx_d = bx + (bw - bw_d) / 2.0
            elementos.append(
                f'<rect x="{bx_d:.1f}" y="{by_d:.1f}" width="{bw_d:.1f}" height="{bh_d:.1f}" '
                f'fill="#059669" fill-opacity="0.65" stroke="#047857" stroke-width="0.8"/>'
            )

    # Linha vertical do prazo contratual se estiver próxima do intervalo plotado
    span = vmax - vmin
    if span > 1e-9:
        px = ml + ((prazo - vmin) / span) * pw
        px_clamped = min(max(px, ml), ml + pw)
        elementos.append(
            f'<line x1="{px_clamped:.1f}" y1="{mt}" x2="{px_clamped:.1f}" y2="{mt + ph}" '
            'stroke="#1d4ed8" stroke-width="2" stroke-dasharray="5,4"/>'
        )
        elementos.append(
            f'<text x="{min(px_clamped + 4, w - 95):.1f}" y="{mt + 14}" '
            f'fill="#1d4ed8" font-size="11" font-weight="600">Prazo {prazo:g}d</text>'
        )

    elementos.append(
        f'<text x="{ml}" y="{h - 14}" fill="#475569" font-size="11">{vmin:.1f}d</text>'
    )
    elementos.append(
        f'<text x="{ml + pw - 36}" y="{h - 14}" fill="#475569" font-size="11">{vmax:.1f}d</text>'
    )
    leg_depois = (
        f" | P80 Depois: {termino_p80_depois:.1f}d" if termino_p80_depois is not None else ""
    )
    elementos.append(
        f'<text x="{ml}" y="17" fill="#0f172a" font-size="11.5" font-weight="600">'
        f'Vermelho: Antes (P80 {termino_p80_antes:.1f}d){_esc(leg_depois)}'
        + (" | Verde: Pós-Resgate" if hist_depois else "")
        + "</text>"
    )

    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Histograma Antes e Depois">' + "".join(elementos) + "</svg>"


def _svg_barras_spearman(sensibilidade: Sequence[Mapping[str, Any]]) -> str:
    itens = list(sensibilidade)[:10]
    w = 680
    row_h = 24
    mt, mb, ml, mr = 24, 24, 195, 55
    h = max(mt + mb + len(itens) * row_h, 90)
    pw = w - ml - mr
    cx = ml + pw / 2.0

    elementos: list[str] = [
        f'<line x1="{cx:.1f}" y1="{mt}" x2="{cx:.1f}" y2="{h - mb}" stroke="#94a3b8" stroke-width="1"/>'
    ]
    for idx, item in enumerate(itens):
        y = mt + idx * row_h + 4
        val = float(item["spearman"])
        label = _esc(_truncar(f"{item['id']} — {item['nome']}", 26))
        bw = abs(val) * (pw / 2.0 - 4.0)
        bx = cx if val >= 0.0 else cx - bw
        cor = "#0284c7" if val >= 0.0 else "#64748b"
        elementos.append(
            f'<text x="{ml - 8}" y="{y + 13}" text-anchor="end" fill="#1e293b" font-size="11">{label}</text>'
        )
        elementos.append(
            f'<rect x="{bx:.1f}" y="{y + 2}" width="{max(bw, 1.5):.1f}" height="14" rx="2" fill="{cor}"/>'
        )
        elementos.append(
            f'<text x="{ml + pw + 6}" y="{y + 13}" fill="#334155" font-size="11">{val:+.2f}</text>'
        )

    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Tornado Spearman">' + "".join(elementos) + "</svg>"


def _svg_criticidade_mc(criticidade: Sequence[Mapping[str, Any]]) -> str:
    itens = list(criticidade)[:10]
    w = 680
    row_h = 24
    mt, mb, ml, mr = 24, 24, 195, 55
    h = max(mt + mb + len(itens) * row_h, 90)
    pw = w - ml - mr
    lim_x = ml + 0.40 * pw

    elementos: list[str] = [
        f'<line x1="{lim_x:.1f}" y1="{mt - 6}" x2="{lim_x:.1f}" y2="{h - mb}" '
        'stroke="#dc2626" stroke-width="1.5" stroke-dasharray="4,3"/>',
        f'<text x="{lim_x + 4:.1f}" y="{mt - 9}" fill="#dc2626" font-size="10.5">Limiar 40%</text>',
    ]
    for idx, item in enumerate(itens):
        y = mt + idx * row_h + 4
        val = max(0.0, min(1.0, float(item["indice"])))
        label = _esc(_truncar(f"{item['id']} — {item['nome']}", 26))
        bw = val * pw
        cor = "#dc2626" if val >= 0.40 else "#94a3b8"
        elementos.append(
            f'<text x="{ml - 8}" y="{y + 13}" text-anchor="end" fill="#1e293b" font-size="11">{label}</text>'
        )
        elementos.append(
            f'<rect x="{ml}" y="{y + 2}" width="{max(bw, 1.5):.1f}" height="14" rx="2" fill="{cor}"/>'
        )
        elementos.append(
            f'<text x="{ml + pw + 6}" y="{y + 13}" fill="#334155" font-size="11">{val:.0%}</text>'
        )

    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Criticidade Monte Carlo">' + "".join(elementos) + "</svg>"


def _svg_folgas_cpm(frentes: Sequence[Mapping[str, Any]]) -> str:
    itens = sorted(frentes, key=lambda x: (float(x["folga_dias"]), str(x["id"])))[:12]
    w = 680
    row_h = 24
    mt, mb, ml, mr = 24, 24, 195, 65
    h = max(mt + mb + len(itens) * row_h, 90)
    pw = w - ml - mr
    max_abs = max([abs(float(x["folga_dias"])) for x in itens] + [5.0])
    cx = ml + pw / 2.0

    elementos: list[str] = [
        f'<line x1="{cx:.1f}" y1="{mt}" x2="{cx:.1f}" y2="{h - mb}" stroke="#334155" stroke-width="1.5"/>'
    ]
    for idx, item in enumerate(itens):
        y = mt + idx * row_h + 4
        folga = float(item["folga_dias"])
        label = _esc(_truncar(f"{item['id']} — {item['nome']}", 26))
        bw = (abs(folga) / max_abs) * (pw / 2.0 - 4.0)
        bx = cx if folga >= 0.0 else cx - bw
        if folga <= 0.0:
            cor = "#dc2626"
        elif folga <= FOLGA_ALERTA:
            cor = "#d97706"
        else:
            cor = "#059669"
        elementos.append(
            f'<text x="{ml - 8}" y="{y + 13}" text-anchor="end" fill="#1e293b" font-size="11">{label}</text>'
        )
        elementos.append(
            f'<rect x="{bx:.1f}" y="{y + 2}" width="{max(bw, 2.0):.1f}" height="14" rx="2" fill="{cor}"/>'
        )
        elementos.append(
            f'<text x="{ml + pw + 6}" y="{y + 13}" fill="#334155" font-size="11">{folga:+.1f}d</text>'
        )

    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Folgas CPM">' + "".join(elementos) + "</svg>"


def _render_tabela_custos(plano: Mapping[str, Any]) -> str:
    opcoes = plano.get("opcoes") or []
    if not opcoes:
        return "<p class=\"muted\">Nenhuma intervenção onerosa aplicável às frentes filtradas.</p>"

    linhas_html: list[str] = []
    for idx, op in enumerate(opcoes, start=1):
        custo_dia = op.get("custo_por_dia_brl")
        hh_add = float(op.get("hh_adicional") or 0.0)
        dias_rec = float(op.get("dias_recuperados_estimados") or 0.0)
        custo_tot = float(op.get("custo_estimado_brl") or 0.0)
        badge_sat = ' <span class="badge warn">SATURADA</span>' if op.get("saturado") else ""
        linhas_html.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td><code>{_esc(op['atividade_id'])}</code> — {_esc(op['nome'])}{badge_sat}</td>"
            f"<td>{_esc(op['tipo'])}</td>"
            f"<td><strong>{_esc(op['estrategia'])}</strong><br><small class=\"muted\">{_esc(op['descricao'])}</small></td>"
            f"<td class=\"num\">+{hh_add:.1f} HH</td>"
            f"<td class=\"num\">-{dias_rec:.2f}d</td>"
            f"<td class=\"num\">{_esc(_fmt_brl(custo_tot))}</td>"
            f"<td class=\"num\"><strong>{_esc(_fmt_brl_dia(custo_dia))}</strong></td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>#</th><th>Frente</th><th>Tipo</th><th>Alavanca</th>"
        "<th class=\"num\">Esforço</th><th class=\"num\">Ganho Est.</th>"
        "<th class=\"num\">Custo Total</th><th class=\"num\">Custo / Dia (R$/dia)</th>"
        "</tr></thead><tbody>"
        + "".join(linhas_html)
        + "</tbody></table>"
    )


def renderizar_html_executivo(payload: Mapping[str, Any]) -> str:
    """Renderiza relatório executivo HTML5 offline autocontido com gráficos SVG inline."""
    modo = str(payload.get("modo") or "")
    resumo = payload.get("resumo") or {}
    avisos = payload.get("avisos") or []
    origem = payload.get("origem") or {}
    plano_custo = payload.get("plano_otimizado_custo") or {}
    gerado_em = str(payload.get("gerado_em") or "")
    schema_version = str(payload.get("schema_version") or SCHEMA_VERSION)

    titulo_projeto = str(origem.get("projeto") or origem.get("arquivo") or "Cronograma")
    subtitulo_origem = (
        f"SQLite • Projeto {_esc(origem.get('projeto'))} • Ciclo {_esc(origem.get('ciclo'))}"
        if origem.get("fonte") == "sqlite"
        else f"CSV • {_esc(origem.get('arquivo') or 'Entrada local')}"
    )

    if modo == "monte_carlo":
        resgate = payload.get("resgate")
        termino_p80 = float(resumo.get("termino_p80") or 0.0)
        prazo = float(resumo.get("prazo_contratual") or 0.0)
        atraso_alvo = float(resumo.get("atraso_p_alvo") or 0.0)
        orcamento = resumo.get("custo_total_malha_fechada_brl")
        melhor_dia = resumo.get("melhor_opcao_custo_por_dia_brl")

        termino_p80_dep = (
            float(resgate["termino_p80_depois"]) if isinstance(resgate, Mapping) else None
        )
        svg_hist = _svg_histograma_mc(
            payload.get("histograma_antes") or [],
            payload.get("histograma_depois") or [],
            prazo=prazo,
            termino_p80_antes=termino_p80,
            termino_p80_depois=termino_p80_dep,
        )
        svg_tornado = _svg_barras_spearman(payload.get("sensibilidade") or [])
        svg_crit = _svg_criticidade_mc(payload.get("criticidade") or [])

        resgate_html = ""
        if isinstance(resgate, Mapping):
            status_cls = "ok" if resgate.get("convergiu") else "bad"
            status_txt = "META ATINGIDA" if resgate.get("convergiu") else "GAP RESIDUAL"
            gargalos = resgate.get("gargalos_residuais") or []
            garg_html = (
                "<p><strong>Gargalos residuais:</strong> "
                + ", ".join(_esc(g) for g in gargalos)
                + "</p>"
                if gargalos
                else ""
            )
            resgate_html = (
                "<section class=\"card\">"
                f"<h2>Malha Fechada Antes × Depois <span class=\"badge {status_cls}\">{status_txt}</span></h2>"
                f"<p>P80 Antes: <strong>{float(resgate['termino_p80_antes']):.1f}d</strong> → "
                f"P80 Depois: <strong>{float(resgate['termino_p80_depois']):.1f}d</strong> | "
                f"Dias recuperados: <strong>{float(resgate['dias_recuperados']):.1f}d</strong> | "
                f"Iterações: <strong>{int(resgate['iteracoes'])}</strong> | "
                f"Orçamento Crashing: <strong>{_esc(_fmt_brl(resgate.get('custo_total_malha_fechada_brl')))}</strong></p>"
                f"{garg_html}"
                "</section>"
            )

        cards_html = (
            "<section class=\"kpis\">"
            f"<div class=\"kpi\"><span>Prazo Contratual</span><strong>{prazo:.1f}d</strong></div>"
            f"<div class=\"kpi\"><span>Término P80 (Antes)</span><strong>{termino_p80:.1f}d</strong></div>"
            f"<div class=\"kpi\"><span>Desvio P{float(resumo.get('alvo_percentil') or 80):g}</span><strong>{atraso_alvo:+.1f}d</strong></div>"
            f"<div class=\"kpi\"><span>Orçamento Malha Fechada</span><strong>{_esc(_fmt_brl(orcamento) if orcamento is not None else 'N/A')}</strong></div>"
            f"<div class=\"kpi\"><span>Melhor Alavanca</span><strong>{_esc(_fmt_brl_dia(melhor_dia, fallback='N/A'))}</strong></div>"
            "</section>"
        )
        graficos_html = (
            "<section class=\"card\"><h2>Distribuição de Término (Antes × Depois)</h2>"
            f"{svg_hist}</section>"
            "<section class=\"grid-2\">"
            f"<div class=\"card\"><h2>Sensibilidade de Spearman (Tornado)</h2>{svg_tornado}</div>"
            f"<div class=\"card\"><h2>Índice de Criticidade Monte Carlo</h2>{svg_crit}</div>"
            "</section>"
            f"{resgate_html}"
        )
        titulo_modo = "Simulação Monte Carlo × PID × Custo"
    else:
        dur_cpm = float(resumo.get("duracao_cpm_dias") or 0.0)
        tot_crit = int(resumo.get("total_criticas") or 0)
        tot_al = int(resumo.get("total_alerta") or 0)
        melhor_dia = resumo.get("melhor_opcao_custo_por_dia_brl")
        cards_html = (
            "<section class=\"kpis\">"
            f"<div class=\"kpi\"><span>Duração Rede CPM</span><strong>{dur_cpm:.1f}d</strong></div>"
            f"<div class=\"kpi\"><span>Frentes Críticas</span><strong>{tot_crit}</strong></div>"
            f"<div class=\"kpi\"><span>Frentes em Alerta</span><strong>{tot_al}</strong></div>"
            f"<div class=\"kpi\"><span>Folga Mínima</span><strong>{float(resumo.get('folga_minima_dias') or 0.0):+.1f}d</strong></div>"
            f"<div class=\"kpi\"><span>Melhor Alavanca</span><strong>{_esc(_fmt_brl_dia(melhor_dia, fallback='N/A'))}</strong></div>"
            "</section>"
        )
        svg_cpm = _svg_folgas_cpm(payload.get("frentes_totais") or [])
        linhas_crit = []
        for fc in payload.get("frentes_criticas") or []:
            acoes_str = "<br>".join(_esc(a) for a in (fc.get("acoes") or []))
            linhas_crit.append(
                "<tr>"
                f"<td><code>{_esc(fc['id'])}</code> — {_esc(fc['nome'])}</td>"
                f"<td>{_esc(fc['tipo'])}</td>"
                f"<td class=\"num\">{float(fc['folga_dias']):+.1f}d</td>"
                f"<td class=\"num\">{float(fc['pid']['uk_hh']):.1f} HH</td>"
                f"<td>{acoes_str}</td>"
                "</tr>"
            )
        tabela_crit = (
            "<p class=\"muted\">Nenhuma frente crítica ou em alerta.</p>"
            if not linhas_crit
            else "<table><thead><tr><th>Frente</th><th>Tipo</th><th class=\"num\">Folga</th>"
            "<th class=\"num\">PID uk_hh</th><th>Prescrições</th></tr></thead><tbody>"
            + "".join(linhas_crit)
            + "</tbody></table>"
        )
        graficos_html = (
            f"<section class=\"card\"><h2>Folgas Totais da Rede CPM</h2>{svg_cpm}</section>"
            f"<section class=\"card\"><h2>Diagnóstico Detalhado CPM + PID</h2>{tabela_crit}</section>"
        )
        titulo_modo = "Diagnóstico Determinístico CPM + PID × Custo"

    avisos_html = ""
    if avisos:
        avisos_html = (
            "<section class=\"card alert\">"
            "<strong>AVISO (predecessoras externas/ausentes ancoradas em t=0):</strong> "
            + ", ".join(f"<code>{_esc(a)}</code>" for a in avisos)
            + "</section>"
        )

    tabela_custos_html = _render_tabela_custos(plano_custo)

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(titulo_modo)} — {_esc(titulo_projeto)}</title>
<style>
  :root {{ --bg:#f1f5f9; --card:#ffffff; --ink:#0f172a; --muted:#475569; --line:#cbd5e1; }}
  body {{ margin:0; padding:24px; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--ink); line-height:1.45; }}
  .wrap {{ max-width:1160px; margin:0 auto; }}
  header {{ background:#0f172a; color:#f8fafc; padding:20px 24px; border-radius:10px; margin-bottom:18px; }}
  header h1 {{ margin:0 0 6px 0; font-size:20px; }}
  header p {{ margin:0; color:#cbd5e1; font-size:13px; }}
  .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(175px,1fr)); gap:12px; margin-bottom:16px; }}
  .kpi {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px 16px; }}
  .kpi span {{ display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }}
  .kpi strong {{ font-size:20px; color:var(--ink); }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:16px 18px; margin-bottom:16px; overflow-x:auto; }}
  .card.alert {{ border-left:4px solid #d97706; background:#fffbeb; }}
  .grid-2 {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(460px,1fr)); gap:16px; margin-bottom:16px; }}
  h2 {{ margin:0 0 12px 0; font-size:16px; }}
  svg {{ width:100%; height:auto; display:block; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #e2e8f0; vertical-align:top; }}
  th {{ background:#f8fafc; font-weight:600; color:#334155; }}
  .num {{ text-align:right; white-space:nowrap; }}
  .muted {{ color:var(--muted); }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:700; vertical-align:middle; }}
  .badge.ok {{ background:#dcfce7; color:#166534; }}
  .badge.bad {{ background:#fee2e2; color:#991b1b; }}
  .badge.warn {{ background:#fef3c7; color:#92400e; }}
  footer {{ font-size:12px; color:var(--muted); text-align:center; padding-top:8px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>{_esc(titulo_modo)} — {_esc(titulo_projeto)}</h1>
    <p>{subtitulo_origem} | Schema v{_esc(schema_version)} | Gerado em {_esc(gerado_em)}</p>
  </header>
  {avisos_html}
  {cards_html}
  {graficos_html}
  <section class="card">
    <h2>Plano Otimizado de Recuperação (R$/dia recuperado)</h2>
    {tabela_custos_html}
  </section>
  <footer>
    Relatório executivo autocontido (100% offline, zero dependências externas). Parâmetros: <code>{_esc(json.dumps(payload.get("parametros") or {}, ensure_ascii=False))}</code>
  </footer>
</div>
</body>
</html>
"""


def exportar_html(payload: Mapping[str, Any], caminho: str | Path) -> Path:
    """Grava relatório executivo HTML5 offline criando diretórios pai automaticamente."""
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(renderizar_html_executivo(payload), encoding="utf-8")
    return destino


__all__ = [
    "SCHEMA_VERSION",
    "construir_payload_cpm_pid",
    "construir_payload_monte_carlo",
    "exportar_html",
    "exportar_json",
    "renderizar_html_executivo",
]
