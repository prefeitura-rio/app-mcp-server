import asyncio

import pytest

from src.tools.luminaria_entity_extractor import encode_flow_token
from src.tools.luminaria_flow import _handle_init
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
    assert "processar o endereço" in rlu_tpl.endereco_erro_processamento(1, 3)
    assert "3 tentativas" in rlu_tpl.endereco_maximo_tentativas()
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
    assert address.locality == "Perto da praça"
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
    # reference_point_required=False → não força ponto de referência (−1 turno).
    assert result.data["need_reference_point"] is False

    # _collect_reference_point passa direto, sem perguntar nada.
    result.payload = {}
    result = await workflow._collect_reference_point(result)
    assert result.agent_response is None
    assert not result.data.get("reference_point_collected")


@pytest.mark.asyncio
async def test_reparo_reference_point_correction_reactivates_collection():
    """reference_point_required=False remove a pergunta forçada, mas uma
    correção explícita ('corrigir ponto de referência') reativa a coleta."""
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
    assert (
        workflow._route_after_ticket_confirmation(
            make_state(data={"ticket_data_confirmed": True})
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
