"""
Testes da DEV-360: os passos da poda de árvore depois do endereço não mandam
texto livre nem dado pessoal do cidadão para o log.

Antes, `workflow.py` logava o ponto de referência, o nome, o texto da correção,
o CPF e o e-mail coletados, o `str(e)` dos erros de validação (que o Pydantic
monta com o valor digitado, `input_value=...`) e dos erros da consulta de
cadastro, o `agent_response` inteiro no roteamento (a confirmação traz
endereço, nome e e-mail) e o CPF do estado. A redação global por padrão não
alcança texto sem formato nem rótulo (CHATR-167), e CPF e e-mail não devem
depender só dela.

Invariantes cobertos aqui:
- nenhuma linha de log dos passos contém trecho dos textos sintéticos (ponto
  de referência, nome, correção, CPF, e-mail) nem do conteúdo do Typesense;
- o sucesso registra só metadado (`resumir_texto_livre`, coletado sim, a
  presença do valor); o erro registra só o tipo da exceção;
- o fluxo dos passos não muda: o dado segue no estado e a mensagem de erro da
  resposta ao agente continua a mesma.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from src.utils.pii import resumir_texto_livre


PROJECT_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_PATH = "src/tools/multi_step_service/workflows/poda_de_arvore/workflow.py"
PODA_DIR = PROJECT_ROOT / "src" / "tools" / "multi_step_service" / "workflows"


def _cpf_sintetico(base: str = "123456789") -> str:
    """CPF com dígitos verificadores válidos a partir de uma base inventada."""
    digitos = [int(d) for d in base]
    for peso_inicial in (10, 11):
        soma = sum(d * (peso_inicial - i) for i, d in enumerate(digitos))
        digitos.append((soma * 10 % 11) % 10)
    return "".join(str(d) for d in digitos)


# Dados sintéticos.
PONTO_REFERENCIA = "Em frente a padaria Quimera Azul do Larguinho"
NOME = "Zoraide Ficticia Quarzo"
CORRECAO = "o nome esta errado, o certo e Ermelinda Sintetica Brumal"
CPF = _cpf_sintetico()
EMAIL = "zoraide.quarzo@exemplo-sintetico.test"
EMAIL_INVALIDO = "zoraide.quarzo@@invalido"
ENDERECO = "Rua Sintetica dos Ipes 4321"
CONHECIMENTO = {"id": "svc", "descricao": "Texto Nebuloso Sigiloso do Typesense"}

TRECHOS_PROIBIDOS = (
    "Quimera",
    "Azul",
    "padaria",
    "Larguinho",
    "Zoraide",
    "Ficticia",
    "Quarzo",
    "zoraide",
    "quarzo",
    "Ermelinda",
    "Brumal",
    "o certo e",
    CPF,
    CPF[:9],
    "exemplo-sintetico",
    "invalido",
    "Ipes",
    "4321",
    "Nebuloso",
    "Sigiloso",
)


class _LoggerCapturado:
    """Guarda toda chamada de log, em qualquer nível, com os argumentos."""

    def __init__(self):
        self.registros = []

    def __getattr__(self, nivel):
        def _registrar(msg, *args, **kwargs):
            self.registros.append((nivel, str(msg), repr(args), repr(kwargs)))

        return _registrar

    def texto(self) -> str:
        return "\n".join(" ".join(r) for r in self.registros)

    def mensagens(self, nivel: str) -> list:
        return [r[1] for r in self.registros if r[0] == nivel]


def _ensure_package(name: str, path: Path) -> None:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg


def _load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def service_models():
    return sys.modules["src.tools.multi_step_service.core.models"]


@pytest.fixture
def poda(monkeypatch):
    """Carrega o `workflow.py` da poda com as integrações externas stubadas."""
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    _ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    _ensure_package(
        "src.tools.multi_step_service",
        PROJECT_ROOT / "src" / "tools" / "multi_step_service",
    )
    _ensure_package(
        "src.tools.multi_step_service.core",
        PROJECT_ROOT / "src" / "tools" / "multi_step_service" / "core",
    )
    _ensure_package("src.tools.multi_step_service.workflows", PODA_DIR)
    _ensure_package(
        "src.tools.multi_step_service.workflows.poda_de_arvore.api",
        PODA_DIR / "poda_de_arvore" / "api",
    )

    monkeypatch.setitem(
        sys.modules, "src.config.env", types.SimpleNamespace(PODA_SERVICE_ID="svc")
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.core.base_workflow",
        types.SimpleNamespace(
            BaseWorkflow=type("BaseWorkflow", (), {"__init__": lambda self: None}),
            handle_errors=lambda func: func,
        ),
    )
    templates = _load_module(
        "src.tools.multi_step_service.workflows.poda_de_arvore.templates",
        "src/tools/multi_step_service/workflows/poda_de_arvore/templates.py",
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.poda_de_arvore",
        types.SimpleNamespace(templates=templates),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.poda_de_arvore.integrations",
        types.SimpleNamespace(build_ticket_payload=lambda *a, **k: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.workflows.poda_de_arvore.api.api_service",
        types.SimpleNamespace(
            SGRCAPIService=lambda: types.SimpleNamespace(),
            AddressAPIService=lambda: types.SimpleNamespace(),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.typesense_api",
        types.SimpleNamespace(
            HubSearchRequest=lambda **kwargs: types.SimpleNamespace(**kwargs),
            hub_search_by_id=lambda request: None,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "prefeitura_rio.integrations.sgrc",
        types.SimpleNamespace(async_new_ticket=lambda **kwargs: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "prefeitura_rio.integrations.sgrc.models",
        types.SimpleNamespace(Address=object, Requester=object),
    )
    monkeypatch.setitem(
        sys.modules,
        "prefeitura_rio.integrations.sgrc.exceptions",
        types.SimpleNamespace(
            SGRCBusinessRuleException=Exception,
            SGRCInvalidBodyException=Exception,
            SGRCMalformedBodyException=Exception,
            SGRCDuplicateTicketException=Exception,
            SGRCEquivalentTicketException=Exception,
            SGRCInternalErrorException=Exception,
        ),
    )

    modulo = _load_module("test_poda_workflow_dev360", WORKFLOW_PATH)
    capturado = _LoggerCapturado()
    monkeypatch.setattr(modulo, "logger", capturado)
    return modulo, capturado


def _estado(service_models, payload=None, data=None):
    return service_models.ServiceState(
        user_id="u-sintetico",
        service_name="poda_de_arvore",
        payload=payload or {},
        data=data or {},
    )


def _sem_texto_livre(texto: str) -> None:
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in texto, f"texto do cidadão no log: {trecho!r}"


# ---------------------------------------------------------------------------
# Ponto de referência
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ponto_de_referencia_loga_so_metadado(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)
    estado = _estado(
        service_models,
        payload={"ponto_referencia": PONTO_REFERENCIA},
        data={"need_reference_point": True},
    )

    resultado = await workflow._collect_reference_point(estado)

    assert resultado.agent_response is None
    assert resultado.data["ponto_referencia"] == PONTO_REFERENCIA
    assert resultado.data["reference_point_collected"] is True

    log = capturado.texto()
    _sem_texto_livre(log)
    assert (
        f"Ponto de referência coletado: sim, {resumir_texto_livre(PONTO_REFERENCIA)}"
        in log
    )


@pytest.mark.asyncio
async def test_erro_no_ponto_de_referencia_loga_so_o_tipo(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)
    # Valor que não é texto: o ValidationError do Pydantic cita o input.
    estado = _estado(
        service_models,
        payload={"ponto_referencia": {"texto": PONTO_REFERENCIA}},
        data={"need_reference_point": True},
    )

    resultado = await workflow._collect_reference_point(estado)

    assert resultado.data["ponto_referencia"] is None
    assert resultado.data["reference_point_collected"] is True

    _sem_texto_livre(capturado.texto())
    assert capturado.mensagens("warning") == [
        "Erro ao processar ponto de referência: ValidationError"
    ]


# ---------------------------------------------------------------------------
# Nome, e-mail e CPF
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nome_loga_so_metadado(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)
    estado = _estado(service_models, payload={"name": NOME})

    resultado = await workflow._collect_name(estado)

    assert resultado.agent_response is None
    assert resultado.data["name"] == NOME
    assert resultado.data["name_processed"] is True

    log = capturado.texto()
    _sem_texto_livre(log)
    assert f"Nome coletado: sim, {resumir_texto_livre(NOME)}" in log


@pytest.mark.asyncio
async def test_email_coletado_nao_vai_ao_log(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)
    estado = _estado(service_models, payload={"email": EMAIL})

    resultado = await workflow._collect_email(estado)

    assert resultado.agent_response is None
    assert resultado.data["email"] == EMAIL

    log = capturado.texto()
    _sem_texto_livre(log)
    assert "Email coletado: sim" in log


@pytest.mark.asyncio
async def test_email_invalido_loga_so_o_tipo_e_resposta_nao_muda(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)
    estado = _estado(service_models, payload={"email": EMAIL_INVALIDO})

    resultado = await workflow._collect_email(estado)

    assert resultado.data["email_attempts"] == 1
    assert "Email inválido" in resultado.agent_response.error_message

    log = capturado.texto()
    _sem_texto_livre(log)
    assert "[EMAIL] Tentativa 1/3 - Erro: ValidationError" in log


@pytest.mark.asyncio
async def test_cpf_coletado_nao_vai_ao_log(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)
    estado = _estado(service_models, payload={"cpf": CPF})

    resultado = await workflow._collect_cpf(estado)

    assert resultado.agent_response is None
    assert resultado.data["cpf"] == CPF

    log = capturado.texto()
    _sem_texto_livre(log)
    assert "CPF coletado: sim" in log


@pytest.mark.parametrize(
    ("excecao", "nivel", "esperado"),
    [
        (
            AttributeError(f"resposta sem campo para {CPF}"),
            "error",
            "Erro ao processar resposta da API de cadastro: AttributeError",
        ),
        (
            ConnectionError(f"falhou GET /cadastro/{CPF}"),
            "error",
            "API de cadastro indisponível: ConnectionError",
        ),
        (
            RuntimeError(f"cadastro {CPF} de {NOME} não encontrado"),
            "info",
            "Usuário não encontrado no cadastro ou erro na consulta: RuntimeError",
        ),
    ],
)
@pytest.mark.asyncio
async def test_erro_da_consulta_de_cadastro_loga_so_o_tipo(
    poda, service_models, excecao, nivel, esperado
):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=False)
    consultas = []

    async def get_user_info(cpf):
        consultas.append(cpf)
        raise excecao

    workflow.api_service = types.SimpleNamespace(get_user_info=get_user_info)
    estado = _estado(service_models, payload={"cpf": CPF})

    resultado = await workflow._collect_cpf(estado)

    assert consultas == [CPF]
    assert resultado.data["cpf"] == CPF
    assert resultado.data["cadastro_verificado"] is False

    _sem_texto_livre(capturado.texto())
    assert esperado in capturado.mensagens(nivel)


@pytest.mark.asyncio
async def test_erro_na_confirmacao_de_dados_pessoais_loga_so_o_tipo(
    poda, service_models
):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)
    estado = _estado(
        service_models,
        payload={"confirmacao": f"talvez, sou {NOME}"},
        data={"awaiting_user_memory_confirmation": True, "name": NOME},
    )

    await workflow._collect_cpf(estado)

    _sem_texto_livre(capturado.texto())
    assert capturado.mensagens("error") == [
        "Erro ao processar confirmação de dados pessoais: ValidationError"
    ]


@pytest.mark.asyncio
async def test_erro_na_confirmacao_do_endereco_da_memoria_loga_so_o_tipo(
    poda, service_models
):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)
    estado = _estado(
        service_models,
        payload={"confirmacao": f"talvez, perto da {PONTO_REFERENCIA}"},
        data={"awaiting_address_memory_confirmation": True},
    )

    workflow._handle_address_from_memory(estado)

    _sem_texto_livre(capturado.texto())
    assert capturado.mensagens("error") == [
        "Erro ao processar confirmação de endereço da memória: ValidationError"
    ]


# ---------------------------------------------------------------------------
# Confirmação dos dados do ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correcao_loga_so_metadado_e_fluxo_nao_muda(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)
    estado = _estado(
        service_models,
        payload={"confirmacao": False, "correcao": CORRECAO},
        data={"name": NOME, "name_processed": True},
    )

    resultado = await workflow._confirm_ticket_data(estado)

    assert resultado.data["correction_requested"] == "name"
    assert "name" not in resultado.data
    assert resultado.agent_response is not None

    log = capturado.texto()
    _sem_texto_livre(log)
    assert f"Usuário solicitou correção: {resumir_texto_livre(CORRECAO)}" in log


@pytest.mark.asyncio
async def test_erro_na_confirmacao_do_ticket_loga_so_o_tipo(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)
    estado = _estado(service_models, payload={"confirmacao": f"talvez, {CORRECAO}"})

    resultado = await workflow._confirm_ticket_data(estado)

    # A resposta ao agente continua com o motivo, como antes.
    assert resultado.agent_response.error_message.startswith("Resposta inválida:")

    _sem_texto_livre(capturado.texto())
    assert capturado.mensagens("error") == [
        "Erro ao processar confirmação de dados do ticket: ValidationError"
    ]


# ---------------------------------------------------------------------------
# Roteamento e conhecimento do serviço
# ---------------------------------------------------------------------------


def test_roteamento_apos_endereco_nao_despeja_agent_response(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)
    estado = _estado(service_models)
    estado.agent_response = service_models.AgentResponse(
        description=f"Confirme: {ENDERECO}, {NOME}, {EMAIL}, {PONTO_REFERENCIA}",
        data={"cpf": CPF},
    )

    assert workflow._route_after_address(estado) == modulo.END

    log = capturado.texto()
    _sem_texto_livre(log)
    assert "[STATE] agent_response: True" in log


def test_roteamento_apos_cpf_nao_loga_o_cpf(poda, service_models):
    modulo, capturado = poda
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)
    estado = _estado(
        service_models,
        data={"cpf": CPF, "email_processed": True, "name_processed": True},
    )

    assert workflow._route_after_cpf(estado) == "confirm_ticket_data"

    log = capturado.texto()
    _sem_texto_livre(log)
    assert "[STATE.DATA] cpf: True" in log


@pytest.mark.asyncio
async def test_conhecimento_do_servico_loga_so_as_chaves(poda, monkeypatch):
    modulo, capturado = poda

    async def hub_search_by_id(_request):
        return dict(CONHECIMENTO)

    monkeypatch.setattr(modulo, "hub_search_by_id", hub_search_by_id)
    workflow = modulo.PodaDeArvoreWorkflow(use_fake_api=True)

    await workflow._load_service_knowledge()

    assert workflow.service_knowledge == CONHECIMENTO
    _sem_texto_livre(capturado.texto())
    assert capturado.mensagens("debug") == ["[KNOWLEDGE] Chaves: ['descricao', 'id']"]
