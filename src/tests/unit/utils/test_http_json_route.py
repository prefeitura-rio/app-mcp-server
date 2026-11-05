"""`executar_rota_json`: tradução de falhas das rotas HTTP em 400/500 (DEV-240).

O caminho pelo app real está em `src/tests/unit/app/test_divida_ativa_rotas_erros.py`;
aqui ficam os casos que um cliente HTTP não produz de forma confiável, como a
leitura do corpo falhar por outro motivo que não o conteúdo.
"""

import json

import pytest

from src.utils import http_json_route
from src.utils.http_json_route import (
    MENSAGEM_CORPO_NAO_JSON,
    MENSAGEM_ERRO_INTERNO,
    executar_rota_json,
)

TEXTO_INTERNO = "segredo-interno-da-dependencia"


class _Url:
    path = "/rota_de_teste"


class _RequestFalso:
    url = _Url()

    def __init__(self, efeito):
        self._efeito = efeito

    async def json(self):
        if isinstance(self._efeito, BaseException):
            raise self._efeito
        return self._efeito


@pytest.fixture
def logs(monkeypatch):
    registro = []

    class _Logger:
        def warning(self, mensagem):
            registro.append(("warning", mensagem))

        def error(self, mensagem):
            registro.append(("error", mensagem))

    monkeypatch.setattr(http_json_route, "logger", _Logger())
    return registro


async def _nao_chamar(parameters):
    raise AssertionError("a função de negócio não pode ser chamada")


@pytest.mark.asyncio
async def test_falha_ao_ler_o_corpo_responde_500_generico(logs):
    response = await executar_rota_json(
        _RequestFalso(RuntimeError(TEXTO_INTERNO)), _nao_chamar
    )

    assert response.status_code == 500
    corpo = json.loads(response.body)
    assert corpo["api_descricao_erro"] == MENSAGEM_ERRO_INTERNO
    assert TEXTO_INTERNO not in response.body.decode()
    assert logs == [
        (
            "error",
            {
                "event": "http_route_body_error",
                "route": "/rota_de_teste",
                "error_type": "RuntimeError",
            },
        )
    ]


@pytest.mark.asyncio
async def test_json_invalido_responde_400_e_loga_sem_o_corpo(logs):
    erro = json.JSONDecodeError("Expecting value", TEXTO_INTERNO, 0)

    response = await executar_rota_json(_RequestFalso(erro), _nao_chamar)

    assert response.status_code == 400
    assert json.loads(response.body)["api_descricao_erro"] == MENSAGEM_CORPO_NAO_JSON
    assert TEXTO_INTERNO not in repr(logs)


@pytest.mark.asyncio
async def test_excecao_da_funcao_loga_so_rota_e_tipo(logs):
    async def _quebra(parameters):
        raise KeyError(TEXTO_INTERNO)

    response = await executar_rota_json(_RequestFalso({"a": 1}), _quebra)

    assert response.status_code == 500
    assert TEXTO_INTERNO not in response.body.decode()
    assert TEXTO_INTERNO not in repr(logs)
    assert logs[-1][1]["error_type"] == "KeyError"


@pytest.mark.asyncio
async def test_resultado_da_funcao_sai_com_200(logs):
    async def _ok(parameters):
        return {"api_resposta_sucesso": True, "recebido": parameters}

    response = await executar_rota_json(_RequestFalso({"a": 1}), _ok)

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "api_resposta_sucesso": True,
        "recebido": {"a": 1},
    }
    assert logs == []
