"""Classificação de frentes (Interna vs Externa) insensível a acentos."""
from __future__ import annotations

import unicodedata
from typing import Mapping

CHAVES_EXTERNAS = (
    # Português
    "compra",
    "aprova",
    "entrega",
    "inspec",
    "fornecedor",
    "cliente",
    "terceir",
    "espera",
    "documento",
    "valvula",
    # Inglês (suprimentos, órgãos reguladores, aprovações de cliente/terceiros)
    "permit",
    "approv",
    "deliver",
    "vendor",
    "supplier",
    "procure",
    "long-lead",
    "long lead",
    "landlord",
    "client",
    "customer",
    "subcontract",
    "manufacture",
    "valve",
    "inspect",
    "third-party",
    "sign the lease",
    "lease agreement",
    "regulatory",
    "sign-off",
    "acceptance",
    "loan",
    "financing",
    "sale agreement",
)


def _normalizar_texto(texto: str | None) -> str:
    """Remove acentos/diacríticos via NFKD e converte para minúsculas."""
    decomposed = unicodedata.normalize("NFKD", texto or "")
    sem_acentos = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return sem_acentos.lower().strip()


def classificar_tipo(
    nome: str,
    tipo_explicito: str | None = None,
    overrides: Mapping[str, str] | None = None,
    task_id: str | None = None,
) -> str:
    """Classifica uma frente como `Interna` ou `Externa` respeitando overrides explícitos."""
    if overrides:
        if task_id and task_id in overrides and str(overrides[task_id]).strip():
            return str(overrides[task_id]).strip()
        if nome in overrides and str(overrides[nome]).strip():
            return str(overrides[nome]).strip()

    if tipo_explicito is not None and tipo_explicito.strip():
        return tipo_explicito.strip()

    normalizado = _normalizar_texto(nome)
    if any(chave in normalizado for chave in CHAVES_EXTERNAS):
        return "Externa"
    return "Interna"
