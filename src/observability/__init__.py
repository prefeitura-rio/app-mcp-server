"""
Observability helpers para o MCP server.

Atualmente expõe apenas o módulo `audit_log` — hooks pra registrar acoes
com side-effect em tools sensiveis (LGPD + accountability gov).

C4 do plano-bot-2026 Fase 0.
"""

from src.observability.audit_log import (
    audit_log,
    record_action,
    hash_user_id,
    redact_pii,
    generate_snowflake_id,
)

__all__ = [
    "audit_log",
    "record_action",
    "hash_user_id",
    "redact_pii",
    "generate_snowflake_id",
]
