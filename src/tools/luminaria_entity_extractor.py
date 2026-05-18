"""
Utilitários para codificar/decodificar dados de pre-fill no flow_token.

O agente (LLM) é responsável por extrair entidades da mensagem do usuário.
Este módulo apenas codifica essas entidades no flow_token para envio ao WhatsApp.
"""

from typing import Dict, Optional


def encode_flow_token(base_token: str, entities: Dict[str, Optional[str]]) -> str:
    """
    Codifica o flow_token com entidades extraídas.

    Args:
        base_token: Token UUID base
        entities: Dicionário de entidades extraídas

    Returns:
        Token codificado no formato: uuid|key1=val1|key2=val2

    Exemplo:
        >>> encode_flow_token("abc-123", {"defect_type": "Pendurada"})
        "abc-123|defect_type=Pendurada"
    """
    parts = [base_token]

    for key, value in entities.items():
        if value:  # Somente adiciona se não for None
            parts.append(f"{key}={value}")

    return "|".join(parts)


def decode_flow_token(flow_token: str) -> Dict[str, Optional[str]]:
    """
    Decodifica flow_token com entidades extraídas.

    Args:
        flow_token: Token no formato uuid|key1=val1|key2=val2

    Returns:
        Dicionário com entidades extraídas

    Exemplo:
        >>> decode_flow_token("abc-123|defect_type=Pendurada|qty_pattern=uma")
        {"defect_type": "Pendurada", "qty_pattern": "uma", "location": None}
    """
    entities: Dict[str, Optional[str]] = {
        "defect_type": None,
        "qty_pattern": None,
        "location": None,
    }

    if "|" not in flow_token:
        return entities  # Token simples sem entidades

    parts = flow_token.split("|")[1:]  # Pula o UUID

    for part in parts:
        if "=" in part:
            key, value = part.split("=", 1)
            if key in entities:
                entities[key] = value

    return entities
