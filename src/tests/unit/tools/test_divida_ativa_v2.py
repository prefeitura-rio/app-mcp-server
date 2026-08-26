"""
Testes da v2 de emissão de guias de dívida ativa (CHATR-120).

Cobrem a validação Pydantic da entrada (formato legado e nativo, placeholders
do SFMC, tipos inválidos), a montagem do payload da PGM e o modelo de resposta.
"""

import pytest
from pydantic import ValidationError

from src.tools.divida_ativa_v2 import service as service_module
from src.tools.divida_ativa_v2.models import EmitirGuiaRequest, EmitirGuiaResponse
from src.tools.divida_ativa_v2.service import (
    MENSAGEM_SELECAO_VAZIA,
    MENSAGEM_SEM_GUIA,
    emitir_guia_a_vista_v2,
    emitir_guia_regularizacao_v2,
    formatar_erro_validacao,
    montar_parametros_entrada,
)


CDA = "01/225716/2024-00"
GUIA = "99/111111/2023-11"

# Payload exato que o SFMC envia hoje (registrado no CHATR-120).
PAYLOAD_LEGADO = {
    "dicionario_itens": '{"1": "01/225716/2024-00"}',
    "lista_cdas": '["01/225716/2024-00"]',
    "lista_efs": "",
    "lista_guias": "",
    "apenas_um_item": "1",
}

PLACEHOLDER = '{{Event.DEAudience-9f2c."itens_informados"}}'


@pytest.fixture(autouse=True)
def interceptor_mock(monkeypatch):
    """
    Captura os reports ao error interceptor e registra as chamadas.

    Autouse porque o conftest raiz define ERROR_INTERCEPTOR_URL/TOKEN: sem o
    mock, toda falha de validação dispararia um POST HTTP real.
    """
    reports = []

    async def fake_send_general_error(**kwargs):
        reports.append(kwargs)
        return True

    monkeypatch.setattr(service_module, "send_general_error", fake_send_general_error)
    return reports


@pytest.fixture
def logger_spy(monkeypatch):
    """Substitui o logger do serviço e registra as mensagens por nível."""
    registros = {"info": [], "warning": [], "error": []}

    class _Spy:
        def info(self, mensagem, *_args, **_kwargs):
            registros["info"].append(mensagem)

        def warning(self, mensagem, *_args, **_kwargs):
            registros["warning"].append(mensagem)

        def error(self, mensagem, *_args, **_kwargs):
            registros["error"].append(mensagem)

    monkeypatch.setattr(service_module, "logger", _Spy())
    return registros


# ===== Validação e coerção da entrada =====


def test_payload_legado_do_sfmc_e_coagido_para_estruturas():
    requisicao = EmitirGuiaRequest.model_validate(PAYLOAD_LEGADO)

    assert requisicao.dicionario_itens == {"1": CDA}
    assert requisicao.lista_cdas == [CDA]
    assert requisicao.lista_efs == []
    assert requisicao.lista_guias == []
    assert requisicao.apenas_um_item == "1"
    assert requisicao.sequenciais_escolhidos() == ["1"]


def test_payload_nativo_e_aceito_sem_alteracao():
    requisicao = EmitirGuiaRequest.model_validate(
        {
            "dicionario_itens": {"1": CDA, "2": GUIA},
            "lista_cdas": [CDA],
            "lista_guias": [GUIA],
            "itens_informados": ["1", "2"],
        }
    )

    assert requisicao.dicionario_itens == {"1": CDA, "2": GUIA}
    assert requisicao.lista_cdas == [CDA]
    assert requisicao.lista_guias == [GUIA]
    assert requisicao.sequenciais_escolhidos() == ["1", "2"]


@pytest.mark.parametrize("vazio", ["", "   ", None])
def test_campos_vazios_viram_colecoes_vazias(vazio):
    requisicao = EmitirGuiaRequest.model_validate(
        {"lista_efs": vazio, "lista_guias": vazio, "dicionario_itens": vazio}
    )

    assert requisicao.lista_efs == []
    assert requisicao.lista_guias == []
    assert requisicao.dicionario_itens == {}


def test_aspas_simples_do_formato_legado_sao_toleradas():
    requisicao = EmitirGuiaRequest.model_validate(
        {
            "dicionario_itens": "{'1': '01/225716/2024-00'}",
            "lista_cdas": "['01/225716/2024-00']",
        }
    )

    assert requisicao.dicionario_itens == {"1": CDA}
    assert requisicao.lista_cdas == [CDA]


@pytest.mark.parametrize(
    "bruto,esperado", [("1", "1"), (1, "1"), (1.0, "1"), (True, "1")]
)
def test_apenas_um_item_aceita_variacoes_de_tipo(bruto, esperado):
    requisicao = EmitirGuiaRequest.model_validate({"apenas_um_item": bruto})

    assert requisicao.apenas_um_item == esperado
    assert requisicao.sequenciais_escolhidos() == [esperado]


def test_itens_informados_escalar_vira_lista():
    requisicao = EmitirGuiaRequest.model_validate({"itens_informados": "2"})

    assert requisicao.itens_informados == ["2"]


def test_itens_informados_nao_numerico_e_preservado():
    """Um identificador não numérico não pode ser destruído por conversão."""
    requisicao = EmitirGuiaRequest.model_validate({"itens_informados": [CDA]})

    assert requisicao.itens_informados == [CDA]


def test_tupla_do_formato_legado_vira_lista():
    requisicao = EmitirGuiaRequest.model_validate(
        {"lista_cdas": "('01/225716/2024-00',)"}
    )

    assert requisicao.lista_cdas == [CDA]


@pytest.mark.parametrize("vazio", [None, "", "   "])
def test_apenas_um_item_vazio_vira_none(vazio):
    requisicao = EmitirGuiaRequest.model_validate({"apenas_um_item": vazio})

    assert requisicao.apenas_um_item is None
    assert requisicao.sequenciais_escolhidos() == []


def test_itens_informados_tem_precedencia_sobre_apenas_um_item():
    requisicao = EmitirGuiaRequest.model_validate(
        {"itens_informados": ["1", "3"], "apenas_um_item": "2"}
    )

    assert requisicao.sequenciais_escolhidos() == ["1", "3"]


def test_campo_desconhecido_do_sfmc_e_ignorado():
    requisicao = EmitirGuiaRequest.model_validate(
        {**PAYLOAD_LEGADO, "campo_de_controle_sfmc": "qualquer coisa"}
    )

    assert not hasattr(requisicao, "campo_de_controle_sfmc")
    assert requisicao.lista_cdas == [CDA]


def test_payload_vazio_e_valido_e_nao_seleciona_nada():
    requisicao = EmitirGuiaRequest.model_validate({})

    assert requisicao.sequenciais_escolhidos() == []


# ===== Guarda de placeholder do SFMC =====


@pytest.mark.parametrize(
    "campo",
    [
        "dicionario_itens",
        "lista_cdas",
        "lista_efs",
        "lista_guias",
        "itens_informados",
        "apenas_um_item",
    ],
)
def test_placeholder_nao_renderizado_e_rejeitado_em_cada_campo(campo):
    with pytest.raises(ValidationError) as exc:
        EmitirGuiaRequest.model_validate({campo: PLACEHOLDER})

    mensagem = str(exc.value)
    assert "placeholder de template não renderizado" in mensagem
    assert campo in mensagem
    assert "SFMC" in mensagem


def test_mensagem_de_placeholder_mostra_as_chaves_duplas():
    """A mensagem precisa exibir '{{...}}', não '{...}' comido pelo str.format."""
    with pytest.raises(ValidationError) as exc:
        EmitirGuiaRequest.model_validate({"lista_cdas": PLACEHOLDER})

    assert "({{...}})" in str(exc.value)


def test_placeholder_aninhado_dentro_de_lista_e_detectado():
    with pytest.raises(ValidationError, match="placeholder de template"):
        EmitirGuiaRequest.model_validate({"lista_cdas": [CDA, PLACEHOLDER]})


def test_placeholder_aninhado_dentro_de_dicionario_e_detectado():
    with pytest.raises(ValidationError, match="placeholder de template"):
        EmitirGuiaRequest.model_validate({"dicionario_itens": {"1": PLACEHOLDER}})


# ===== Entradas inválidas =====


def test_json_malformado_gera_erro_claro():
    with pytest.raises(ValidationError) as exc:
        EmitirGuiaRequest.model_validate({"lista_cdas": '["01/225716/2024-00"'})

    assert "não é um JSON válido" in str(exc.value)


def test_dicionario_itens_com_tipo_errado_e_rejeitado():
    with pytest.raises(ValidationError, match="deve ser um objeto/dicionário"):
        EmitirGuiaRequest.model_validate({"dicionario_itens": '["nao", "e", "dict"]'})


def test_lista_com_tipo_errado_e_rejeitada():
    with pytest.raises(ValidationError, match="deve ser uma lista"):
        EmitirGuiaRequest.model_validate({"lista_cdas": '{"nao": "e lista"}'})


# ===== Normalização simétrica de sequencial =====


def test_chave_do_dicionario_e_normalizada_como_o_sequencial():
    """'01' na chave precisa casar com '01' em apenas_um_item (ambos -> '1')."""
    requisicao = EmitirGuiaRequest.model_validate(
        {"dicionario_itens": {"01": CDA}, "lista_cdas": [CDA], "apenas_um_item": "01"}
    )

    assert requisicao.dicionario_itens == {"1": CDA}
    assert montar_parametros_entrada(requisicao, "a_vista") == {
        "origem_solicitação": 0,
        "cdas": [CDA],
        "efs": [],
    }


def test_chaves_que_colidem_apos_normalizacao_sao_rejeitadas():
    with pytest.raises(ValidationError, match="chaves duplicadas após normalização"):
        EmitirGuiaRequest.model_validate({"dicionario_itens": {"1": CDA, "01": GUIA}})


def test_chave_nao_numerica_e_preservada():
    requisicao = EmitirGuiaRequest.model_validate({"dicionario_itens": {CDA: GUIA}})

    assert requisicao.dicionario_itens == {CDA: GUIA}


def test_formatar_erro_validacao_remove_prefixo_do_pydantic():
    with pytest.raises(ValidationError) as exc:
        EmitirGuiaRequest.model_validate({"lista_cdas": PLACEHOLDER})

    mensagem = formatar_erro_validacao(exc.value)

    assert mensagem.startswith("Parâmetros inválidos. ")
    assert "Value error," not in mensagem
    assert "lista_cdas: Campo 'lista_cdas' contém placeholder" in mensagem


# ===== Montagem do payload da PGM =====


def test_montar_parametros_a_vista_separa_cdas_e_efs():
    requisicao = EmitirGuiaRequest.model_validate(
        {
            "dicionario_itens": {"1": CDA, "2": "02/000002/2024-00"},
            "lista_cdas": [CDA],
            "lista_efs": ["02/000002/2024-00"],
            "itens_informados": ["1", "2"],
        }
    )

    assert montar_parametros_entrada(requisicao, "a_vista") == {
        "origem_solicitação": 0,
        "cdas": [CDA],
        "efs": ["02/000002/2024-00"],
    }


def test_montar_parametros_regularizacao_usa_guias():
    requisicao = EmitirGuiaRequest.model_validate(
        {
            "dicionario_itens": {"1": GUIA},
            "lista_guias": [GUIA],
            "itens_informados": ["1"],
        }
    )

    assert montar_parametros_entrada(requisicao, "regularizacao") == {
        "origem_solicitação": 0,
        "guias": [GUIA],
    }


def test_sequencial_inexistente_nao_resolve_identificador():
    """A montagem segue pura e devolve listas vazias; quem recusa é `_emitir`."""
    requisicao = EmitirGuiaRequest.model_validate(
        {"dicionario_itens": {"1": CDA}, "lista_cdas": [CDA], "itens_informados": ["9"]}
    )

    assert montar_parametros_entrada(requisicao, "a_vista") == {
        "origem_solicitação": 0,
        "cdas": [],
        "efs": [],
    }


def test_sequencial_descartado_gera_warning(logger_spy):
    requisicao = EmitirGuiaRequest.model_validate(
        {"dicionario_itens": {"1": CDA}, "lista_cdas": [CDA], "itens_informados": ["9"]}
    )

    montar_parametros_entrada(requisicao, "a_vista")

    assert any(
        registro.get("event") == "emitir_guia_v2_sequenciais_descartados"
        and registro.get("sequenciais") == ["9"]
        for registro in logger_spy["warning"]
    )


def test_identificador_fora_das_listas_e_descartado():
    """Sequencial existe no dicionário, mas o valor não consta em cda/ef."""
    requisicao = EmitirGuiaRequest.model_validate(
        {"dicionario_itens": {"1": CDA}, "lista_cdas": [], "itens_informados": ["1"]}
    )

    assert montar_parametros_entrada(requisicao, "a_vista") == {
        "origem_solicitação": 0,
        "cdas": [],
        "efs": [],
    }


# ===== Emissão ponta a ponta com a PGM mockada =====


@pytest.fixture
def pgm_mock(monkeypatch):
    """Substitui pgm_api e registra as chamadas feitas."""
    chamadas = []
    resposta = {
        "valor": [
            {
                "codigoDeBarras": "8360000000",
                "pdf": "https://pgm.example/guia.pdf",
                "dataVencimento": "2026-09-01",
                "codigoQrEMVPix": "00020126",
            }
        ]
    }

    async def fake_pgm_api(endpoint, consumidor, data):
        chamadas.append({"endpoint": endpoint, "consumidor": consumidor, "data": data})
        return resposta["valor"]

    monkeypatch.setattr(service_module, "pgm_api", fake_pgm_api)
    return chamadas


@pytest.mark.asyncio
async def test_emitir_a_vista_caminho_feliz(pgm_mock):
    resultado = await emitir_guia_a_vista_v2(dict(PAYLOAD_LEGADO))

    assert resultado["api_resposta_sucesso"] is True
    assert resultado["cdas"] == [CDA]
    assert resultado["codigo_de_barras"] == "8360000000"
    assert resultado["link"] == "https://pgm.example/guia.pdf"
    assert resultado["data_vencimento"] == "2026-09-01"
    assert resultado["pix"] == "00020126"
    assert "api_descricao_erro" not in resultado

    assert pgm_mock[0]["endpoint"] == "v2/guiapagamento/emitir/avista"
    assert pgm_mock[0]["consumidor"] == "emitir-guia-vista"


@pytest.mark.asyncio
async def test_emitir_regularizacao_usa_endpoint_proprio(pgm_mock):
    resultado = await emitir_guia_regularizacao_v2(
        {
            "dicionario_itens": {"1": GUIA},
            "lista_guias": [GUIA],
            "apenas_um_item": 1,
        }
    )

    assert resultado["api_resposta_sucesso"] is True
    assert resultado["guias"] == [GUIA]
    assert pgm_mock[0]["endpoint"] == "v2/guiapagamento/emitir/regularizacao"
    assert pgm_mock[0]["consumidor"] == "emitir-guia-regularizacao"


@pytest.mark.asyncio
async def test_erro_de_validacao_nao_chama_a_pgm(pgm_mock):
    resultado = await emitir_guia_a_vista_v2({"lista_cdas": PLACEHOLDER})

    assert resultado["api_resposta_sucesso"] is False
    assert "placeholder de template não renderizado" in resultado["api_descricao_erro"]
    assert "lista_cdas" in resultado["api_descricao_erro"]
    assert pgm_mock == [], "a PGM não pode ser chamada com entrada inválida"


# ===== Report ao error interceptor =====


@pytest.mark.asyncio
async def test_placeholder_e_reportado_ao_interceptor(pgm_mock, interceptor_mock):
    payload = {"lista_cdas": PLACEHOLDER}

    await emitir_guia_a_vista_v2(dict(payload))

    assert len(interceptor_mock) == 1
    report = interceptor_mock[0]
    assert report["error_type"] == "ValidationError"
    assert report["source"]["tool"] == "divida_ativa_v2"
    assert report["source"]["function"] == "emitir_guia_a_vista_v2"
    assert "placeholder de template não renderizado" in report["error_message"]
    assert report["input_body"] == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_invalido",
    [
        {"lista_cdas": '["01/225716/2024-00"'},  # JSON malformado
        {"dicionario_itens": '["nao", "e", "dict"]'},  # tipo errado
    ],
)
async def test_toda_falha_de_validacao_e_reportada(
    pgm_mock, interceptor_mock, payload_invalido
):
    resultado = await emitir_guia_a_vista_v2(payload_invalido)

    assert resultado["api_resposta_sucesso"] is False
    assert pgm_mock == []
    assert [r["error_type"] for r in interceptor_mock] == ["ValidationError"]


@pytest.mark.asyncio
async def test_caminho_feliz_nao_reporta_nada(pgm_mock, interceptor_mock):
    await emitir_guia_a_vista_v2(dict(PAYLOAD_LEGADO))

    assert interceptor_mock == []


# ===== Seleção vazia =====


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,motivo",
    [
        ({}, "payload sem nenhum campo"),
        (
            {
                "dicionario_itens": {"1": CDA},
                "lista_cdas": [CDA],
                "apenas_um_item": "9",
            },
            "sequencial fora do dicionario_itens",
        ),
        (
            {"dicionario_itens": {"1": CDA}, "lista_cdas": [], "apenas_um_item": "1"},
            "identificador fora das listas",
        ),
        (
            {
                "dicionario_itens": {"1": CDA},
                "lista_cdas": [CDA],
                "itens_informado": "1",  # campo renomeado, absorvido por extra=ignore
            },
            "campo renomeado pelo consumidor",
        ),
    ],
)
async def test_selecao_vazia_nao_chama_a_pgm(
    pgm_mock, interceptor_mock, payload, motivo
):
    resultado = await emitir_guia_a_vista_v2(payload)

    assert resultado["api_resposta_sucesso"] is False, motivo
    assert resultado["api_descricao_erro"] == MENSAGEM_SELECAO_VAZIA
    assert pgm_mock == [], f"PGM não pode ser chamada: {motivo}"
    assert [r["error_type"] for r in interceptor_mock] == ["SelecaoVaziaError"]


@pytest.mark.asyncio
async def test_selecao_vazia_na_regularizacao_tambem_e_recusada(
    pgm_mock, interceptor_mock
):
    resultado = await emitir_guia_regularizacao_v2(
        {"dicionario_itens": {"1": GUIA}, "lista_guias": [], "apenas_um_item": "1"}
    )

    assert resultado["api_resposta_sucesso"] is False
    assert resultado["api_descricao_erro"] == MENSAGEM_SELECAO_VAZIA
    assert pgm_mock == []
    assert interceptor_mock[0]["source"]["function"] == "emitir_guia_regularizacao_v2"


@pytest.mark.asyncio
async def test_resolucao_parcial_emite_a_guia_dos_itens_validos(
    pgm_mock, interceptor_mock
):
    """Um sequencial inválido no meio não pode derrubar a emissão inteira."""
    resultado = await emitir_guia_a_vista_v2(
        {
            "dicionario_itens": {"1": CDA},
            "lista_cdas": [CDA],
            "itens_informados": ["1", "9"],
        }
    )

    assert resultado["api_resposta_sucesso"] is True
    assert resultado["cdas"] == [CDA]
    assert len(pgm_mock) == 1
    assert interceptor_mock == []


@pytest.mark.asyncio
async def test_erro_da_pgm_vira_resposta_de_falha(monkeypatch):
    async def fake_pgm_api(endpoint, consumidor, data):
        return {"erro": True, "motivos": "Não há parcelas em atraso"}

    monkeypatch.setattr(service_module, "pgm_api", fake_pgm_api)

    resultado = await emitir_guia_a_vista_v2(dict(PAYLOAD_LEGADO))

    assert resultado["api_resposta_sucesso"] is False
    assert resultado["api_descricao_erro"] == "Não há parcelas em atraso"


@pytest.mark.asyncio
async def test_excecao_inesperada_da_pgm_e_convertida_em_falha(monkeypatch):
    async def fake_pgm_api(endpoint, consumidor, data):
        raise RuntimeError("conexão recusada")

    monkeypatch.setattr(service_module, "pgm_api", fake_pgm_api)

    resultado = await emitir_guia_a_vista_v2(dict(PAYLOAD_LEGADO))

    assert resultado["api_resposta_sucesso"] is False
    assert "conexão recusada" in resultado["api_descricao_erro"]


@pytest.mark.asyncio
async def test_excecao_na_regularizacao_e_convertida_em_falha(monkeypatch):
    async def fake_pgm_api(endpoint, consumidor, data):
        raise RuntimeError("timeout na PGM")

    monkeypatch.setattr(service_module, "pgm_api", fake_pgm_api)

    resultado = await emitir_guia_regularizacao_v2(
        {"dicionario_itens": {"1": GUIA}, "lista_guias": [GUIA], "apenas_um_item": "1"}
    )

    assert resultado["api_resposta_sucesso"] is False
    assert "Erro ao emitir guia de regularização" in resultado["api_descricao_erro"]
    assert "timeout na PGM" in resultado["api_descricao_erro"]


@pytest.mark.asyncio
async def test_motivo_nulo_da_pgm_vira_mensagem_padrao(monkeypatch):
    """Sem o fallback, exclude_none removeria api_descricao_erro da resposta."""

    async def fake_pgm_api(endpoint, consumidor, data):
        return {"erro": True, "motivos": None}

    monkeypatch.setattr(service_module, "pgm_api", fake_pgm_api)

    resultado = await emitir_guia_a_vista_v2(dict(PAYLOAD_LEGADO))

    assert resultado["api_resposta_sucesso"] is False
    assert resultado["api_descricao_erro"] == "Erro na PGM"


@pytest.mark.asyncio
async def test_multiplos_registros_devolvem_todas_as_guias(monkeypatch):
    """
    CHATR-164: o EPGM emite uma guia por natureza de débito.

    Antes, o loop de extração sobrescrevia os mesmos campos e só a última guia
    chegava ao consumidor — o cidadão pagava uma achando que quitou todas.
    """

    async def fake_pgm_api(endpoint, consumidor, data):
        return [
            {
                "codigoDeBarras": "111",
                "pdf": "a.pdf",
                "dataVencimento": "10/04/2026",
                "codigoQrEMVPix": "pix-1",
            },
            {
                "codigoDeBarras": "222",
                "pdf": "b.pdf",
                "dataVencimento": "11/04/2026",
                "codigoQrEMVPix": "pix-2",
            },
        ]

    monkeypatch.setattr(service_module, "pgm_api", fake_pgm_api)

    resultado = await emitir_guia_a_vista_v2(dict(PAYLOAD_LEGADO))

    assert resultado["total_guias"] == 2
    # `id` vazio e `valor` nulo: o fake não tem GUID no link nem PIX parseável.
    assert resultado["guias_emitidas"] == [
        {
            "id": "",
            "codigo_de_barras": "111",
            "link": "a.pdf",
            "data_vencimento": "10/04/2026",
            "pix": "pix-1",
            "valor": None,
        },
        {
            "id": "",
            "codigo_de_barras": "222",
            "link": "b.pdf",
            "data_vencimento": "11/04/2026",
            "pix": "pix-2",
            "valor": None,
        },
    ]
    # Campos legado: a primeira guia, não a última.
    assert resultado["codigo_de_barras"] == "111"
    assert resultado["pix"] == "pix-1"


@pytest.mark.asyncio
async def test_registro_unico_fora_de_lista_vira_uma_guia(monkeypatch):
    """A PGM devolvendo o registro solto não pode virar resposta sem guia."""

    async def fake_pgm_api(endpoint, consumidor, data):
        return {"codigoDeBarras": "111", "pdf": "a.pdf"}

    monkeypatch.setattr(service_module, "pgm_api", fake_pgm_api)

    resultado = await emitir_guia_a_vista_v2(dict(PAYLOAD_LEGADO))

    assert resultado["api_resposta_sucesso"] is True
    assert resultado["total_guias"] == 1
    assert resultado["codigo_de_barras"] == "111"


@pytest.mark.asyncio
async def test_resposta_vazia_da_pgm_nao_vira_guia_em_branco(monkeypatch):
    """`pgm_api` devolve {"success": True} quando a PGM não retorna nada."""

    async def fake_pgm_api(endpoint, consumidor, data):
        return {"success": True}

    monkeypatch.setattr(service_module, "pgm_api", fake_pgm_api)

    resultado = await emitir_guia_a_vista_v2(dict(PAYLOAD_LEGADO))

    assert resultado["api_resposta_sucesso"] is False
    assert resultado["api_descricao_erro"] == MENSAGEM_SEM_GUIA


@pytest.mark.asyncio
async def test_resposta_sem_registros_vira_erro(monkeypatch, logger_spy):
    """Sucesso sem guia nenhuma deixaria o cidadão sem nada para pagar."""

    async def fake_pgm_api(endpoint, consumidor, data):
        return []

    monkeypatch.setattr(service_module, "pgm_api", fake_pgm_api)

    resultado = await emitir_guia_a_vista_v2(dict(PAYLOAD_LEGADO))

    assert resultado["api_resposta_sucesso"] is False
    assert resultado["api_descricao_erro"] == MENSAGEM_SEM_GUIA
    assert any(
        registro.get("event") == "emitir_guia_v2_sem_guia"
        for registro in logger_spy["error"]
    )


@pytest.mark.asyncio
async def test_regularizacao_nao_devolve_campos_de_a_vista(pgm_mock):
    """Regressão: a resposta é montada campo a campo, não por **unpacking."""
    resultado = await emitir_guia_regularizacao_v2(
        {"dicionario_itens": {"1": GUIA}, "lista_guias": [GUIA], "apenas_um_item": "1"}
    )

    assert resultado["api_resposta_sucesso"] is True
    assert resultado["guias"] == [GUIA]
    assert "cdas" not in resultado
    assert "efs" not in resultado


@pytest.mark.asyncio
async def test_item_nao_dict_na_resposta_da_pgm_e_ignorado(monkeypatch):
    async def fake_pgm_api(endpoint, consumidor, data):
        return [
            "ruído",
            {"codigoDeBarras": "8360000000", "pdf": "", "dataVencimento": ""},
        ]

    monkeypatch.setattr(service_module, "pgm_api", fake_pgm_api)

    resultado = await emitir_guia_a_vista_v2(dict(PAYLOAD_LEGADO))

    assert resultado["api_resposta_sucesso"] is True
    assert resultado["codigo_de_barras"] == "8360000000"


# ===== Modelo de resposta =====


def test_response_de_erro_omite_campos_nao_preenchidos():
    resposta = EmitirGuiaResponse.de_erro("Parâmetros inválidos.")

    assert resposta.para_dict() == {
        "api_resposta_sucesso": False,
        "api_descricao_erro": "Parâmetros inválidos.",
    }


def test_response_de_sucesso_mantem_contrato_da_v1():
    resposta = EmitirGuiaResponse(
        api_resposta_sucesso=True,
        **{"origem_solicitação": 0},
        cdas=[CDA],
        efs=[],
        codigo_de_barras="8360000000",
        link="https://pgm.example/guia.pdf",
        data_vencimento="2026-09-01",
        pix="00020126",
    )

    assert resposta.para_dict() == {
        "api_resposta_sucesso": True,
        "origem_solicitação": 0,
        "cdas": [CDA],
        "efs": [],
        "codigo_de_barras": "8360000000",
        "link": "https://pgm.example/guia.pdf",
        "data_vencimento": "2026-09-01",
        "pix": "00020126",
    }


def test_response_rejeita_campo_desconhecido():
    with pytest.raises(ValidationError):
        EmitirGuiaResponse(api_resposta_sucesso=True, campo_inventado="x")
