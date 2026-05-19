"""
Normalização de prefill_data: workflow Python → IDs canônicos do Flow JSON.

Workflows do `multi_step_service` armazenam dados em chaves/valores internos
(pt-BR, aliases numerados, separações como `luminaria_quantidade='grupo'`
+ `luminaria_intercaladas_bloco='bloco'`). O Flow JSON declara IDs canônicos
diferentes em `data-source` (ex: `qty_pattern='bloco'`).

Sem normalização, prefill silenciosamente falha — Meta dropa option IDs
desconhecidos sem error, deixando RadioButtons vazios.

Cada `service_name` tem normalizador próprio.
"""

from __future__ import annotations

from typing import Any


# ─────────────────────────────────────────────────────────────────────
# reparo_luminaria
# ─────────────────────────────────────────────────────────────────────

# Whitelist de IDs aceitos pelo Flow JSON (data-source IDs canônicos)
_LUMINARIA_VALID_DEFECTS: set[str] = {
    "Apagada",
    "Piscando",
    "Acesa de dia",
    "Pendurada",
    "Danificada",
    "Com ruído",
}

# Aliases case-insensitive comuns → ID canônico. LLM/workflow podem produzir
# variações ("apagada" lowercase, "Acesa durante o dia" forma humana).
# Codex P2 round 7: sem isso, valores válidos workflow-side eram dropados.
_LUMINARIA_DEFECT_ALIASES: dict[str, str] = {
    "apagada": "Apagada",
    "piscando": "Piscando",
    "acesa de dia": "Acesa de dia",
    "acesa durante o dia": "Acesa de dia",
    "acesa": "Acesa de dia",
    "pendurada": "Pendurada",
    "danificada": "Danificada",
    "com ruído": "Com ruído",
    "com ruido": "Com ruído",
}

_LUMINARIA_VALID_QTY: set[str] = {"uma", "bloco", "intercaladas"}

_LUMINARIA_VALID_LOCATIONS: set[str] = {
    "Calçada",
    "Fachada",
    "Monumento",
    "Parque",
    "Praça",
    "Quadra de esportes",
    "Rua",
    "Não sei",
}

_LUMINARIA_LOCATION_ALIASES: dict[str, str] = {
    "calçada": "Calçada",
    "calcada": "Calçada",
    "fachada": "Fachada",
    "monumento": "Monumento",
    "parque": "Parque",
    "praça": "Praça",
    "praca": "Praça",
    "quadra de esportes": "Quadra de esportes",
    "quadra": "Quadra de esportes",
    "rua": "Rua",
    "não sei": "Não sei",
    "nao sei": "Não sei",
    "naosei": "Não sei",
}


def _resolve_alias(value: Any, aliases: dict[str, str], valid: set[str]) -> str | None:
    """Tenta resolver `value` pra ID canônico via match exato, depois alias
    case-insensitive. Retorna None se não casa."""
    if not isinstance(value, str):
        return None
    if value in valid:
        return value
    canonical = aliases.get(value.strip().lower())
    return canonical if canonical in valid else None


def _normalize_luminaria(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Workflow `reparo_luminaria` → Flow JSON IDs.

    Mapeamentos:
        luminaria_defeito | defect_type        → defect_type (se valid)
        luminaria_localizacao | location       → location    (se valid)
        address | endereco                     → endereco    (string truthy)
        luminaria_quantidade='uma'             → qty_pattern='uma'
        luminaria_quantidade='grupo'
            + luminaria_intercaladas_bloco='X' → qty_pattern=X (bloco/intercaladas)
        qty_pattern='X' (já canônico)          → qty_pattern=X (se valid)

    Valores que não passam validação são silenciosamente dropados (codex P2:
    Meta dropa option IDs inválidos sem erro, melhor não enviar).
    """
    out: dict[str, Any] = {}

    # defect_type (case-insensitive + aliases)
    defect_raw = payload.get("defect_type") or payload.get("luminaria_defeito")
    defect = _resolve_alias(
        defect_raw, _LUMINARIA_DEFECT_ALIASES, _LUMINARIA_VALID_DEFECTS
    )
    if defect:
        out["defect_type"] = defect

    # location (case-insensitive + aliases)
    location_raw = payload.get("location") or payload.get("luminaria_localizacao")
    location = _resolve_alias(
        location_raw, _LUMINARIA_LOCATION_ALIASES, _LUMINARIA_VALID_LOCATIONS
    )
    if location:
        out["location"] = location

    # endereco (free-text — string truthy)
    endereco = payload.get("endereco") or payload.get("address")
    if isinstance(endereco, str) and endereco.strip():
        out["endereco"] = endereco.strip()
    elif isinstance(endereco, dict):
        # workflow às vezes guarda endereço como dict {logradouro, numero, bairro}
        parts = [
            endereco.get("logradouro_nome_ipp") or endereco.get("logradouro"),
            endereco.get("numero"),
            endereco.get("bairro_nome_ipp") or endereco.get("bairro"),
        ]
        joined = ", ".join(str(p) for p in parts if p)
        if joined:
            out["endereco"] = joined

    # qty_pattern — caminho canônico (já era um dos IDs)
    qty_raw = payload.get("qty_pattern")
    if isinstance(qty_raw, str) and qty_raw in _LUMINARIA_VALID_QTY:
        out["qty_pattern"] = qty_raw
    else:
        # Caminho workflow: luminaria_quantidade + luminaria_intercaladas_bloco
        quantidade = payload.get("luminaria_quantidade")
        intercaladas = payload.get("luminaria_intercaladas_bloco")
        if quantidade == "uma":
            out["qty_pattern"] = "uma"
        elif quantidade == "grupo" and intercaladas in _LUMINARIA_VALID_QTY:
            # intercaladas tem valor "bloco" ou "intercaladas" no workflow
            out["qty_pattern"] = intercaladas

    return out


# ─────────────────────────────────────────────────────────────────────
# Dispatcher por service_name
# ─────────────────────────────────────────────────────────────────────

_NORMALIZERS = {
    "reparo_luminaria": _normalize_luminaria,
}


def normalize_prefill_for_flow(
    service_name: str, raw_prefill: dict[str, Any] | None
) -> dict[str, Any]:
    """
    Aplica normalizer por service. Service desconhecido retorna pass-through
    (sem mapping) — caller pode passar prefill bruto sem normalização,
    e o sender ainda normaliza nomes via sufixo `_prefill`.

    Args:
        service_name: chave do FLOW_TEMPLATES (ex: "reparo_luminaria")
        raw_prefill: dict cru do workflow / chat. None ou {} → {}

    Returns:
        dict com keys canônicas do Flow JSON e valores válidos pra option IDs.
    """
    if not raw_prefill:
        return {}
    normalizer = _NORMALIZERS.get(service_name)
    if not normalizer:
        # Service sem normalizer custom: pass-through filtrado pra valores truthy.
        return {k: v for k, v in raw_prefill.items() if v not in (None, "", {})}
    return normalizer(raw_prefill)
