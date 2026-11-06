"""
Testes da DEV-380: a abertura do chamado de poda de árvore no SGRC não pode
falhar por dependência ausente.

`async_new_ticket` (`prefeitura_rio.integrations.sgrc`) é decorada com
`assert_dependencies(["pendulum", "pytz"], extras=["sgrc"])` e usa
`pendulum.now(tz=pytz.timezone("America/Sao_Paulo"))` para a data/hora do
chamado. O `pyproject.toml` declarava `prefeitura-rio` sem o extra `[sgrc]` e
sem `pendulum`, então toda abertura lançava `ImportError` antes de chamar o
SGRC, e o cidadão recebia sempre a mensagem de erro geral.

Invariantes cobertos aqui, com a biblioteca de verdade (só o POST HTTP ao SGRC
é simulado):
- as dependências que o extra `[sgrc]` exige para abrir chamado estão
  instaladas;
- `async_new_ticket` monta o corpo (data/hora no fuso de São Paulo) e chega ao
  POST do SGRC sem `ImportError`;
- o fluxo da poda (`_open_ticket`) abre o chamado e devolve o protocolo;
- SGRC indisponível continua tratado: vira `BaseSGRCException` na biblioteca e
  `erro_geral` no fluxo, sem `ImportError`.
"""

import importlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import pytest

import prefeitura_rio.integrations.sgrc as sgrc
from prefeitura_rio.integrations.sgrc.exceptions import BaseSGRCException
from prefeitura_rio.utils import base_assert_dependencies


WORKFLOW_MODULE = "src.tools.multi_step_service.workflows.poda_de_arvore.workflow"
MODELS_MODULE = "src.tools.multi_step_service.core.models"

# Dados sintéticos.
SGRC_URL_SINTETICA = "https://sgrc.sintetico.invalid"
PROTOCOLO = "PROTO-SINTETICO-1"
CHAMADO = "CHAMADO-SINTETICO-1"
RESPOSTA_SUCESSO = {
    "codigo": 1,
    "descricao": "Chamado criado",
    "protocolo": PROTOCOLO,
    "codigoChamado": CHAMADO,
}
FORMATO_DATA_HORA = "%Y-%m-%dT%H:%M:%S"
FUSO_SGRC = ZoneInfo("America/Sao_Paulo")


class _PostSGRC:
    """Substitui o POST HTTP ao SGRC e guarda cada chamada."""

    def __init__(self, resposta=None, erro=None):
        self.resposta = resposta
        self.erro = erro
        self.chamadas = []

    async def __call__(self, url, data, *args, **kwargs):
        self.chamadas.append((url, data))
        if self.erro is not None:
            raise self.erro
        return self.resposta


@pytest.fixture
def post_sgrc(monkeypatch):
    monkeypatch.setenv("SGRC_URL", SGRC_URL_SINTETICA)

    def instalar(resposta=None, erro=None):
        post = _PostSGRC(resposta=resposta, erro=erro)
        monkeypatch.setattr(sgrc, "apost", post)
        return post

    return instalar


@pytest.mark.parametrize("dependencia", ["pendulum", "pytz", "aiohttp", "requests"])
def test_dependencias_do_extra_sgrc_estao_instaladas(dependencia):
    # Lança ImportError com "pip install prefeitura_rio[sgrc]" se faltar.
    base_assert_dependencies([dependencia], extras=["sgrc"])


@pytest.mark.asyncio
async def test_async_new_ticket_chega_ao_post_do_sgrc(post_sgrc):
    post = post_sgrc(resposta=RESPOSTA_SUCESSO)
    antes = datetime.now(FUSO_SGRC).replace(microsecond=0, tzinfo=None)

    ticket = await sgrc.async_new_ticket(
        classification_code="1614", description="poda de árvore"
    )

    depois = datetime.now(FUSO_SGRC).replace(microsecond=0, tzinfo=None)
    assert ticket.protocol_id == PROTOCOLO
    assert ticket.ticket_id == CHAMADO

    [(url, corpo)] = post.chamadas
    assert url == f"{SGRC_URL_SINTETICA}/api/WSPortalWeb/NovoChamado"
    assert corpo["codigoClassificacao"] == "1614"
    # A data/hora vem do pendulum, no fuso de São Paulo.
    data_hora = datetime.strptime(corpo["dataHora"], FORMATO_DATA_HORA)
    assert antes - timedelta(seconds=1) <= data_hora <= depois + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_async_new_ticket_sgrc_indisponivel_vira_base_sgrc_exception(post_sgrc):
    post_sgrc(erro=aiohttp.ClientConnectionError("sgrc fora do ar"))

    with pytest.raises(BaseSGRCException) as excinfo:
        await sgrc.async_new_ticket(
            classification_code="1614", description="poda de árvore"
        )

    assert isinstance(excinfo.value.__cause__, aiohttp.ClientConnectionError)


def _estado_pronto_para_abrir():
    models = importlib.import_module(MODELS_MODULE)
    return models.ServiceState(
        user_id="u-sintetico",
        service_name="poda_de_arvore",
        payload={},
        data={
            "address": {
                "logradouro_nome_ipp": "Rua Sintetica",
                "logradouro_id_ipp": "000001",
                "bairro_nome_ipp": "Bairro Sintetico",
                "bairro_id_ipp": "001",
                "numero": "10",
            },
        },
    )


@pytest.fixture
def workflow():
    modulo = importlib.import_module(WORKFLOW_MODULE)
    # O fluxo chama a função da biblioteca, não uma cópia ou um stub.
    assert modulo.async_new_ticket is sgrc.async_new_ticket
    return modulo.PodaDeArvoreWorkflow(use_fake_api=False)


@pytest.mark.asyncio
async def test_open_ticket_da_poda_abre_o_chamado(post_sgrc, workflow):
    post = post_sgrc(resposta=RESPOSTA_SUCESSO)

    resultado = await workflow._open_ticket(_estado_pronto_para_abrir())

    assert resultado.data["ticket_created"] is True
    assert resultado.data["protocol_id"] == PROTOCOLO
    assert resultado.agent_response.error_message is None
    [(_url, corpo)] = post.chamadas
    assert corpo["codigoClassificacao"] == workflow.service_id
    assert corpo["endereco"]["codigoLogradouro"] == "000001"


@pytest.mark.asyncio
async def test_open_ticket_da_poda_com_sgrc_indisponivel(post_sgrc, workflow):
    post_sgrc(erro=aiohttp.ClientConnectionError("sgrc fora do ar"))

    resultado = await workflow._open_ticket(_estado_pronto_para_abrir())

    assert resultado.data["ticket_created"] is False
    assert resultado.data["error"] == "erro_geral"
    assert "dependencies are missing" not in resultado.agent_response.error_message
