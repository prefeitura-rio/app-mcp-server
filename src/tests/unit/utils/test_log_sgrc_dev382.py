"""
Testes da DEV-382: a integração do SGRC da biblioteca `prefeitura-rio` não pode
levar segredo nem dado pessoal ao log, em nenhum `LOG_LEVEL`.

`post` e `apost` (`prefeitura_rio.integrations.sgrc.utils`) fazem
`logger.debug(f"Making POST request to {url} with data {data} and headers
{headers}")` no loguru global. O corpo leva o `token` (`SGRC_BODY_TOKEN`), o
solicitante e o endereço; os cabeçalhos levam o `Authorization`
(`SGRC_AUTHORIZATION_HEADER`). Com `LOG_LEVEL=DEBUG` a linha ia inteira para o
SigNoz. `src/utils/log.py` desliga o logger do pacote na origem.

Tudo roda com a biblioteca de verdade; só o HTTP ao SGRC é simulado. Todos os
valores são sintéticos.
"""

import pytest
import requests
from loguru import logger

import prefeitura_rio.integrations.sgrc as sgrc
import prefeitura_rio.integrations.sgrc.utils as sgrc_utils
import src.utils.log as log_mod
from prefeitura_rio.integrations.sgrc.models import Address, Phones, Requester


PACOTE_SGRC = "prefeitura_rio.integrations.sgrc"
LINHA_DA_BIBLIOTECA = "Making POST request"

SGRC_URL_SINTETICA = "https://sgrc.sintetico.invalid"
TOKEN = "TOKEN-CORPO-SINTETICO-DEV382"
AUTORIZACAO = "AUTORIZACAO-SINTETICA-DEV382"
NOME = "Solicitante Sintetico Dev"
CPF = "00000000000"
EMAIL = "solicitante.sintetico@exemplo.invalid"
TELEFONE = "21900000000"
LOGRADOURO = "Rua Sintetica Dev Trezentos"
RESPOSTA_SUCESSO = {
    "codigo": 1,
    "descricao": "Chamado criado",
    "protocolo": "PROTO-SINTETICO-1",
    "codigoChamado": "CHAMADO-SINTETICO-1",
}

VALORES_QUE_NAO_PODEM_VAZAR = (
    TOKEN,
    AUTORIZACAO,
    NOME,
    CPF,
    EMAIL,
    TELEFONE,
    LOGRADOURO,
)


class _RespostaAiohttp:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def json(self):
        return RESPOSTA_SUCESSO


class _SessaoAiohttp:
    """Substitui `aiohttp.ClientSession` e guarda os POSTs."""

    chamadas = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        _SessaoAiohttp.chamadas.append((url, json, headers))
        return _RespostaAiohttp()


class _RespostaRequests:
    def raise_for_status(self):
        return None

    def json(self):
        return RESPOSTA_SUCESSO


@pytest.fixture
def sgrc_simulado(monkeypatch):
    monkeypatch.setenv("SGRC_URL", SGRC_URL_SINTETICA)
    monkeypatch.setenv("SGRC_BODY_TOKEN", TOKEN)
    monkeypatch.setenv("SGRC_AUTHORIZATION_HEADER", AUTORIZACAO)

    _SessaoAiohttp.chamadas = []
    monkeypatch.setattr(sgrc_utils.aiohttp, "ClientSession", _SessaoAiohttp)

    chamadas_requests = []

    def post_requests(self, url, json=None, headers=None, **kwargs):
        chamadas_requests.append((url, json, headers))
        return _RespostaRequests()

    monkeypatch.setattr(requests.Session, "post", post_requests)
    return {"aiohttp": _SessaoAiohttp.chamadas, "requests": chamadas_requests}


@pytest.fixture
def barreira_em_debug(monkeypatch):
    """A barreira de produção instalada como num pod com `LOG_LEVEL=DEBUG`."""
    monkeypatch.setattr(log_mod.Settings, "LOG_LEVEL", "DEBUG")
    log_mod._redacao_instalada = False
    log_mod.instalar_redacao()
    yield
    monkeypatch.undo()
    log_mod._redacao_instalada = False
    log_mod.instalar_redacao()


@pytest.fixture
def sink_debug():
    """Sink de teste em DEBUG: qualquer linha que passe da origem cai aqui."""
    linhas = []
    sink_id = logger.add(
        linhas.append, level="DEBUG", format="{name} | {message}", colorize=False
    )
    try:
        yield linhas
    finally:
        logger.remove(sink_id)


def _corpo_com_dado_pessoal():
    return {
        "solicitantePortalWeb": {
            "nome": NOME,
            "cpf": CPF,
            "email": EMAIL,
            "telefones": {"telefoneR": TELEFONE},
        },
        "endereco": {"logradouro": LOGRADOURO},
    }


def _requester_sintetico():
    return Requester(
        name=NOME,
        email=EMAIL,
        cpf=CPF,
        phones=Phones(mobile_phone=TELEFONE),
    )


def _assert_sem_vazamento(texto):
    assert LINHA_DA_BIBLIOTECA not in texto
    assert PACOTE_SGRC not in texto
    for valor in VALORES_QUE_NAO_PODEM_VAZAR:
        assert valor not in texto


def test_log_py_desliga_o_logger_do_pacote_sgrc():
    ativacao = dict(logger._core.activation_list)
    assert ativacao.get(f"{PACOTE_SGRC}.") is False


@pytest.mark.asyncio
async def test_apost_nao_loga_corpo_nem_cabecalhos_em_debug(
    sgrc_simulado, barreira_em_debug, sink_debug, capsys
):
    resposta = await sgrc_utils.apost(
        f"{SGRC_URL_SINTETICA}/api/WSPortalWeb/NovoChamado", _corpo_com_dado_pessoal()
    )

    assert resposta == RESPOSTA_SUCESSO
    # O POST de fato levou token e Authorization: o que se prova é só o log.
    [(_url, corpo, cabecalhos)] = sgrc_simulado["aiohttp"]
    assert corpo["token"] == TOKEN
    assert cabecalhos["Authorization"] == AUTORIZACAO

    _assert_sem_vazamento("".join(sink_debug))
    _assert_sem_vazamento(capsys.readouterr().err)


def test_post_nao_loga_corpo_nem_cabecalhos_em_debug(
    sgrc_simulado, barreira_em_debug, sink_debug, capsys
):
    resposta = sgrc_utils.post(
        f"{SGRC_URL_SINTETICA}/api/WSPortalWeb/NovoChamado", _corpo_com_dado_pessoal()
    )

    assert resposta == RESPOSTA_SUCESSO
    [(_url, corpo, cabecalhos)] = sgrc_simulado["requests"]
    assert corpo["token"] == TOKEN
    assert cabecalhos["Authorization"] == AUTORIZACAO

    _assert_sem_vazamento("".join(sink_debug))
    _assert_sem_vazamento(capsys.readouterr().err)


@pytest.mark.asyncio
async def test_async_new_ticket_com_solicitante_nao_loga_nada_do_sgrc(
    sgrc_simulado, barreira_em_debug, sink_debug, capsys
):
    ticket = await sgrc.async_new_ticket(
        classification_code="1614",
        description="poda de árvore",
        address=Address(street=LOGRADOURO, number="10"),
        requester=_requester_sintetico(),
    )

    assert ticket.protocol_id == RESPOSTA_SUCESSO["protocolo"]
    [(_url, corpo, _cabecalhos)] = sgrc_simulado["aiohttp"]
    assert corpo["solicitantePortalWeb"]["cpf"] == CPF

    _assert_sem_vazamento("".join(sink_debug))
    _assert_sem_vazamento(capsys.readouterr().err)


@pytest.mark.asyncio
async def test_controle_sem_o_disable_a_linha_vaza(sgrc_simulado, sink_debug):
    """
    Controle: com o logger do pacote religado, a mesma chamada emite a linha.

    Prova que os testes acima detectam a regressão, em vez de passarem porque a
    biblioteca deixou de logar ou porque o sink não recebe nada.
    """
    logger.enable(PACOTE_SGRC)
    try:
        await sgrc_utils.apost(
            f"{SGRC_URL_SINTETICA}/api/WSPortalWeb/NovoChamado",
            _corpo_com_dado_pessoal(),
        )
    finally:
        logger.disable(PACOTE_SGRC)

    texto = "".join(sink_debug)
    assert LINHA_DA_BIBLIOTECA in texto
    assert f"{PACOTE_SGRC}.utils" in texto
