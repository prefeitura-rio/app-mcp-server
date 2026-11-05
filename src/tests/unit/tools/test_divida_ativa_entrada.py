"""Validação de entrada e mensagens de erro da Dívida Ativa v1 (DEV-240).

`consultar_debitos` lia `parameters["consulta_debitos"]` sem checar, e o
`except` genérico devolvia `Erro ao consultar débitos: 'consulta_debitos'`: o
texto cru de um `KeyError`, que não diz qual campo falta. As três funções v1
também embutiam `str(e)` de qualquer exceção na resposta ao chamador.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[4]
MODULE_PATH = PROJECT_ROOT / "src" / "tools" / "divida_ativa.py"

TEXTO_INTERNO = "segredo-interno-da-dependencia"


@pytest.fixture
def divida_module(monkeypatch):
    """Carrega `divida_ativa.py` isolado, como em `test_divida_ativa.py`."""
    env_module = types.SimpleNamespace(
        CHATBOT_PGM_API_URL="https://pgm.example.local",
        CHATBOT_PGM_ACCESS_KEY="chave-falsa",
    )
    logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    interceptor_module = types.SimpleNamespace(
        interceptor=lambda *args, **kwargs: lambda func: func
    )

    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.utils.log", types.SimpleNamespace(logger=logger)
    )
    monkeypatch.setitem(sys.modules, "src.utils.error_interceptor", interceptor_module)
    monkeypatch.setitem(
        sys.modules,
        "src.tools.utils",
        types.SimpleNamespace(internal_request=None),
    )

    spec = importlib.util.spec_from_file_location(
        "test_divida_ativa_entrada_module", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "test_divida_ativa_entrada_module", module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pgm_proibida(divida_module, monkeypatch):
    """A PGM nunca pode ser chamada com entrada inválida."""

    async def _pgm(**_kwargs):
        raise AssertionError("a PGM não pode ser chamada com entrada inválida")

    monkeypatch.setattr(divida_module, "pgm_api", _pgm)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameters, campo",
    [
        ({}, "'consulta_debitos' ausente"),
        ({"consulta_debitos": None}, "'consulta_debitos' ausente"),
        ({"consulta_debitos": ""}, "'consulta_debitos' ausente"),
        ({"consulta_debitos": "placa"}, "Valor inválido em 'consulta_debitos'"),
        ({"consulta_debitos": 1}, "Valor inválido em 'consulta_debitos'"),
        ({"consulta_debitos": ["cda"]}, "Valor inválido em 'consulta_debitos'"),
        ({"consulta_debitos": "cda"}, "Campo obrigatório 'cda' ausente"),
        (
            {"consulta_debitos": "cpfCnpj", "cpfCnpj": 123},
            "'cpfCnpj' deve ser um texto",
        ),
        (
            {"consulta_debitos": "numeroAutoInfracao", "numeroAutoInfracao": "1"},
            "Campo obrigatório 'anoAutoInfracao' ausente",
        ),
        (
            {
                "consulta_debitos": "numeroAutoInfracao",
                "numeroAutoInfracao": "1",
                "anoAutoInfracao": 2024,
            },
            "'anoAutoInfracao' deve ser um texto",
        ),
        ([], "envie um objeto JSON"),
    ],
    ids=[
        "sem-tipo",
        "tipo-null",
        "tipo-vazio",
        "tipo-desconhecido",
        "tipo-numero",
        "tipo-lista",
        "sem-valor",
        "valor-numero",
        "sem-ano",
        "ano-numero",
        "nao-objeto",
    ],
)
async def test_consultar_debitos_entrada_invalida_nomeia_o_campo(
    divida_module, pgm_proibida, parameters, campo
):
    result = await divida_module.consultar_debitos(parameters)

    assert result["api_resposta_sucesso"] is False
    assert campo in result["api_descricao_erro"]
    assert "Erro ao consultar débitos" not in result["api_descricao_erro"]


@pytest.mark.asyncio
async def test_consultar_debitos_tipo_desconhecido_nao_ecoa_o_valor(
    divida_module, pgm_proibida
):
    result = await divida_module.consultar_debitos(
        {"consulta_debitos": "<script>alert(1)</script>"}
    )

    assert "<script>" not in result["api_descricao_erro"]
    for tipo in divida_module.TIPOS_CONSULTA_DEBITOS:
        assert tipo in result["api_descricao_erro"]


@pytest.mark.asyncio
async def test_consultar_debitos_tipos_aceitos_chegam_a_pgm(divida_module, monkeypatch):
    chamadas = []

    async def _pgm(endpoint, consumidor, data):
        chamadas.append(data)
        return {"erro": True, "motivos": "Nenhum débito encontrado."}

    monkeypatch.setattr(divida_module, "pgm_api", _pgm)

    for tipo in divida_module.TIPOS_CONSULTA_DEBITOS:
        parameters = {"consulta_debitos": tipo, tipo: "0"}  # dado sintético
        if tipo == "numeroAutoInfracao":
            parameters["anoAutoInfracao"] = "2024"
        result = await divida_module.consultar_debitos(parameters)
        assert result == {
            "api_resposta_sucesso": False,
            "api_descricao_erro": "Nenhum débito encontrado.",
        }

    assert [list(d)[1] for d in chamadas] == list(divida_module.TIPOS_CONSULTA_DEBITOS)


@pytest.mark.asyncio
async def test_consultar_debitos_excecao_nao_expoe_o_texto(divida_module, monkeypatch):
    async def _pgm(**_kwargs):
        raise RuntimeError(TEXTO_INTERNO)

    monkeypatch.setattr(divida_module, "pgm_api", _pgm)

    result = await divida_module.consultar_debitos(
        {"consulta_debitos": "cda", "cda": "0"}
    )

    assert result["api_resposta_sucesso"] is False
    assert TEXTO_INTERNO not in result["api_descricao_erro"]
    assert result["api_descricao_erro"].startswith("Erro ao consultar débitos.")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "funcao, prefixo",
    [
        ("emitir_guia_a_vista", "Erro ao emitir guia à vista."),
        ("emitir_guia_regularizacao", "Erro ao emitir guia de regularização."),
    ],
)
async def test_emissao_v1_excecao_nao_expoe_o_texto(
    divida_module, monkeypatch, funcao, prefixo
):
    async def _quebra(parameters, tipo):
        raise RuntimeError(TEXTO_INTERNO)

    monkeypatch.setattr(divida_module, "da_emitir_guia", _quebra)

    result = await getattr(divida_module, funcao)({})

    assert result["api_resposta_sucesso"] is False
    assert TEXTO_INTERNO not in result["api_descricao_erro"]
    assert result["api_descricao_erro"].startswith(prefixo)
