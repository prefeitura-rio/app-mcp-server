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
    # Sinal opcional pra camada-tool (app.py) renderizar esta resposta como
    # WhatsApp interactive (buttons/list) em vez do menu de texto em `description`.
    # Shape buttons: {"body": str, "buttons": [{"id": str, "title": str}]}.
    # `id` é o reply_id estável; `title` é o que o cidadão vê (e volta como texto
    # no tap → parse_affirmation resolve determinístico). Default None → zero
    # efeito nos serviços que não setam (back-compat).
    interactive: Optional[Dict[str, Any]] = None


class PayloadFieldSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str | list[str] | None = None
    anyOf: list[dict[str, Any]] | None = None
    title: str | None = None
    description: str | None = None
    default: Any | None = None
    enum: list[Any] | None = None
    items: dict[str, Any] | None = None


class PayloadSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["object"] = "object"
    title: str | None = None
    description: str | None = None
    properties: dict[str, PayloadFieldSchema] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)


class ChannelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["flow_sent", "interactive_sent"]
    next_step: str | None = None
    instruction: str | None = None
    flow_token: str | None = None


class MultiStepServiceOutput(BaseModel):
    """
    Contrato publico da tool multi_step_service.

    `status` descreve o estado do workflow; `channel_action` descreve efeitos
    ja executados fora da resposta textual, como WhatsApp Flow ou interativo.
    """

    model_config = ConfigDict(extra="forbid")

    service_name: str
    status: Literal["in_progress", "completed", "error"]
    description: str = ""
    payload_schema: PayloadSchema | None = None
    data: Dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    channel_action: ChannelAction | None = None

    @classmethod
    def from_agent_response(
        cls,
        response: AgentResponse,
        *,
        workflow_status: Literal["progress", "completed", "error"] = "progress",
    ) -> "MultiStepServiceOutput":
        status: Literal["in_progress", "completed", "error"]
        if response.error_message or workflow_status == "error":
            status = "error"
        elif workflow_status == "completed":
            status = "completed"
        else:
            status = "in_progress"

        return cls(
            service_name=response.service_name or "",
            status=status,
            description=response.description,
            payload_schema=response.payload_schema,
            data=response.data,
            error_message=response.error_message,
        )


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
    internal: Dict[str, Any] = Field(
        default_factory=dict
    )  # Dados internos não visíveis ao agente
    metadata: ServiceMetadata = Field(
        default_factory=ServiceMetadata
    )  # Metadados autogeridos
    agent_response: Optional[AgentResponse] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)
