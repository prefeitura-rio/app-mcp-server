"""
Ferramentas para armazenar feedback de usuários no BigQuery.
"""

import asyncio
from typing import Dict, Any
from src.utils.bigquery import save_feedback_in_bq_background, get_datetime
from src.utils.log import logger


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

        # Gera timestamp
        timestamp = get_datetime()
        
        # Salva no BigQuery em background usando a função especializada para feedback
        asyncio.create_task(
            save_feedback_in_bq_background(
                user_id=user_id.strip(),
                feedback=feedback.strip(),
                timestamp=timestamp,
                dataset_id="brutos_eai_logs",
                table_id="feedback",
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
