from pydantic import BaseModel, Field, ConfigDict
from typing import Any, Dict, Literal, Optional
from datetime import datetime


class ServiceRequest(BaseModel):
    """
    Estrutura da requisição para um serviço.
    """

    service_name: Optional[str]
    user_id: str
    payload: Dict[str, Any] = {}


class AgentResponse(BaseModel):
    """
    Resposta enviada para o agente a cada step.
    """

    service_name: Optional[str] = None
    error_message: Optional[str] = None
    description: str = ""
    payload_schema: Optional[Dict[str, Any]] = None
    data: Dict[str, Any] = {}


class ServiceMetadata(BaseModel):
    """
    Metadados autogeridos do serviço com timestamps.
    """

    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None

    def __init__(self, **data):
        super().__init__(**data)
        # Se updated_at não foi fornecido, usa o mesmo valor de created_at
        if self.updated_at is None:
            self.updated_at = self.created_at

    def update_timestamp(self) -> None:
        """Atualiza o timestamp de updated_at."""
        self.updated_at = datetime.now()


class ServiceState(BaseModel):
    """
    Estado completo de um serviço - fonte única da verdade.
    Contém dados persistidos, payload atual e resposta para o agente.
    """

    user_id: str
    service_name: str
    status: Literal["progress", "completed", "error"] = "progress"
    data: Dict[str, Any] = Field(default_factory=dict)
    payload: Dict[str, Any] = Field(default_factory=dict)  # Payload atual da requisição
    internal: Dict[str, Any] = Field(default_factory=dict)  # Dados internos não visíveis ao agente
    metadata: ServiceMetadata = Field(
        default_factory=ServiceMetadata
    )  # Metadados autogeridos
    agent_response: Optional[AgentResponse] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)
