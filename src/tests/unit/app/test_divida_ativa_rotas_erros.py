"""Erros das rotas HTTP de Dívida Ativa pelo app ASGI real (DEV-240).

As cinco `custom_route`s de Dívida Ativa liam o corpo e chamavam a função de
negócio dentro de um `try` que, em qualquer exceção, respondia 500 com
`str(e)`. Corpo que não é JSON virava erro de servidor e o texto interno da
exceção Python (`Expecting value: line 1 column 1 (char 0)`) ia ao chamador.

Estes testes montam o app de produção (`create_app()` com `IS_LOCAL=false`) e
fazem requisições de verdade pelo `http_app()`. Toda requisição leva um
`Bearer` válido de `VALID_TOKENS`, para que os testes sigam valendo quando as
rotas passarem a exigir autenticação (DEV-213).
"""

import importlib
import sys

import pytest
from starlette.testclient import TestClient

from src.health import state as health_state


TOKEN = "token-de-teste-dev-240"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Rota -> nome, em `src.app`, da função de negócio que ela chama. As rotas
# resolvem esse nome no módulo a cada requisição, então trocá-lo no módulo
# basta para controlar o que a função devolve ou levanta.
DIVIDA_ATIVA_ROUTES = {
    "/consulta_debitos": "consultar_debitos",
    "/emitir_guia": "emitir_guia_a_vista",
    "/emitir_guia_regularizacao": "emitir_guia_regularizacao",
    "/v2/emitir_guia": "emitir_guia_a_vista_v2",
    "/v2/emitir_guia_regularizacao": "emitir_guia_regularizacao_v2",
}

# Texto que, se aparecer na resposta, é a exceção Python vazando.
TEXTO_INTERNO = "segredo-interno-da-dependencia"


@pytest.fixture
def app_env(monkeypatch):
    """Importa `src.app` em produção (sem `IS_LOCAL`) num `sys.modules` limpo.

    Mesma higiene do `test_app_factory.py`: `src.config.env` lê o ambiente no
    import, então `src.*` é purgado antes e o snapshot volta no fim.
    """
    monkeypatch.setenv("IS_LOCAL", "false")
    monkeypatch.setenv("VALID_TOKENS", TOKEN)

    ready_antes = health_state.is_ready()
    snapshot = {
        name: mod for name, mod in sys.modules.items() if name.startswith("src")
    }
    for name in list(sys.modules):
        if name.startswith("src"):
            del sys.modules[name]
    try:
        yield importlib.import_module("src.app")
    finally:
        for name in list(sys.modules):
            if name.startswith("src"):
                del sys.modules[name]
        sys.modules.update(snapshot)
        health_state.set_ready(ready_antes)


@pytest.fixture
def chamadas(monkeypatch, app_env):
    """Troca as funções de negócio por duplos que registram o que recebem."""
    registro = []

    for nome in set(DIVIDA_ATIVA_ROUTES.values()):

        async def _duplo(parameters, _nome=nome):
            registro.append((_nome, parameters))
            return {"api_resposta_sucesso": True, "duplo": _nome}

        monkeypatch.setattr(app_env, nome, _duplo)
    return registro


@pytest.fixture
def client(app_env, chamadas):
    # Sem `with`: o lifespan (Redis, BigQuery, line-up) não sobe. As rotas de
    # Dívida Ativa não dependem dele.
    return TestClient(app_env.create_app().http_app(), raise_server_exceptions=False)


def _assert_erro_controlado(response, status_code: int) -> dict:
    assert response.status_code == status_code
    corpo = response.json()
    assert corpo["api_resposta_sucesso"] is False
    assert corpo["api_descricao_erro"]
    # A chave legada continua, com o mesmo texto controlado.
    assert corpo["error"] == corpo["api_descricao_erro"]
    assert "Expecting value" not in response.text
    assert "line 1 column" not in response.text
    assert TEXTO_INTERNO not in response.text
    return corpo


@pytest.mark.parametrize("route", list(DIVIDA_ATIVA_ROUTES))
@pytest.mark.parametrize(
    "corpo",
    [b"not-json", b"", b"{", b"\xff\xfe\x00", b"{'a': 1}"],
    ids=["texto", "vazio", "truncado", "bytes-invalidos", "aspas-simples"],
)
def test_corpo_que_nao_e_json_responde_400_sem_chamar_a_funcao(
    client, chamadas, route, corpo
):
    response = client.post(
        route,
        content=corpo,
        headers={**HEADERS, "Content-Type": "application/json"},
    )

    corpo_resposta = _assert_erro_controlado(response, 400)
    assert "objeto JSON" in corpo_resposta["api_descricao_erro"]
    assert chamadas == []


@pytest.mark.parametrize("route", list(DIVIDA_ATIVA_ROUTES))
@pytest.mark.parametrize(
    "corpo",
    ["[]", '["consulta_debitos"]', '"texto"', "42", "null", "true"],
    ids=["lista-vazia", "lista", "string", "numero", "null", "booleano"],
)
def test_json_que_nao_e_objeto_responde_400_sem_chamar_a_funcao(
    client, chamadas, route, corpo
):
    response = client.post(
        route,
        content=corpo,
        headers={**HEADERS, "Content-Type": "application/json"},
    )

    corpo_resposta = _assert_erro_controlado(response, 400)
    assert "objeto" in corpo_resposta["api_descricao_erro"]
    assert chamadas == []


@pytest.mark.parametrize("route", list(DIVIDA_ATIVA_ROUTES))
def test_excecao_inesperada_responde_500_sem_o_texto_da_excecao(
    client, app_env, monkeypatch, route
):
    async def _quebra(parameters):
        raise RuntimeError(TEXTO_INTERNO)

    monkeypatch.setattr(app_env, DIVIDA_ATIVA_ROUTES[route], _quebra)

    response = client.post(route, json={"qualquer": "coisa"}, headers=HEADERS)

    corpo = _assert_erro_controlado(response, 500)
    assert corpo["api_descricao_erro"].startswith("Erro interno")


@pytest.mark.parametrize("route", list(DIVIDA_ATIVA_ROUTES))
def test_objeto_json_valido_chega_a_funcao_e_responde_200(client, chamadas, route):
    payload = {"consulta_debitos": "cda", "cda": "0"}  # dado sintético

    response = client.post(route, json=payload, headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "api_resposta_sucesso": True,
        "duplo": DIVIDA_ATIVA_ROUTES[route],
    }
    assert chamadas == [(DIVIDA_ATIVA_ROUTES[route], payload)]


def test_consulta_debitos_sem_o_campo_nomeia_o_campo(app_env, monkeypatch):
    """Ocorrência 2 do QA: `{}` respondia `Erro ao consultar débitos: 'consulta_debitos'`.

    Usa a função de negócio real; a PGM é trocada por um duplo que falha o teste
    se for chamada.
    """

    async def _pgm_nao_pode_ser_chamada(**_kwargs):
        raise AssertionError("a PGM não pode ser chamada com entrada inválida")

    monkeypatch.setattr(
        sys.modules["src.tools.divida_ativa"], "pgm_api", _pgm_nao_pode_ser_chamada
    )
    client = TestClient(app_env.create_app().http_app(), raise_server_exceptions=False)

    response = client.post("/consulta_debitos", json={}, headers=HEADERS)

    assert response.status_code == 200
    corpo = response.json()
    assert corpo["api_resposta_sucesso"] is False
    assert "Campo obrigatório 'consulta_debitos' ausente" in corpo["api_descricao_erro"]
    assert "Erro ao consultar débitos" not in corpo["api_descricao_erro"]


# Payloads sintéticos que passam pela validação de cada rota e chegam à PGM.
# Nenhum CPF, inscrição, CDA ou guia real.
_CDA = "01/000000/2024-00"
_GUIA = "99/000000/2023-00"
PAYLOAD_ATE_A_PGM = {
    "/consulta_debitos": {"consulta_debitos": "cda", "cda": "0"},
    "/emitir_guia": {
        "dicionario_itens": f'{{"1": "{_CDA}"}}',
        "lista_cdas": f'["{_CDA}"]',
        "apenas_um_item": "1",
    },
    "/emitir_guia_regularizacao": {
        "dicionario_itens": f'{{"1": "{_GUIA}"}}',
        "lista_guias": f'["{_GUIA}"]',
        "apenas_um_item": "1",
    },
}
PAYLOAD_ATE_A_PGM["/v2/emitir_guia"] = PAYLOAD_ATE_A_PGM["/emitir_guia"]
PAYLOAD_ATE_A_PGM["/v2/emitir_guia_regularizacao"] = PAYLOAD_ATE_A_PGM[
    "/emitir_guia_regularizacao"
]


@pytest.mark.parametrize("route", list(DIVIDA_ATIVA_ROUTES))
def test_falha_da_pgm_nao_vaza_o_texto_da_excecao(app_env, monkeypatch, route):
    """Exceção vinda da dependência, pelas funções de negócio reais.

    `pgm_api` relança o que não é timeout (PGM inacessível, token recusado,
    resposta fora do formato). O texto dessa exceção não pode chegar ao
    chamador por nenhuma das cinco rotas.
    """
    chamadas_pgm = []

    async def _pgm_quebrada(**kwargs):
        chamadas_pgm.append(kwargs["endpoint"])
        raise RuntimeError(TEXTO_INTERNO)

    for modulo in ("src.tools.divida_ativa", "src.tools.divida_ativa_v2.service"):
        monkeypatch.setattr(sys.modules[modulo], "pgm_api", _pgm_quebrada)

    client = TestClient(app_env.create_app().http_app(), raise_server_exceptions=False)

    response = client.post(route, json=PAYLOAD_ATE_A_PGM[route], headers=HEADERS)

    assert chamadas_pgm, "o payload deveria chegar à PGM"
    assert response.status_code == 200
    corpo = response.json()
    assert corpo["api_resposta_sucesso"] is False
    assert corpo["api_descricao_erro"].startswith("Erro ao ")
    assert TEXTO_INTERNO not in response.text
