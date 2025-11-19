from typing import Optional, List, Union
from pydantic import BaseModel, Field, field_validator
from src.config.env import EQUIPMENTS_VALID_THEMES

class EquipmentsInstructionsPayload(BaseModel):
    tema: str = Field(..., description="Tema específico para filtrar as instruções iniciais. Use para refinar as instruções quando o contexto do usuário já for claro.")
    
    @field_validator("tema")
    @classmethod
    def validate_tema(cls, v: str) -> str:
        if v not in EQUIPMENTS_VALID_THEMES:
            raise ValueError(f"Tema inválido. Temas aceitos: {', '.join(EQUIPMENTS_VALID_THEMES)}")
        return v

class EquipmentsSearchPayload(BaseModel):
    address: str = Field(..., description="O endereço completo para busca de equipamentos. Ex: 'Rua da Assembleia, 10, Centro, Rio de Janeiro'.")
    categories: Optional[List[str]] = Field(default=[], description="Lista de categorias de equipamentos para filtrar (ex: 'CF', 'CMS', 'CRAS', 'PONTOS_DE_APOIO'). O Agente DEVE inferir estas categorias com base na intenção do usuário e nas instruções fornecidas no passo anterior. NÃO pergunte explicitamente ao usuário sobre categorias.")