"""
Testes da DEV-514: o `IPTUAPIService` e o `IPTUWorkflow` não gravam inscrição
imobiliária, número da guia, cotas nem os corpos das respostas das APIs no log.

Antes, o serviço logava `inscricao_clean`, `numero_guia` e `cotas_str` nas
consultas (guias, cotas, DARM, PDF, imóvel e dívida ativa), a URL do imóvel com
a inscrição no caminho, as respostas inteiras de guias e cotas em DEBUG, o dado
que falhou no parse (e a mensagem do Pydantic, que repete o valor) e o corpo das
respostas de erro. O workflow logava a inscrição salva, rejeitada e consultada
na dívida ativa, os dados do imóvel (endereço e proprietário) e o número da
guia. A redação global de PII (CHATR-167) não reconhece esses valores sem
formato nem rótulo.

Invariantes cobertos aqui, com o `logger` dos módulos trocado por um que guarda
toda chamada antes de qualquer redação:
- nenhuma linha de log, em qualquer nível, contém a inscrição (limpa ou
  digitada), o número da guia, as cotas, o nome ou o endereço sintéticos, nos
  caminhos de sucesso, de resposta vazia, de 404/400-033, de parse que falha,
  de resposta de erro que ecoa a inscrição e de exceção que traz a URL;
- a exceção de domínio continua subindo, sem a inscrição na mensagem quando
  ela vinha da URL ou de um erro de validação;
- o fluxo não muda: os retornos e o estado do workflow são os de antes.
"""

import httpx
import pytest

from src.tools.multi_step_service.core.models import ServiceState
from src.tools.multi_step_service.workflows.iptu_pagamento import iptu_workflow
from src.tools.multi_step_service.workflows.iptu_pagamento.api import api_service

# Dados sintéticos.
INSCRICAO = "90817263"
INSCRICAO_DIGITADA = "9081726-3"
EXERCICIO = 2025
NUMERO_GUIA = "86420"
COTAS = ["75319", "97531"]
NOME = "Contribuinte Sintetico Ficticio"
LOGRADOURO = "Travessa Inventada"
CDA = "60504030201"
TRECHOS_PROIBIDOS = (
    INSCRICAO,
    "9081726",
    NUMERO_GUIA,
    "75319",
    "97531",
    "Sintetico",
    "Ficticio",
    "Inventada",
    CDA,
)

IMOVEL_BASE = "https://imovel.sintetico.local/api"


class _LoggerCapturado:
    """Guarda toda chamada de log, em qualquer nível, com os argumentos."""

    def __init__(self):
        self.linhas = []

    def __getattr__(self, nivel):
        def registrar(*args, **kwargs):
            self.linhas.append((nivel, " ".join(str(a) for a in args), kwargs))

        return registrar

    def assert_sem_contribuinte(self):
        assert self.linhas, "nenhum log capturado: o teste não exercitou o caminho"
        for nivel, mensagem, kwargs in self.linhas:
            texto = f"{mensagem} {kwargs}"
            for trecho in TRECHOS_PROIBIDOS:
                assert trecho not in texto, (nivel, texto)


@pytest.fixture
def log(monkeypatch):
    capturado = _LoggerCapturado()
    monkeypatch.setattr(api_service, "logger", capturado)
    monkeypatch.setattr(iptu_workflow, "logger", capturado)
    return capturado


@pytest.fixture
def servico(monkeypatch):
    monkeypatch.setattr(api_service.env, "WA_IPTU_URL", IMOVEL_BASE)
    monkeypatch.setattr(
        api_service, "encrypt_token_rsa", lambda **_kwargs: "token-cifrado"
    )

    async def sem_espera(_segundos):
        return None

    monkeypatch.setattr(api_service.asyncio, "sleep", sem_espera)
    return api_service.IPTUAPIService(user_id="usuario-sintetico")


def _guia(numero=NUMERO_GUIA, codigo="01"):
    return {
        "Situacao": {"codigo": codigo, "descricao": "EM ABERTO"},
        "Inscricao": INSCRICAO,
        "Exercicio": str(EXERCICIO),
        "NGuia": numero,
        "Tipo": "ORDINÁRIA",
        "ValorIPTUOriginalGuia": "1.234,56",
        "DataVenctoDescCotaUnica": "07/02/2025",
        "QuantDiasEmAtraso": "0",
        "PercentualDescCotaUnica": "00007",
        "ValorIPTUDescontoAvista": "1.200,00",
        "ValorParcelas": "100,00",
        "CreditoNotaCarioca": "0,00",
        "CreditoDECAD": "0,00",
        "CreditoIsencao": "0,00",
        "CreditoCotaUnica": "0,00",
        "ValorQuitado": "0,00",
        "DataQuitacao": "",
        "Deposito": "N",
    }


def _cota(numero):
    return {
        "Situacao": {"codigo": "03", "descricao": "VENCIDA"},
        "NCota": numero,
        "ValorCota": "89,44",
        "DataVencimento": "07/11/2025",
        "ValorPago": "0,00",
        "DataPagamento": "",
        "QuantDiasEmAtraso": "10",
    }


def _darm(**extra):
    darm = {
        "Cotas": [{"ncota": c, "valor": "89,44"} for c in COTAS],
        "Inscricao": INSCRICAO,
        "Exercicio": str(EXERCICIO),
        "NGuia": NUMERO_GUIA,
        "Tipo": "ORDINÁRIA",
        "DataVencimento": "30/04/2026",
        "ValorIPTUOriginal": "100,00",
        "ValorDARM": "89,44",
        "ValorDescCotaUnica": "0,00",
        "CreditoNotaCarioca": "0,00",
        "CreditoDECAD": "0,00",
        "CreditoIsencao": "0,00",
        "CreditoEmissao": "0,00",
        "ValorAPagar": "89,44",
        "SequenciaNumerica": "123.456 789",
        "DescricaoDARM": "DARM",
        "CodReceita": "310-7",
        "DesReceita": "RECEITA",
        "Endereco": f"{LOGRADOURO} 12",
        "Nome": NOME,
    }
    darm.update(extra)
    return darm


def _api_falsa(monkeypatch, servico, respostas):
    async def fake_request(endpoint, params, expect_json=True):
        assert params["inscricao"] == INSCRICAO
        return respostas[endpoint]

    monkeypatch.setattr(servico, "_make_api_request", fake_request)


@pytest.mark.asyncio
async def test_consultar_guias_nao_loga_inscricao_nem_resposta(
    monkeypatch, servico, log
):
    # Um item válido e um que falha no parse e carrega a inscrição e a guia: a
    # mensagem do Pydantic repetiria esses valores
    _api_falsa(
        monkeypatch,
        servico,
        {"ConsultarGuias": [_guia(), {"Inscricao": INSCRICAO, "NGuia": NUMERO_GUIA}]},
    )
    dados = await servico.consultar_guias(INSCRICAO_DIGITADA, EXERCICIO)

    assert dados.inscricao_imobiliaria == INSCRICAO
    assert [g.numero_guia for g in dados.guias] == [NUMERO_GUIA]
    assert any("Failed to parse guia data" in m for _, m, _ in log.linhas)
    log.assert_sem_contribuinte()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resposta",
    [None, [], {"Inscricao": INSCRICAO}, [{"Inscricao": INSCRICAO}]],
)
async def test_consultar_guias_vazio_nao_loga_inscricao(
    monkeypatch, servico, log, resposta
):
    _api_falsa(monkeypatch, servico, {"ConsultarGuias": resposta})

    assert await servico.consultar_guias(INSCRICAO, EXERCICIO) is None
    log.assert_sem_contribuinte()


@pytest.mark.asyncio
async def test_obter_cotas_nao_loga_guia_nem_resposta(monkeypatch, servico, log):
    _api_falsa(
        monkeypatch,
        servico,
        {"ConsultarCotas": {"Cotas": [_cota(COTAS[0]), {"NCota": COTAS[1]}]}},
    )
    dados = await servico.obter_cotas(INSCRICAO, EXERCICIO, NUMERO_GUIA)

    assert dados.numero_guia == NUMERO_GUIA
    assert [c.numero_cota for c in dados.cotas] == [COTAS[0]]
    log.assert_sem_contribuinte()


@pytest.mark.asyncio
async def test_obter_cotas_vazio_nao_loga_guia(monkeypatch, servico, log):
    _api_falsa(monkeypatch, servico, {"ConsultarCotas": {"Erro": NUMERO_GUIA}})

    assert await servico.obter_cotas(INSCRICAO, EXERCICIO, NUMERO_GUIA) is None
    log.assert_sem_contribuinte()


@pytest.mark.asyncio
async def test_consultar_darm_nao_loga_guia_nem_cotas(monkeypatch, servico, log):
    _api_falsa(monkeypatch, servico, {"ConsultarDARM": _darm()})
    dados = await servico.consultar_darm(INSCRICAO, EXERCICIO, NUMERO_GUIA, COTAS)

    assert dados.numero_guia == NUMERO_GUIA
    assert dados.cotas_selecionadas == COTAS
    log.assert_sem_contribuinte()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resposta",
    [
        None,
        # Falha no parse: a mensagem do Pydantic traria nome, endereço e guia
        _darm(ValorAPagar={"valor": NOME}, NGuia=None),
    ],
)
async def test_consultar_darm_falho_nao_loga_dados(monkeypatch, servico, log, resposta):
    _api_falsa(monkeypatch, servico, {"ConsultarDARM": resposta})

    assert (
        await servico.consultar_darm(INSCRICAO, EXERCICIO, NUMERO_GUIA, COTAS) is None
    )
    log.assert_sem_contribuinte()


@pytest.mark.asyncio
@pytest.mark.parametrize("pdf", ["JVBERi0xLjQK", "<!DOCTYPE html>"])
async def test_download_pdf_darm_nao_loga_inscricao(monkeypatch, servico, log, pdf):
    _api_falsa(monkeypatch, servico, {"DownloadPdfDARM": pdf})

    async def fake_upload_to_gcs(**_kwargs):
        return "https://storage.sintetico.local/pdf"

    async def fake_get_short_url(**_kwargs):
        return "https://curto.sintetico.local/x"

    monkeypatch.setattr(api_service, "upload_to_gcs", fake_upload_to_gcs)
    monkeypatch.setattr(api_service, "get_short_url", fake_get_short_url)

    await servico.download_pdf_darm(INSCRICAO, EXERCICIO, NUMERO_GUIA, COTAS)
    log.assert_sem_contribuinte()


class _ClienteFalso:
    """Substitui o `InterceptedHTTPClient`: devolve as respostas em ordem."""

    def __init__(self, respostas, **_kwargs):
        self._respostas = respostas

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def _proxima(self):
        resposta = self._respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta

    async def get(self, url, **_kwargs):
        return self._proxima()

    async def post(self, url, **_kwargs):
        return self._proxima()


def _cliente(monkeypatch, respostas):
    monkeypatch.setattr(
        api_service,
        "InterceptedHTTPClient",
        lambda **kwargs: _ClienteFalso(respostas, **kwargs),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 418])
async def test_resposta_de_erro_nao_loga_corpo(monkeypatch, servico, log, status):
    # O backend ecoa a inscrição e a guia no corpo do erro; a primeira resposta
    # é transitória, para passar pelo log do retry
    corpo = f"Inscricao {INSCRICAO} guia {NUMERO_GUIA} invalida"
    _cliente(
        monkeypatch,
        [httpx.Response(502, text=corpo), httpx.Response(status, text=corpo)],
    )

    with pytest.raises(api_service.APIUnavailableError) as erro:
        await servico._make_api_request(
            "ConsultarGuias", {"inscricao": INSCRICAO, "guia": NUMERO_GUIA}
        )

    assert INSCRICAO not in str(erro.value)
    assert any("Transient API error" in m for _, m, _ in log.linhas)
    assert any(f"{len(corpo)} chars" in m for _, m, _ in log.linhas)
    log.assert_sem_contribuinte()


def _imovel_ok():
    return httpx.Response(
        200,
        json={
            "tipoLogradouro": "TV",
            "nomeLogradouro": LOGRADOURO,
            "numPorta": "12",
            "bairro": "Bairro",
            "cep": "20000000",
            "proprietarioPrincipal": NOME,
        },
    )


@pytest.mark.asyncio
async def test_get_imovel_info_nao_loga_inscricao_nem_url(monkeypatch, servico, log):
    _cliente(monkeypatch, [_imovel_ok()])

    dados = await servico.get_imovel_info(INSCRICAO_DIGITADA)

    assert dados["proprietario"] == NOME
    assert any(f"{IMOVEL_BASE}/<inscricao>" in m for _, m, _ in log.linhas)
    log.assert_sem_contribuinte()


@pytest.mark.asyncio
async def test_get_imovel_info_nao_encontrado_nao_loga_inscricao(
    monkeypatch, servico, log
):
    _cliente(monkeypatch, [httpx.Response(404)])

    assert await servico.get_imovel_info(INSCRICAO) is None
    log.assert_sem_contribuinte()


@pytest.mark.asyncio
async def test_get_imovel_info_codigo_033_nao_loga_inscricao(monkeypatch, servico, log):
    _cliente(monkeypatch, [httpx.Response(400, json={"codigo": "033"})])

    with pytest.raises(api_service.InvalidInscricaoError):
        await servico.get_imovel_info(INSCRICAO)
    log.assert_sem_contribuinte()


@pytest.mark.asyncio
async def test_get_imovel_info_excecao_com_url_nao_expoe_inscricao(
    monkeypatch, servico, log
):
    _cliente(
        monkeypatch,
        [httpx.ConnectError(f"falha ao conectar em {IMOVEL_BASE}/{INSCRICAO}")],
    )

    with pytest.raises(api_service.APIUnavailableError) as erro:
        await servico.get_imovel_info(INSCRICAO)

    assert INSCRICAO not in str(erro.value)
    assert "<inscricao>" in str(erro.value)
    log.assert_sem_contribuinte()


def _token():
    return httpx.Response(200, json={"access_token": "token-sintetico"})


@pytest.mark.asyncio
async def test_get_divida_ativa_info_nao_loga_inscricao(monkeypatch, servico, log):
    _cliente(monkeypatch, [_token(), httpx.Response(200, json={"success": False})])
    dados = await servico.get_divida_ativa_info(INSCRICAO)
    assert dados.tem_divida_ativa is False

    _cliente(monkeypatch, [_token(), httpx.Response(404)])
    assert await servico.get_divida_ativa_info(INSCRICAO) is None

    log.assert_sem_contribuinte()


@pytest.mark.asyncio
async def test_get_divida_ativa_info_erro_de_validacao_nao_expoe_cda(
    monkeypatch, servico, log
):
    # CDA numérica: `CDA.numero` é str, e a mensagem do Pydantic repetiria o valor
    resposta = {
        "success": True,
        "data": {
            "debitosNaoParceladosComSaldoTotal": {
                "cdasNaoAjuizadasNaoParceladas": [{"cdaId": int(CDA)}]
            }
        },
    }
    _cliente(monkeypatch, [_token(), httpx.Response(200, json=resposta)])

    with pytest.raises(api_service.APIUnavailableError) as erro:
        await servico.get_divida_ativa_info(INSCRICAO)

    assert CDA not in str(erro.value)
    assert "ValidationError" in str(erro.value)
    log.assert_sem_contribuinte()


class _ServicoFalso:
    """API do workflow: cada método devolve ou levanta o que o teste pede."""

    def __init__(self, **comportamento):
        self.comportamento = comportamento

    async def _executar(self, nome):
        resultado = self.comportamento[nome]
        if isinstance(resultado, Exception):
            raise resultado
        return resultado

    async def get_imovel_info(self, inscricao):
        assert inscricao == INSCRICAO
        return await self._executar("get_imovel_info")

    async def get_divida_ativa_info(self, inscricao):
        assert inscricao == INSCRICAO
        return await self._executar("get_divida_ativa_info")

    async def consultar_guias(self, inscricao, exercicio):
        return await self._executar("consultar_guias")

    async def obter_cotas(self, **_kwargs):
        return await self._executar("obter_cotas")


def _workflow(servico_falso):
    workflow = iptu_workflow.IPTUWorkflow(use_fake_api=True)
    workflow._api_service = servico_falso
    return workflow


def _estado(**kwargs):
    return ServiceState(
        user_id="usuario-sintetico", service_name="iptu_pagamento", **kwargs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "imovel",
    [
        {"endereco": f"{LOGRADOURO} 12", "proprietario": NOME},
        None,
        api_service.APIUnavailableError("indisponível"),
    ],
)
async def test_workflow_inscricao_salva_nao_loga_inscricao_nem_imovel(log, imovel):
    workflow = _workflow(_ServicoFalso(get_imovel_info=imovel))
    estado = _estado(payload={"inscricao_imobiliaria": INSCRICAO_DIGITADA})

    estado = await workflow._informar_inscricao_imobiliaria(estado)

    assert estado.data["inscricao_imobiliaria"] == INSCRICAO
    assert estado.agent_response is None
    log.assert_sem_contribuinte()


@pytest.mark.asyncio
async def test_workflow_inscricao_rejeitada_nao_loga_inscricao(log):
    erro = api_service.InvalidInscricaoError(inscricao=INSCRICAO, message="033")
    workflow = _workflow(_ServicoFalso(get_imovel_info=erro))
    estado = _estado(payload={"inscricao_imobiliaria": INSCRICAO})

    estado = await workflow._informar_inscricao_imobiliaria(estado)

    assert "inscricao_imobiliaria" not in estado.data
    assert estado.agent_response is not None
    log.assert_sem_contribuinte()


@pytest.mark.asyncio
async def test_workflow_divida_ativa_nao_loga_inscricao(log):
    divida = api_service.DadosDividaAtiva(tem_divida_ativa=True)
    workflow = _workflow(
        _ServicoFalso(get_divida_ativa_info=divida, consultar_guias=None)
    )
    estado = _estado(
        data={"inscricao_imobiliaria": INSCRICAO, "ano_exercicio": EXERCICIO}
    )

    estado = await workflow._consultar_guias_disponiveis(estado)

    assert estado.data["divida_ativa_data"]["tem_divida_ativa"] is True
    assert any("Dívida ativa encontrada" in m for _, m, _ in log.linhas)
    log.assert_sem_contribuinte()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "erro, mantida",
    [
        (api_service.APIUnavailableError("indisponível"), True),
        (api_service.DataNotFoundError("Endpoint não encontrado: X"), False),
    ],
)
async def test_workflow_filtro_de_guias_nao_loga_numero_da_guia(log, erro, mantida):
    dados_guias = api_service.DadosGuias(
        inscricao_imobiliaria=INSCRICAO,
        exercicio=str(EXERCICIO),
        guias=[api_service.Guia(**_guia())],
        total_guias=1,
    )
    dados_guias.guias[0].esta_em_aberto = True
    workflow = _workflow(_ServicoFalso(obter_cotas=erro))

    await workflow._filtrar_guias_sem_cotas_pagaveis(dados_guias)

    assert (len(dados_guias.guias) == 1) is mantida
    log.assert_sem_contribuinte()


def test_template_dados_imovel_nao_loga_guias(monkeypatch):
    from src.tools.multi_step_service.workflows.iptu_pagamento.templates import (
        IPTUMessageTemplates,
    )
    from src.utils import log as log_module

    capturado = _LoggerCapturado()
    # O template importa o logger dentro da função, a cada chamada
    monkeypatch.setattr(log_module, "logger", capturado)
    guias = [
        {
            "numero_guia": NUMERO_GUIA,
            "inscricao": INSCRICAO,
            "esta_em_aberto": True,
            "valor_original": 10.0,
        }
    ]

    texto = IPTUMessageTemplates.dados_imovel(
        inscricao=INSCRICAO,
        proprietario=NOME,
        endereco=f"{LOGRADOURO} 12",
        exercicio=str(EXERCICIO),
        guias=guias,
    )

    # A mensagem ao cidadão continua com os dados dele; só o log não os leva
    assert NUMERO_GUIA in texto
    assert ("debug", "Guias em aberto: 1", {}) in capturado.linhas
    capturado.assert_sem_contribuinte()
