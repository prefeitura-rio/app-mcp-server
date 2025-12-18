from src.tools.multi_step_service.core.models import AgentResponse, ServiceState


def ticket_opened(state: ServiceState, protocol_id: str, description: str) -> ServiceState:
    state.data["protocol_id"] = protocol_id
    state.data["ticket_created"] = True
    state.agent_response = AgentResponse(description=description)
    return state


def ticket_failed(
    state: ServiceState,
    *,
    error_code: str,
    description: str,
    error_message: str | None = None,
) -> ServiceState:
    state.data["ticket_created"] = False
    state.data["error"] = error_code
    state.agent_response = AgentResponse(
        description=description,
        error_message=error_message,
    )
    return state
