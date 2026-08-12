from src.tools.multi_step_service.core.orchestrator import Orchestrator


def test_divida_ativa_esta_registrado_no_orchestrator():
    workflows = Orchestrator().list_workflows()

    assert "divida_ativa" in workflows
    assert "dívidas ativas" in workflows["divida_ativa"]
