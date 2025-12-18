import httpx
from pydantic import BaseModel, model_validator
from typing import Optional, Literal
from src.config.env import TYPESENSE_HUB_SEARCH_URL


class HubSearchRequest(BaseModel):
    q: Optional[str] = ""
    id: Optional[str] = ""
    type: Optional[Literal["keyword", "semantic", "hybrid", "ai"]] = "hybrid"
    page: Optional[int] = 1
    per_page: Optional[int] = 10
    alpha: Optional[float] = 0.8  # 1 -> semantic, 0 -> keyword
    threshold_semantic: Optional[float] = 0.9
    threshold_keyword: Optional[float] = 1
    threshold_hybrid: Optional[float] = 0.9
    threshold_ai: Optional[float] = 0.9
    generate_scores: Optional[bool] = False
    include_inactive: Optional[bool] = True
    exclude_agent_exclusive: Optional[bool] = False

    @model_validator(mode="after")
    def validate_q_or_id(self):
        if not self.q and not self.id:
            raise ValueError("Either 'q' or 'id' must be provided.")
        return self


async def hub_search(request: HubSearchRequest) -> Optional[dict]:
    params = request.model_dump()
    header = {"Authorization": "Bearer"}
    response = httpx.get(
        TYPESENSE_HUB_SEARCH_URL, params=params, headers=header, timeout=30.0
    )
    response.raise_for_status()
    r = response.json()

    if "results" in r:

        results_clean = []
        for doc in r["results"]:
            metadata = doc.get("metadata", {})
            agents = metadata.get("agents", {})
            results_clean.append(
                {
                    "title": doc.get("title", ""),
                    "id": doc.get("id", ""),
                    "description": doc.get("description", ""),
                    "category": doc.get("category", ""),
                    "hint": agents.get("tool_hint", ""),
                    "custo_servico": metadata.get("custo_servico", ""),
                    "descricao_completa": metadata.get("descricao_completa", ""),
                    "is_free": metadata.get("is_free", ""),
                    "orgao_gestor": metadata.get("orgao_gestor", []),
                    "publico_especifico": metadata.get("publico_especifico", []),
                    "documentos_necessarios": metadata.get(
                        "documentos_necessarios", []
                    ),
                    "instrucoes_solicitante": metadata.get(
                        "instrucoes_solicitante", ""
                    ),
                    "legislacao_relacionada": metadata.get(
                        "legislacao_relacionada", []
                    ),
                    "resultado_solicitacao": metadata.get("resultado_solicitacao", ""),
                    "resumo_plaintext": metadata.get("resumo_plaintext", ""),
                    "servico_nao_cobre": metadata.get("servico_nao_cobre", ""),
                    "tempo_atendimento": metadata.get("tempo_atendimento", ""),
                    "score_info": metadata.get("score_info", {}),
                    "ai_score": metadata.get("ai_score", {}),
                }
            )

        r["results_clean"] = results_clean
        return r
    else:
        return None

async def hub_search_by_id(request: HubSearchRequest) -> Optional[dict]:
    header = {"Authorization": "Bearer"}
    url = f"{TYPESENSE_HUB_SEARCH_URL}/{request.id}"
    print(url)
    response = httpx.get(url, headers=header, timeout=30.0)
    response.raise_for_status()
    doc = response.json()

    if doc and "id" in doc:
        result = {
                "id": doc.get("id", ""),
                "title": doc.get("nome_servico", ""),
                "resumo": doc.get("resumo", ""),
                "tempo_atendimento": doc.get("tempo_atendimento", ""),
                "custo_servico": doc.get("custo_servico", ""),
                "resultado_solicitacao": doc.get("resultado_solicitacao", ""),
                "descricao": doc.get("descricao_completa", ""),
                "documentos_necessarios": doc.get("documentos_necessarios", []),
                "instrucoes": doc.get("instrucoes_solicitante", ""),
                "servico_nao_cobre": doc.get("servico_nao_cobre", ""),
                "publico_especifico": doc.get("publico_especifico", []),
        }
        return result
    else:
        return None
