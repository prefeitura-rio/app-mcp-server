import sys


from src.tools.multi_step_service.core.models import AgentResponse, ServiceState

poda_state_helpers = sys.modules[
    "src.tools.multi_step_service.workflows.poda_de_arvore.state_helpers"
]


def test_ticket_opened_sets_ticket_state():
    state = ServiceState(user_id="u1", service_name="poda_de_arvore")

    result = poda_state_helpers.ticket_opened(
        state,
        protocol_id="12345",
        description="Chamado aberto com sucesso",
    )

    assert result.data["protocol_id"] == "12345"
    assert result.data["ticket_created"] is True
    assert result.agent_response == AgentResponse(
        description="Chamado aberto com sucesso"
    )


def test_ticket_failed_sets_error_state():
    state = ServiceState(user_id="u1", service_name="poda_de_arvore")

    result = poda_state_helpers.ticket_failed(
        state,
        error_code="API_ERROR",
        description="Falha ao abrir chamado",
        error_message="sem conexão",
    )

    assert result.data["ticket_created"] is False
    assert result.data["error"] == "API_ERROR"
    assert result.agent_response.description == "Falha ao abrir chamado"
    assert result.agent_response.error_message == "sem conexão"
