from src.tools.multi_step_service.core.models import AgentResponse, ServiceState


def ticket_opened(
    state: ServiceState, protocol_id: str, description: str
) -> ServiceState:
    state.data["protocol_id"] = protocol_id
    state.data["ticket_created"] = True
    state.agent_response = AgentResponse(description=description)
    state.data["_reset_on_next_call"] = True

    return state


def ticket_failed(
    state: ServiceState,
    *,
    error_code: str,
    description: str,
    error_message: str | None = None,
    reset_workflow: bool = True,
) -> ServiceState:
    state.data["ticket_created"] = False
    state.agent_response = AgentResponse(
        description=description,
        error_message=error_message,
    )
    if reset_workflow:
        # Erro NÃO-retryable (dados inválidos, duplicado, endereço ausente): marca
        # `error` e agenda o reset do workflow no próximo turno. `_reset_on_next_call`
        # faz o base_workflow WIPAR state.data (era pensado pro pós-SUCESSO — ver
        # ticket_opened); como vai limpar tudo mesmo, `error` aqui é só diagnóstico.
        state.data["error"] = error_code
        state.data["_reset_on_next_call"] = True
    else:
        # Erro RETRYABLE (SGRC fora do ar / transitório): preserva TODO o estado
        # coletado+confirmado p/ o "tente novamente" re-rodar `_open_ticket` com os
        # mesmos dados = retry de verdade (incidente 2026-06-04 — antes re-abria o
        # form do zero). NÃO setar `data["error"]`: ele gateia
        # `_has_valid_confirmed_address` (address.py) e o retry re-pediria endereço
        # em vez de reabrir o ticket. O erro vai pro cidadão via agent_response
        # (description + error_message). Mesma lógica do guard endereco_ausente que
        # já fazia `data.pop("error")` (sgrc.py).
        state.data.pop("error", None)

    return state
