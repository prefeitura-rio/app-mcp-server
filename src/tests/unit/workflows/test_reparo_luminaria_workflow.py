import asyncio
from types import SimpleNamespace

import pytest

from src.flows._token import encode_flow_token
from src.flows.reparo_luminaria.handler import _handle_init
from src.tools.multi_step_service.core.models import ServiceState
from src.tools.multi_step_service.workflows.reparo_luminaria import templates as rlu_tpl
from src.tools.multi_step_service.workflows.reparo_luminaria.integrations import (
    build_ticket_payload,
)
from src.tools.multi_step_service.workflows.reparo_luminaria.models import (
    LuminariaDefeitoPayload,
    LuminariaIntercaladasBlocoPayload,
    LuminariaLocalizacaoPayload,
    LuminariaQuantidadePayload,
)
from src.tools.multi_step_service.workflows.reparo_luminaria.workflow import (
    ReparoLuminariaWorkflow,
)
from src.tools.multi_step_service.workflows.sgrc_components import templates as sgrc_tpl
from src.tools.multi_step_service.workflows.sgrc_components.models import (
    AddressConfirmationPayload,
    AddressData,
    AddressPayload,
    AddressValidationState,
    CPFPayload,
    EmailPayload,
    NomePayload,
    PontoReferenciaPayload,
    TicketDataConfirmationPayload,
)
from src.tools.multi_step_service.workflows.sgrc_components.ticket_state import (
    ticket_failed,
    ticket_opened,
)
from src.tools.whatsapp_flows.normalizers import normalize_prefill_for_flow


def make_state(payload=None, data=None):
    return ServiceState(
        user_id="u1",
        service_name="reparo_luminaria",
        payload=payload or {},
        data=data or {},
    )


def make_workflow():
    return ReparoLuminariaWorkflow(use_fake_api=True)


def test_blank_endereco_from_flow_not_aliased_to_address():
    """Campo endereco OPCIONAL do Flow submetido em branco NÃO vira address=""
    (que dispararia 'Endereço não pode estar vazio' em _collect_address);
    preenchido é aliased normalmente pra address."""
    workflow = make_workflow()

    blank = make_state(
        payload={
            "_source": "whatsapp_flow",
            "defect_type": "Pendurada",
            "location": "Rua",
            "endereco": "",
        }
    )
    workflow._normalize_payload_aliases(blank)
    assert "address" not in blank.payload

    filled = make_state(payload={"_source": "whatsapp_flow", "endereco": "Rua X, 100"})
    workflow._normalize_payload_aliases(filled)
    assert filled.payload["address"] == "Rua X, 100"


def test_reparo_luminaria_payload_validators_accept_aliases():
    assert (
        LuminariaDefeitoPayload.model_validate(
            {"luminaria_defeito": "acesa durante o dia"}
        ).luminaria_defeito
        == "Acesa de dia"
    )
    assert (
        LuminariaQuantidadePayload.model_validate(
            {"luminaria_quantidade": "2"}
        ).luminaria_quantidade
        == "grupo"
    )
    assert (
        LuminariaIntercaladasBlocoPayload.model_validate(
            {"luminaria_intercaladas_bloco": "sequência"}
        ).luminaria_intercaladas_bloco
        == "bloco"
    )
    assert (
        LuminariaLocalizacaoPayload.model_validate(
            {"luminaria_localizacao": "nao sei"}
        ).luminaria_localizacao
        is None
    )


@pytest.mark.parametrize(
    ("model", "payload", "message"),
    [
        (LuminariaDefeitoPayload, {"luminaria_defeito": "outro"}, "invalido"),
        (LuminariaQuantidadePayload, {"luminaria_quantidade": "muitas"}, "invalida"),
        (
            LuminariaIntercaladasBlocoPayload,
            {"luminaria_intercaladas_bloco": "longe"},
            "Opcao",
        ),
        (LuminariaLocalizacaoPayload, {"luminaria_localizacao": "poste"}, "invalida"),
    ],
)
def test_reparo_luminaria_payload_validators_reject_invalid_values(
    model, payload, message
):
    with pytest.raises(ValueError, match=message):
        model.model_validate(payload)


def test_sgrc_payload_validators_normalize_optional_data():
    assert NomePayload.model_validate({"name": "  maria   da silva "}).name == (
        "Maria Da Silva"
    )
    assert EmailPayload.model_validate({"email": " USER@MAIL.COM "}).email == (
        "user@mail.com"
    )
    assert CPFPayload.model_validate({"cpf": "123.456.789-09"}).cpf == "12345678909"
    assert CPFPayload.model_validate({"cpf": ""}).cpf is None
    assert (
        AddressData.model_validate(
            {"logradouro": "Rua A", "bairro": "Centro", "cep": "20.000-000"}
        ).cep
        == "20000000"
    )
    assert AddressPayload(address="Rua A").address == "Rua A"
    assert AddressConfirmationPayload(confirmacao=True).confirmacao is True
    assert AddressValidationState().attempts == 0
    assert PontoReferenciaPayload(ponto_referencia="Praça").ponto_referencia == "Praça"
    assert TicketDataConfirmationPayload(confirmacao=False, correcao="email").correcao


def test_reparo_and_sgrc_templates_return_expected_texts():
    service_info = {
        "nome": "Reparo de luminária",
        "resumo": "Troca e reparo",
        "prazo": "7 dias",
        "servico_nao_cobre": "Rede particular",
    }
    assert "**Serviço:** Reparo de luminária" in rlu_tpl.solicitar_defeito(service_info)
    assert "1. Apagada" in rlu_tpl.solicitar_defeito()
    assert "1 a 6" in rlu_tpl.defeito_invalido()
    assert "grupo de luminárias" in rlu_tpl.solicitar_quantidade()
    assert "uma luminária" in rlu_tpl.quantidade_invalida()
    assert "funcionando entre" in rlu_tpl.solicitar_intercaladas_bloco()
    assert "juntas" in rlu_tpl.intercaladas_bloco_invalido()
    assert "Quadra de esportes" in rlu_tpl.solicitar_localizacao()
    assert "não sei" in rlu_tpl.localizacao_invalida()
    assert "Afonso Cavalcanti" in rlu_tpl.solicitar_endereco()
    assert "tentativa 1/3" in rlu_tpl.endereco_nao_localizado(1, 3)
    # POC1 #3: o re-prompt GUIA (pede número + bairro), não é só "não encontrei".
    _nao_loc = rlu_tpl.endereco_nao_localizado(1, 3)
    assert "número" in _nao_loc and "bairro" in _nao_loc
    assert "processar o endereço" in rlu_tpl.endereco_erro_processamento(1, 3)
    assert "3 tentativas" in rlu_tpl.endereco_maximo_tentativas()
    assert "1746" in rlu_tpl.endereco_maximo_tentativas()
    assert "Rua A" in rlu_tpl.confirmar_endereco("Rua A")
    assert "histórico" in rlu_tpl.endereco_historico("Rua A")
    assert "sim ou não" in rlu_tpl.confirmar_resposta_invalida()
    assert "tentativa 2/3" in rlu_tpl.solicitar_novo_endereco(2, 3)
    assert "ponto de referência" in rlu_tpl.solicitar_ponto_referencia()
    assert "quadra de esportes" in rlu_tpl.perguntar_quadra_esportes()
    assert "dados da sua solicitação" in rlu_tpl.confirmar_dados_ticket("dados")
    assert "corrigido" in rlu_tpl.solicitar_correcao_dados()
    assert "defeito correto" in rlu_tpl.dados_corrigidos_solicitar_campo("defeito")
    assert "campo_x" in rlu_tpl.dados_corrigidos_solicitar_campo("campo_x")
    assert "PROTO" in rlu_tpl.solicitacao_criada_sucesso("PROTO")
    assert "já existe" in rlu_tpl.solicitacao_existente("PROTO")
    assert "1746.rio" in rlu_tpl.msg_solicitacao()
    assert "não pôde ser criada" in rlu_tpl.erro_criar_solicitacao()
    assert "indisponível" in rlu_tpl.sistema_indisponivel()
    assert "abrir o chamado" in rlu_tpl.erro_geral_chamado()
    assert "Informe o endereço" in rlu_tpl.reiniciar_apos_erro("Falha")

    assert "CPF" in sgrc_tpl.solicitar_cpf(required=True)
    assert "Tentativa 1/3" in sgrc_tpl.cpf_invalido(1)
    assert "CPF válido" in sgrc_tpl.maximo_tentativas_excedido(required=True)
    assert "email" in sgrc_tpl.solicitar_email()
    assert "Email inválido" in sgrc_tpl.email_invalido(1, required=True)
    assert "email válido" in sgrc_tpl.email_maximo_tentativas(required=True)
    assert "nome completo" in sgrc_tpl.solicitar_nome(required=True)
    assert "Nome inválido" in sgrc_tpl.nome_invalido(1)
    assert "nome válido" in sgrc_tpl.nome_maximo_tentativas(required=True)
    assert "- CPF: XXX" in sgrc_tpl.confirmar_dados_salvos(["- CPF: XXX"])


def test_reparo_ticket_builder_and_ticket_state_helpers():
    state = make_state(
        data={
            "address": {
                "logradouro": "Rua Original",
                "logradouro_nome_ipp": "Rua IPP",
                "logradouro_id_ipp": "123",
                "bairro": "Centro",
                "bairro_nome_ipp": "Bairro IPP",
                "bairro_id_ipp": "456",
                "numero": "10A",
                "cep": "20000-000",
            },
            "ponto_referencia": "Perto da praça",
            "email": "user@example.com",
            "cpf": "12345678909",
            "name": "Nome Sobrenome",
            "phone": "21999999999",
        }
    )

    address, requester, description = build_ticket_payload(state)

    assert description == "Reparo de luminária"
    assert address.street == "Rua IPP"
    assert address.number == "10"
    # Ponto de referência vai pro complemento (não localidade) no payload SGRC.
    assert address.complement == "Perto da praça"
    assert address.locality == ""
    assert requester.email == "user@example.com"
    assert requester.phones.telefone1 == "21999999999"

    opened = ticket_opened(state, "PROTO", "Criado")
    assert opened.data["ticket_created"] is True
    assert opened.data["protocol_id"] == "PROTO"

    failed = ticket_failed(
        state,
        error_code="erro",
        description="Falhou",
        error_message="boom",
    )
    assert failed.data["ticket_created"] is False
    assert failed.data["error"] == "erro"
    assert failed.agent_response.description == "Falhou"
    assert failed.agent_response.error_message == "boom"


def test_ticket_failed_reset_workflow_flag():
    """Erro RETRYABLE (reset_workflow=False) NÃO seta _reset_on_next_call → o
    base_workflow preserva os dados já coletados/confirmados e o "tente novamente"
    re-roda _open_ticket em vez de re-abrir o formulário do zero (incidente
    2026-06-04: SGRC fora do ar caía no catch-all e descartava tudo). Default
    (True) mantém o reset pros erros não-retryable (dados inválidos/duplicado)."""
    # Default → reseta (comportamento legado p/ erros não-retryable)
    s1 = make_state(data={"cpf": "12345678909", "logradouro": "Rua X"})
    out1 = ticket_failed(s1, error_code="erro_interno", description="x")
    assert out1.data.get("_reset_on_next_call") is True

    # Retryable → NÃO reseta; dados sobrevivem p/ o retry
    s2 = make_state(data={"cpf": "12345678909", "logradouro": "Rua X"})
    out2 = ticket_failed(
        s2, error_code="erro_geral", description="x", reset_workflow=False
    )
    assert "_reset_on_next_call" not in out2.data
    assert out2.data["cpf"] == "12345678909"
    assert out2.data["logradouro"] == "Rua X"
    assert out2.data["ticket_created"] is False


@pytest.mark.asyncio
async def test_retryable_failure_preserves_state_for_retry(monkeypatch):
    """Erro RETRYABLE (SGRC fora do ar → catch-all erro_geral): preserva TODO o
    estado, LIMPA o gate `error` e NÃO agenda reset → o "tente novamente" re-roda
    _open_ticket com os mesmos dados, sem re-pedir endereço nem re-abrir o form
    (incidente 2026-06-04 + achado do code review: o gate `error` em
    _has_valid_confirmed_address derailava o retry pro endereço)."""
    workflow = ReparoLuminariaWorkflow(use_fake_api=False)
    # payload pronto (address como string bypassa o guard de logradouro) +
    # atributos vazios pra isolar o teste na lógica de retry.
    monkeypatch.setattr(
        workflow,
        "build_ticket_payload",
        lambda state: ("Rua das Luzes, 100", "requester", "descricao"),
    )
    monkeypatch.setattr(workflow, "build_specific_attributes", lambda state: {})

    base = {
        "address_validated": True,
        "address_confirmed": True,
        "ticket_data_confirmed": True,
        "cpf": "12345678909",
        "defect_type": "apagada",
    }

    async def _boom(**_):
        raise RuntimeError("SGRC indisponível")

    monkeypatch.setattr(workflow, "new_ticket", _boom)
    failed = await workflow._open_ticket(make_state(data=dict(base)))
    assert failed.data["ticket_created"] is False
    assert "error" not in failed.data  # gate de endereço LIMPO
    assert "_reset_on_next_call" not in failed.data  # estado NÃO será wipado
    assert failed.data["cpf"] == "12345678909"  # dados preservados
    # endereço segue válido → o retry NÃO re-pergunta endereço (era o bug do review)
    assert workflow._has_valid_confirmed_address(failed) is True

    async def _ok(**_):
        return SimpleNamespace(protocol_id="PROTO-RETRY")

    monkeypatch.setattr(workflow, "new_ticket", _ok)
    retried = await workflow._open_ticket(failed)  # "tente novamente"
    assert retried.data["ticket_created"] is True
    assert retried.data["protocol_id"] == "PROTO-RETRY"


@pytest.mark.asyncio
async def test_open_ticket_sem_logradouro_falha_cedo_sem_chamar_sgrc(monkeypatch):
    """Endereço sem logradouro → falha acionável (chamado_sem_endereco) ANTES do
    SGRC, em vez do catch-all genérico 'erro ao abrir o chamado' (bug do teste
    real do Bruno, 2026-06-01)."""
    workflow = make_workflow()
    workflow.use_fake_api = False  # força o caminho real pra exercitar o guard

    # Endereço vazio que chegou ao open_ticket JÁ "confirmado" (o cenário real:
    # passou batido pela confirmação). Os flags abaixo simulam esse estado stale.
    state = make_state(
        data={
            "address": {},
            "address_confirmed": True,
            "address_validated": True,
            "ticket_data_confirmed": True,
        }
    )

    chamou_sgrc = False

    async def _boom_new_ticket(*args, **kwargs):
        nonlocal chamou_sgrc
        chamou_sgrc = True
        raise AssertionError("new_ticket não deve ser chamado com endereço vazio")

    monkeypatch.setattr(workflow, "new_ticket", _boom_new_ticket)

    result = await workflow._open_ticket(state)

    assert chamou_sgrc is False
    assert result.data["ticket_created"] is False
    assert result.agent_response.description == rlu_tpl.chamado_sem_endereco()
    # Diagnóstico preservado no agent_response (telemetria), não em data["error"].
    assert result.agent_response.error_message == "endereço sem logradouro"

    # Recuperação: os flags de endereço confirmado E o gate `error` são limpos —
    # senão a próxima mensagem com um novo endereço seria ignorada (curto-circuito
    # em _has_valid_confirmed_address) e o guard re-dispararia num loop.
    assert "address_confirmed" not in result.data
    assert "address_validated" not in result.data
    assert "ticket_data_confirmed" not in result.data
    assert "error" not in result.data

    # 2ª mensagem (recuperação real): com um novo endereço, _collect_address
    # re-coleta de fato em vez de curto-circuitar — prova que o loop foi quebrado.
    workflow.use_fake_api = True
    result.payload = {"address": "Rua Nova 123, Centro"}
    result.agent_response = None
    recollected = await workflow._collect_address(result)
    assert recollected.data.get("address_temp"), "novo endereço não foi re-coletado"


@pytest.mark.asyncio
async def test_reparo_workflow_initializes_with_service_knowledge():
    workflow = make_workflow()
    workflow.service_knowledge = {
        "title": "Reparo",
        "resumo": "Resumo",
        "tempo_atendimento": "7 dias",
        "custo_servico": "Gratuito",
        "servico_nao_cobre": "Rede privada",
    }
    state = make_state(payload={"endereco": "Rua A", "defeito": "apagada"})

    result = await workflow._initialize_workflow(state)

    assert result.data["codigo_servico_1746"] == "18131"
    assert result.data["service_info"]["nome"] == "Reparo"
    assert result.payload["address"] == "Rua A"
    assert result.payload["luminaria_defeito"] == "apagada"


@pytest.mark.asyncio
async def test_reparo_workflow_collect_luminaria_details_complete_group_flow():
    workflow = make_workflow()
    state = make_state(
        payload={
            "luminaria_defeito": "apagada",
            "luminaria_quantidade": "grupo",
            "luminaria_intercaladas_bloco": "2",
            "luminaria_localizacao": "praça",
        }
    )

    result = await workflow._collect_luminaria_details(state)

    assert result.agent_response is None
    assert result.data["luminaria_defeito"] == "Apagada"
    assert result.data["luminaria_quantidade"] == "grupo"
    assert result.data["luminaria_intercaladas_bloco"] == "intercaladas"
    assert result.data["luminaria_defeito_classificado"] == (
        "Várias luminárias intercaladas apagadas"
    )
    assert result.data["luminaria_localizacao"] == "Praça"


@pytest.mark.asyncio
async def test_reparo_workflow_collect_luminaria_details_prompts_next_fields():
    workflow = make_workflow()

    no_defect = await workflow._collect_luminaria_details(make_state())
    assert "opções fechadas" in no_defect.agent_response.description

    invalid = await workflow._collect_luminaria_details(
        make_state(payload={"luminaria_defeito": "outro"})
    )
    assert "Não entendi o defeito" in invalid.agent_response.description

    quantity = await workflow._collect_luminaria_details(
        make_state(payload={"luminaria_defeito": "acesa de dia"})
    )
    assert "grupo de luminárias" in quantity.agent_response.description

    intercaladas = await workflow._collect_luminaria_details(
        make_state(
            data={"luminaria_defeito": "Piscando", "luminaria_quantidade": "grupo"}
        )
    )
    assert "grupo de luminárias" in intercaladas.agent_response.description

    location = await workflow._collect_luminaria_details(
        make_state(
            data={
                "luminaria_defeito": "Danificada",
                "luminaria_defeito_classificado": "Danificada",
            }
        )
    )
    assert "Onde está localizada" in location.agent_response.description


@pytest.mark.asyncio
async def test_reparo_workflow_collect_address_reference_and_confirmation():
    workflow = make_workflow()
    state = make_state(payload={"address": "Rua A, 10"})

    result = await workflow._collect_address(state)

    assert result.agent_response is None
    assert result.data["address_needs_confirmation"] is True
    assert result.data["address_temp"]["logradouro"] == "Rua Teste"

    result.payload = {"confirmacao": True}
    result = await workflow._confirm_address(result)

    assert result.data["address_confirmed"] is True
    # reference_point_required=True (igual POC2, decisão de produto 2026-06-05) →
    # pergunta o ponto de referência após confirmar o endereço.
    assert result.data["need_reference_point"] is True

    # _collect_reference_point pergunta; com a resposta do cidadão, coleta.
    result.payload = {}
    result = await workflow._collect_reference_point(result)
    assert result.agent_response is not None
    assert "ponto de referência" in result.agent_response.description.lower()
    assert not result.data.get("reference_point_collected")

    result.payload = {"ponto_referencia": "Perto da praça"}
    result = await workflow._collect_reference_point(result)
    assert result.data["reference_point_collected"] is True
    assert result.data["ponto_referencia"] == "Perto da praça"


@pytest.mark.asyncio
async def test_endereco_confirmado_nao_reconfirma_em_turno_sem_payload():
    """Regressão (bug real 2026-06-03): após confirmar o endereço e iniciar o
    gov.br, o auto-resume do callback reinvoca o workflow com payload VAZIO. O
    endereço já confirmado na sessão viva NÃO pode cair na re-pergunta de memória
    (endereco_historico), pedindo confirmação do MESMO endereço uma 2ª vez.
    _collect_address deve curto-circuitar e não emitir nada."""
    workflow = make_workflow()
    state = make_state(
        # Turno de auto-resume pós-gov.br: chega sem payload.
        payload={},
        data={
            "address": {
                "logradouro": "Rua Guilhermina Guinle",
                "numero": "170",
                "bairro": "Botafogo",
                "cidade": "Rio de Janeiro",
                "estado": "RJ",
            },
            "address_validated": True,
            "address_confirmed": True,
            "govbr_auth_sent": True,
        },
    )

    result = await workflow._collect_address(state)

    # Curto-circuito: nada é perguntado de novo.
    assert result.agent_response is None
    # A re-pergunta de memória NÃO foi acionada.
    assert not result.data.get("awaiting_address_memory_confirmation")
    # O endereço confirmado é preservado intacto.
    assert result.data["address_confirmed"] is True
    assert result.data["address"]["numero"] == "170"


def test_handle_address_from_memory_ignora_endereco_ja_confirmado():
    """Defesa em profundidade (Edit 2): mesmo chamado diretamente, a re-pergunta
    de memória não dispara para um endereço já confirmado na sessão viva — ainda
    que o curto-circuito de _collect_address fosse contornado no futuro."""
    workflow = make_workflow()
    state = make_state(
        payload={},
        data={
            "address": {"logradouro": "Rua A", "numero": "10", "bairro": "Centro"},
            "address_confirmed": True,
            "address_validated": True,
        },
    )

    handled = workflow._handle_address_from_memory(state)

    assert handled is False
    assert not state.data.get("awaiting_address_memory_confirmation")
    assert state.agent_response is None


def test_handle_address_from_memory_reconfirma_endereco_de_atendimento_anterior():
    """Controle: o caso legítimo de memória (endereço de atendimento anterior, SEM
    confirmação na sessão viva) AINDA dispara a re-pergunta endereco_historico."""
    workflow = make_workflow()
    state = make_state(
        payload={},
        data={"address": {"logradouro": "Rua A", "numero": "10", "bairro": "Centro"}},
    )

    handled = workflow._handle_address_from_memory(state)

    assert handled is True
    assert state.data.get("awaiting_address_memory_confirmation") is True
    assert state.agent_response is not None
    assert "histórico" in state.agent_response.description


@pytest.mark.asyncio
async def test_reparo_reference_point_correction_reactivates_collection():
    """Uma correção explícita ('corrigir ponto de referência') reativa a coleta
    do ponto de referência (independente do reference_point_required)."""
    workflow = make_workflow()
    state = make_state(data={"correction_requested": "reference_point"})

    asked = await workflow._collect_reference_point(state)
    assert asked.data["need_reference_point"] is True
    assert "ponto de referência" in asked.agent_response.description.lower()

    asked.payload = {"ponto_referencia": "Ao lado da escola"}
    collected = await workflow._collect_reference_point(asked)
    assert collected.data["reference_point_collected"] is True
    assert collected.data["ponto_referencia"] == "Ao lado da escola"


@pytest.mark.asyncio
async def test_reparo_workflow_collect_quadra_and_format_confirmation():
    workflow = make_workflow()
    state = make_state(
        data={
            "luminaria_defeito_classificado": "Apagada",
            "luminaria_localizacao": "Praça",
            "address": {
                "logradouro_nome_ipp": "Praça XV",
                "numero": "1",
                "bairro_nome_ipp": "Centro",
            },
            "name": "Maria Silva",
            "cpf": "12345678909",
            "email": "maria@example.com",
            "phone": "21999999999",
        }
    )

    question = await workflow._collect_quadra_esportes(state)
    assert "quadra de esportes" in question.agent_response.description

    question.payload = {"reparo_luminaria_quadra_esportes": False}
    answered = await workflow._collect_quadra_esportes(question)
    assert answered.data["reparo_luminaria_quadra_esportes"] is False

    formatted = workflow._format_ticket_confirmation_data(answered)
    assert "Reparo de luminária" in formatted
    assert "Maria Silva" in formatted
    # CPF deve estar mascarado
    assert "123.•••.•••-09" in formatted
    assert "123.456.789-09" not in formatted  # Não deve mostrar CPF completo
    # Email deve estar mascarado
    assert "ma•••@example.com" in formatted
    # Telefone deve estar mascarado
    assert "(21) 9••••-9999" in formatted


@pytest.mark.asyncio
async def test_reparo_workflow_confirm_ticket_data_and_corrections():
    workflow = make_workflow()
    base_data = {
        "luminaria_defeito": "Apagada",
        "luminaria_quantidade": "uma",
        "luminaria_defeito_classificado": "Apagada",
        "luminaria_localizacao": "Rua",
        "address": {"logradouro": "Rua A", "bairro": "Centro"},
    }

    initial = await workflow._confirm_ticket_data(make_state(data=base_data.copy()))
    assert "confirme os dados" in initial.agent_response.description

    confirmed = await workflow._confirm_ticket_data(
        make_state(payload={"confirmacao": True}, data=base_data.copy())
    )
    assert confirmed.data["ticket_data_confirmed"] is True

    correction = await workflow._confirm_ticket_data(
        make_state(
            payload={"confirmacao": False, "correcao": "corrigir email"},
            data=base_data.copy(),
        )
    )
    assert correction.data["correction_requested"] == "email"
    assert "email correto" in correction.agent_response.description

    missing = await workflow._confirm_ticket_data(
        make_state(payload={"confirmacao": False}, data=base_data.copy())
    )
    assert "precisa ser corrigido" in missing.agent_response.description


@pytest.mark.asyncio
async def test_reparo_workflow_identification_steps():
    workflow = make_workflow()

    cpf = await workflow._collect_cpf(
        make_state(payload={"cpf": "123.456.789-09"}, data={"address_confirmed": True})
    )
    assert cpf.data["cpf"] == "12345678909"

    skipped_cpf = await workflow._collect_cpf(
        make_state(payload={"cpf": ""}, data={"address_confirmed": True})
    )
    assert skipped_cpf.data["identificacao_pulada"] is True

    email = await workflow._collect_email(make_state(payload={"email": "A@B.COM"}))
    assert email.data["email"] == "a@b.com"
    assert email.data["email_processed"] is True

    skipped_email = await workflow._collect_email(make_state(payload={"email": ""}))
    assert skipped_email.data["email_skipped"] is True

    name = await workflow._collect_name(make_state(payload={"name": "joao silva"}))
    assert name.data["name"] == "Joao Silva"
    assert name.data["name_processed"] is True

    skipped_name = await workflow._collect_name(make_state(payload={"name": ""}))
    assert skipped_name.data["name_skipped"] is True


def test_reparo_workflow_specific_attributes_and_routes():
    workflow = make_workflow()
    state = make_state(
        data={
            "luminaria_defeito_classificado": "Apagada",
            "luminaria_localizacao": "Praça",
            "address": {"logradouro": "Praça Mauá"},
        }
    )

    attrs = workflow.build_specific_attributes(state)

    assert attrs["defeitoLuminaria"] == "Apagada"
    assert attrs["estaNaPraca"] == "1"
    assert attrs["localizacaoLuminaria"] == "Praça"
    assert workflow._esta_na_praca(state) is True
    assert workflow._nome_praca(state) == "Praça Mauá"

    assert workflow._route_after_luminaria_details(make_state()) == "collect_address"
    assert workflow._route_after_quadra(make_state()) == "collect_reference_point"
    # Após o ponto de referência o fluxo passa pela escolha de método de
    # identificação (gov.br vs CPF) antes do CPF — nó introduzido com a auth
    # gov.br. O routing map de _route_after_reference só aceita
    # "select_identification_method"/END (ver workflow.py), então o destino
    # correto aqui é select_identification_method, não collect_cpf.
    assert (
        workflow._route_after_reference(make_state()) == "select_identification_method"
    )
    # open_ticket exige endereço validado+confirmado (guard POC1 #295-A); o caso
    # anômalo sem endereço é coberto em
    # test_route_after_ticket_confirmation_requires_confirmed_address.
    assert (
        workflow._route_after_ticket_confirmation(
            make_state(
                data={
                    "ticket_data_confirmed": True,
                    "address_validated": True,
                    "address_confirmed": True,
                }
            )
        )
        == "open_ticket"
    )

    for correction, route in {
        "defect": "collect_luminaria_details",
        "quantity": "collect_luminaria_details",
        "intercaladas_bloco": "collect_luminaria_details",
        "location": "collect_luminaria_details",
        "address": "collect_address",
        "reference_point": "collect_reference_point",
        "cpf": "collect_cpf",
        "email": "collect_email",
        "name": "collect_name",
    }.items():
        assert (
            workflow._route_after_ticket_confirmation(
                make_state(data={"correction_requested": correction})
            )
            == route
        )


# --------------------------------------------------------------------------- #
# Prefill seed (2026-06-03): a extração inicial do agente (1ª msg) sobrevive
# até o auto-send do Flow pós-confirmação. Bug: entidades normalizadas em
# state.payload mas perdidas antes de should_send_flow (form abria vazio).
# --------------------------------------------------------------------------- #
def _build_prefill_from_seed(state_data):
    """Replica a construção de prefill_from_state em app.py should_send_flow."""
    prefill = {}
    seed = state_data.get("flow_prefill_seed") or {}
    for src_key in (
        "luminaria_defeito",
        "luminaria_localizacao",
        "luminaria_quantidade",
        "luminaria_intercaladas_bloco",
        "defect_type",
        "location",
        "qty_pattern",
    ):
        val = state_data.get(src_key) or seed.get(src_key)
        if val:
            prefill[src_key] = val
    return prefill


def test_initialize_captures_prefill_seed_without_tripping_defect_guard():
    """Entidades da 1ª msg vão pro flow_prefill_seed, NÃO pro luminaria_defeito
    (que suprimiria o Flow via ja_tem_dados_defeito)."""
    workflow = make_workflow()
    state = make_state(
        payload={
            "luminaria_defeito": "Apagada",
            "luminaria_localizacao": "Calçada",
            "luminaria_quantidade": "uma",
        },
        data={"knowledge_loaded": True},  # evita hub_search (rede)
    )
    asyncio.run(workflow._initialize_workflow(state))
    seed = state.data.get("flow_prefill_seed")
    assert seed == {
        "luminaria_defeito": "Apagada",
        "luminaria_localizacao": "Calçada",
        "luminaria_quantidade": "uma",
    }
    # guard NÃO disparado: luminaria_defeito não vaza pro state.data
    assert "luminaria_defeito" not in {
        k: v for k, v in state.data.items() if k != "flow_prefill_seed"
    }


def test_initialize_seed_captures_flow_canonical_names():
    """defect_type/location/qty_pattern (nomes do Flow): defect/location
    aliasados p/ luminaria_*, qty_pattern capturado cru — o normalizer resolve
    tudo no envio."""
    workflow = make_workflow()
    state = make_state(
        payload={"defect_type": "Apagada", "location": "Rua", "qty_pattern": "uma"},
        data={"knowledge_loaded": True},
    )
    asyncio.run(workflow._initialize_workflow(state))
    seed = state.data.get("flow_prefill_seed") or {}
    assert seed.get("luminaria_defeito") == "Apagada"
    assert seed.get("luminaria_localizacao") == "Rua"
    assert seed.get("qty_pattern") == "uma"
    normalized = normalize_prefill_for_flow(
        "reparo_luminaria", _build_prefill_from_seed(state.data)
    )
    assert normalized == {
        "defect_type": "Apagada",
        "location": "Rua",
        "qty_pattern": "uma",
    }


def test_seed_survives_confirmation_turn():
    """Turn 2 (confirmacao_servico, sem entidades) NÃO apaga o seed do turn 1."""
    workflow = make_workflow()
    state = make_state(
        payload={"confirmacao_servico": True},
        data={
            "knowledge_loaded": True,
            "flow_prefill_seed": {"luminaria_defeito": "Apagada"},
        },
    )
    asyncio.run(workflow._initialize_workflow(state))
    assert state.data["flow_prefill_seed"] == {"luminaria_defeito": "Apagada"}


def test_prefill_seed_end_to_end_to_flow_init():
    """Seed → prefill_from_state (app.py) → normalize → encode → _handle_init:
    o formulário abre preenchido."""
    seed_state = {
        "flow_prefill_seed": {
            "luminaria_defeito": "Apagada",
            "luminaria_localizacao": "Calçada",
            "luminaria_quantidade": "uma",
        }
    }
    raw = _build_prefill_from_seed(seed_state)
    normalized = normalize_prefill_for_flow("reparo_luminaria", raw)
    assert normalized == {
        "defect_type": "Apagada",
        "location": "Calçada",
        "qty_pattern": "uma",
    }
    token = encode_flow_token("uuid-x", normalized)
    assert token.startswith("v1:")
    data = _handle_init(flow_token=token)["data"]
    assert data["defect_type_prefill"] == "Apagada"
    assert data["location_prefill"] == "Calçada"
    assert data["qty_pattern_prefill"] == "uma"


def test_prefill_seed_grupo_bloco_qty():
    """quantidade='grupo' + intercaladas='bloco' → qty_pattern='bloco' no Flow."""
    seed_state = {
        "flow_prefill_seed": {
            "luminaria_defeito": "Danificada",
            "luminaria_quantidade": "grupo",
            "luminaria_intercaladas_bloco": "bloco",
        }
    }
    normalized = normalize_prefill_for_flow(
        "reparo_luminaria", _build_prefill_from_seed(seed_state)
    )
    assert normalized.get("qty_pattern") == "bloco"
    assert normalized.get("defect_type") == "Danificada"


def test_qty_pattern_normalized_in_text_path_not_only_flow():
    """#283: qty_pattern fora do Flow (caminho TEXTO) também vira
    luminaria_quantidade — antes só normalizava com _source=whatsapp_flow,
    então a coleta por texto re-perguntava a quantidade já informada."""
    workflow = make_workflow()

    text_uma = make_state(payload={"qty_pattern": "uma"})
    workflow._normalize_payload_aliases(text_uma)
    assert text_uma.payload.get("luminaria_quantidade") == "uma"

    text_bloco = make_state(payload={"qty_pattern": "bloco"})
    workflow._normalize_payload_aliases(text_bloco)
    assert text_bloco.payload.get("luminaria_quantidade") == "grupo"
    assert text_bloco.payload.get("luminaria_intercaladas_bloco") == "bloco"

    # is_quadra_esportes continua RESTRITO ao Flow (radio): no texto não age.
    text_quadra = make_state(payload={"is_quadra_esportes": "nao"})
    workflow._normalize_payload_aliases(text_quadra)
    assert "reparo_luminaria_quadra_esportes" not in text_quadra.data

    # Caminho Flow segue intacto.
    flow_state = make_state(
        payload={"_source": "whatsapp_flow", "qty_pattern": "intercaladas"}
    )
    workflow._normalize_payload_aliases(flow_state)
    assert flow_state.payload.get("luminaria_quantidade") == "grupo"
    assert flow_state.payload.get("luminaria_intercaladas_bloco") == "intercaladas"


def test_route_after_ticket_confirmation_requires_confirmed_address():
    """POC1 #295-A: nunca abrir chamado sem endereço validado+confirmado.
    No happy path o endereço já está confirmado (abre); no estado anômalo do QA
    (dados confirmados sem endereço) o guard devolve pra coleta."""
    workflow = make_workflow()

    anomalo = make_state(data={"ticket_data_confirmed": True})
    assert workflow._route_after_ticket_confirmation(anomalo) == "collect_address"
    # Limpa o flag pra a re-rota não cair em loop.
    assert "ticket_data_confirmed" not in anomalo.data

    parcial = make_state(
        data={"ticket_data_confirmed": True, "address_validated": True}
    )
    assert workflow._route_after_ticket_confirmation(parcial) == "collect_address"

    ok = make_state(
        data={
            "ticket_data_confirmed": True,
            "address_validated": True,
            "address_confirmed": True,
        }
    )
    assert workflow._route_after_ticket_confirmation(ok) == "open_ticket"


def test_flow_quadra_esportes_nao_persists_in_data_not_payload():
    """Praça + is_quadra_esportes='nao' do Flow: a resposta é gravada em
    state.data (persiste entre turnos), NÃO no payload efêmero — senão sumiria
    antes do passo da quadra (que roda num turno posterior)."""
    workflow = make_workflow()
    state = make_state(
        payload={
            "_source": "whatsapp_flow",
            "location": "Praça",
            "is_quadra_esportes": "nao",
        },
        data={"knowledge_loaded": True},
    )
    asyncio.run(workflow._initialize_workflow(state))
    assert state.data.get("reparo_luminaria_quadra_esportes") is False
    assert state.data.get("reparo_luminaria_endereco_especial_executado") is True


def test_flow_quadra_esportes_nao_not_reasked_next_turn():
    """Bug real (cross-turn): com a resposta em state.data, o passo da quadra
    num turno POSTERIOR (payload já limpo, location=Praça) NÃO re-pergunta."""
    workflow = make_workflow()
    # estado já persistido do turno do Flow (data sobrevive; payload é novo)
    state = make_state(
        payload={},  # turno posterior: payload limpo (base_workflow reseta)
        data={
            "knowledge_loaded": True,
            "luminaria_localizacao": "Praça",
            "reparo_luminaria_quadra_esportes": False,
            "reparo_luminaria_endereco_especial_executado": True,
        },
    )
    asyncio.run(workflow._collect_quadra_esportes(state))
    assert state.agent_response is None  # sem re-pergunta de texto


def test_flow_quadra_esportes_sim_overrides_location():
    """is_quadra_esportes='sim' continua sobrescrevendo location."""
    workflow = make_workflow()
    state = make_state(
        payload={
            "_source": "whatsapp_flow",
            "location": "Praça",
            "is_quadra_esportes": "sim",
        },
        data={"knowledge_loaded": True},
    )
    asyncio.run(workflow._initialize_workflow(state))
    assert state.payload.get("location") == "Quadra de esportes"


@pytest.mark.asyncio
async def test_optional_identification_explicit_skip_goes_anonymous():
    """Identificação opcional + recusa explícita NÃO cai no loop de 3 tentativas
    inválidas → vira anônimo direto (achado 2026-06-03). Inclui as FRASES naturais
    que o agente repassa (achado 2026-06-04: o match exato falhava em "quero
    continuar sem me identificar" → erro "escolha CPF ou Gov.br"; agora há match por
    substring). Cobre o _select_identification_method (mixin compartilhado)."""
    workflow = make_workflow()
    for token in [
        # tokens curtos (match exato)
        "anonimo",
        "pular",
        "nao quero",
        "não",
        "seguir sem",
        # frases naturais (match por substring — fix 2026-06-04)
        "quero continuar sem me identificar",
        "continuar sem me identificar",
        "prefiro não me identificar",
        "nao me identificar",
        "quero seguir sem cpf",
    ]:
        state = make_state(
            payload={"identification_method": token},
            data={"identificacao_obrigatoria_1746": False},
        )
        out = await workflow._select_identification_method(state)
        assert out.data["identification_method"] == "anonimo", token
        assert out.data.get("identificacao_recusada") is True
        assert out.agent_response is None  # sem re-pergunta nem erro


@pytest.mark.asyncio
async def test_optional_identification_valid_method_preserved():
    """cpf/govbr explícito continua aceito (não confundido com skip)."""
    workflow = make_workflow()
    state = make_state(
        payload={"identification_method": "cpf"},
        data={"identificacao_obrigatoria_1746": False},
    )
    out = await workflow._select_identification_method(state)
    assert out.data["identification_method"] == "cpf"


@pytest.mark.asyncio
async def test_identification_prompt_sets_3_buttons_when_optional():
    """Prompt do método (opcional) sinaliza 3 botões: CPF / Gov.br / Sem me
    identificar. Alcançado com payload não-vazio SEM identification_method (payload
    vazio + opcional auto-anonimiza antes do prompt). Títulos mapeiam pros valores."""
    workflow = make_workflow()
    state = make_state(
        payload={"_outro": "x"},
        data={"identificacao_obrigatoria_1746": False},
    )
    out = await workflow._select_identification_method(state)
    interactive = out.agent_response.interactive
    assert interactive["field"] == "identification_method"
    assert [b["id"] for b in interactive["buttons"]] == ["cpf", "govbr", "anonimo"]
    assert interactive["body"] == out.agent_response.description


@pytest.mark.asyncio
async def test_identification_prompt_sets_2_buttons_when_obligatory():
    """Identificação obrigatória → só CPF / Gov.br (sem a 3ª opção anônima)."""
    workflow = make_workflow()
    state = make_state(payload={}, data={"identificacao_obrigatoria_1746": True})
    out = await workflow._select_identification_method(state)
    interactive = out.agent_response.interactive
    assert [b["id"] for b in interactive["buttons"]] == ["cpf", "govbr"]


@pytest.mark.asyncio
async def test_identification_invalid_reprompt_keeps_buttons():
    """Re-prompt de método inválido (tentativa < 3) também traz os botões."""
    workflow = make_workflow()
    state = make_state(
        payload={"identification_method": "xyz"},
        data={"identificacao_obrigatoria_1746": True},
    )
    out = await workflow._select_identification_method(state)
    interactive = out.agent_response.interactive
    assert interactive is not None
    assert interactive["field"] == "identification_method"


@pytest.mark.asyncio
async def test_collect_quadra_sets_sim_nao_buttons():
    """A pergunta da quadra de esportes sinaliza botões Sim/Não (field do
    QuadraEsportesPayload; parse_affirmation resolve o tap)."""
    workflow = make_workflow()
    state = make_state(data={"luminaria_localizacao": "Praça"})
    out = await workflow._collect_quadra_esportes(state)
    interactive = out.agent_response.interactive
    assert interactive["field"] == "reparo_luminaria_quadra_esportes"
    assert [b["id"] for b in interactive["buttons"]] == ["sim", "nao"]
    assert interactive["body"] == out.agent_response.description


def test_metodo_invalido_mantem_opcao_de_pular_quando_opcional():
    """Re-prompt de método inválido MANTÉM a saída anônima quando a identificação
    é opcional (incidente 2026-06-04: o re-prompt removia a opção de pular e o
    cidadão anônimo ficava preso pedindo CPF/Gov.br)."""
    assert "continuar sem identificar" in sgrc_tpl.metodo_identificacao_invalido(
        1, opcional=True
    )
    # Obrigatória: não oferece a saída anônima.
    assert "continuar sem identificar" not in sgrc_tpl.metodo_identificacao_invalido(
        1, opcional=False
    )


# --- Confirmação por botões Sim/Não (camada-tool, ENABLE_INTERACTIVE_CONFIRM) ---


def test_show_service_summary_sets_sim_nao_buttons():
    """A primeira passada por _show_service_summary (sem confirmação no payload)
    sinaliza botões Sim/Não no agent_response.interactive, mantendo o texto como
    fallback."""
    workflow = make_workflow()
    state = (
        make_state()
    )  # fresh: sem service_confirmed, sem _source, sem confirmacao_servico

    result = asyncio.run(workflow._show_service_summary(state))

    interactive = result.agent_response.interactive
    assert interactive is not None, "deveria sinalizar interactive na confirmação"
    buttons = interactive["buttons"]
    assert [b["id"] for b in buttons] == ["sim", "nao"]
    assert [b["title"] for b in buttons] == ["Sim", "Não"]
    # Campo do payload nomeado pro wrapper instruir o agente (evita re-chamada
    # com payload vazio → loop). Tem que casar o campo do ConfirmacaoServicoPayload.
    assert interactive["field"] == "confirmacao_servico"
    # body presente e description (fallback) intactos.
    assert "É este serviço" in interactive["body"]
    assert "É este serviço" in result.agent_response.description


def test_sim_nao_interactive_vira_envelope_valido_e_titulos_mapeiam():
    """Os botões sinalizados formam um envelope Meta válido e seus títulos
    (o que volta como texto no tap) mapeiam de volta determinístico via
    parse_affirmation — o ganho central sobre o texto livre."""
    from src.tools.whatsapp_interactive import build_buttons_envelope
    from src.tools.multi_step_service.workflows.sgrc_components.models import (
        parse_affirmation,
    )

    workflow = make_workflow()
    spec = asyncio.run(
        workflow._show_service_summary(make_state())
    ).agent_response.interactive

    env = build_buttons_envelope(body=spec["body"], buttons=spec["buttons"])
    assert env["status"] == "ok", env.get("error")
    assert env["interactive"]["type"] == "button"
    # tap → title volta como texto → parse_affirmation resolve sem fuzzy.
    assert parse_affirmation("Sim") is True
    assert parse_affirmation("Não") is False


def test_agent_response_interactive_no_model_dump():
    """Contrato que o app.py consome: o campo `interactive` sobrevive ao
    model_dump (mss retorna response.model_dump() e o app.py faz
    response.pop('interactive')), e é None por default nos serviços que não
    setam (back-compat — o pop devolve None e o caminho de texto segue)."""
    from src.tools.multi_step_service.core.models import AgentResponse

    spec = {"body": "q", "buttons": [{"id": "sim", "title": "Sim"}]}
    dumped = AgentResponse(description="q", interactive=spec).model_dump()
    assert dumped["interactive"]["buttons"][0]["id"] == "sim"

    # Default None nos que não setam (app.py pop → None → ignora, sem efeito).
    assert AgentResponse(description="x").model_dump()["interactive"] is None


def test_confirm_ticket_data_sets_sim_nao_buttons():
    """A confirmação dos dados do chamado (ponto ALCANÇADO no caminho-feliz
    pós-Flow, diferente de _show_service_summary que o Flow-first pula) sinaliza
    botões Sim/Não, com o texto como fallback. O `body` espelha o resumo."""
    workflow = make_workflow()
    state = (
        make_state()
    )  # fresh: sem ticket_data_confirmed, payload vazio → pergunta primária

    result = asyncio.run(workflow._confirm_ticket_data(state))

    interactive = result.agent_response.interactive
    assert interactive is not None, (
        "deveria sinalizar interactive na confirmação de dados"
    )
    buttons = interactive["buttons"]
    assert [b["id"] for b in buttons] == ["sim", "nao"]
    assert [b["title"] for b in buttons] == ["Sim", "Não"]
    # Campo do payload nomeado pro wrapper instruir o agente (sem isso o modelo
    # re-chama com payload vazio e o passo loopa). Tem que casar o campo do
    # TicketDataConfirmationPayload que o parse_optional_bool valida.
    assert interactive["field"] == "confirmacao"
    # body == description (o resumo dos dados do chamado), fallback intacto.
    assert interactive["body"] == result.agent_response.description
