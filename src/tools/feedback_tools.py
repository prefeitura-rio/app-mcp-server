"""
Ferramentas de feedback para o servidor FastMCP.
"""

import asyncio
from typing import Dict, Any
from src.utils.bigquery import save_feedback_in_bq_background, get_datetime
from src.utils.log import logger
from src.config.env import ENVIRONMENT
from src.utils.error_interceptor import interceptor


@interceptor(
    source={"source": "mcp", "tool": "feedback"},
    extract_user_id=lambda args, kwargs: kwargs.get("user_id") or (args[0] if args else "unknown"),
)
async def store_user_feedback(user_id: str, feedback: str) -> Dict[str, Any]:
    """
    Armazena feedback do usuário no BigQuery.

    Args:
        user_id: ID único do usuário
        feedback: Texto do feedback fornecido pelo usuário

    Returns:
        Dict com status de sucesso, timestamp e mensagem de instruções
    """
    try:
        # Valida se os parâmetros não estão vazios
        if not user_id or not user_id.strip():
            return {
                "success": False,
                "error": "user_id não pode estar vazio",
                "timestamp": None,
                "message": None
            }
        
        if not feedback or not feedback.strip():
            return {
                "success": False,
                "error": "feedback não pode estar vazio",
                "timestamp": None,
                "message": None
            }

        if feedback.strip() == "closed_beta_feedback":
            return {
                "success": True,
                "timestamp": None,
                "message": "A mensagem não é um feedback. Cumprimente o usuário e pergunte como pode ajudá-lo. Ex.: Olá! Como posso ajudar?",
                "error": None
            }

        # Gera timestamp
        timestamp = get_datetime()
        
        # Salva no BigQuery de forma assíncrona
        asyncio.create_task(
            save_feedback_in_bq_background(
                user_id=user_id.strip(),
                feedback=feedback.strip(),
                timestamp=timestamp,
                environment=ENVIRONMENT,
            )
        )

        logger.info(f"Feedback do usuário {user_id} salvo com sucesso no BigQuery")

        return {
            "success": True,
            "timestamp": timestamp,
            "message": "Feedback armazenado com sucesso. Você pode agradecer ao usuário e informar que o feedback foi registrado para análise e melhorias futuras.",
            "error": None
        }

    except Exception as e:
        logger.error(f"Erro ao salvar feedback no BigQuery: {str(e)}")
        return {
            "success": False,
            "error": f"Erro interno ao salvar feedback: {str(e)}",
            "timestamp": None,
            "message": None
        }
