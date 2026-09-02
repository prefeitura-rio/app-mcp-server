"""Testes da tool de line-up do Rock in Rio (CHATR-187).

O grosso daqui é semântica de data. "Hoje" é ambíguo neste evento por dois
motivos que se somam: a programação de um dia avança pela madrugada do dia
seguinte, e o festival tem um intervalo (08 a 10 de setembro) em que ele não
terminou, mas também não há show. Cada um desses casos tem um teste próprio.
"""

from datetime import datetime

import pytest

from src.tools.rock_in_rio import tool as tool_mod
from src.tools.rock_in_rio.cache import LineupCarregado, LineupIndisponivel
from src.tools.rock_in_rio.tool import (
    APP_OFICIAL,
    ASSUNTOS_DE_APOIO,
    CONTEXTO_PADRAO,
    FRASE_POR_CONTEXTO,
    RAG_DE_APOIO,
    _normalizar_contexto,
    _situacao_temporal,
    get_rock_in_rio_lineup,
)
from src.utils.datetime_utils import get_rio_timezone

# Mesma forma que `Show.para_resposta` produz: sem `url` nem `dia_slug`.
SHOWS = [
    {
        "data": "2026-09-04",
        "palco": "Palco Mundo",
        "artista": "FOO FIGHTERS",
        "slug": "foo-fighters",
    },
    {
        "data": "2026-09-13",
        "palco": "Palco Sunset",
        "artista": "IVETE SANGALO",
        "slug": "ivete-sangalo",
    },
]


def _momento(ano, mes, dia, hora, minuto=0) -> datetime:
    return get_rio_timezone().localize(datetime(ano, mes, dia, hora, minuto))


def test_antes_do_festival_aponta_o_primeiro_dia():
    situacao = _situacao_temporal(_momento(2026, 8, 28, 14))

    assert situacao["status"] == "antes_do_festival"
    assert situacao["hoje_tem_show"] is False
    assert situacao["proximo_dia_com_show"]["data"] == "2026-09-04"
    assert situacao["data_de_hoje"] == "2026-08-28"


def test_dia_de_show_a_noite():
    situacao = _situacao_temporal(_momento(2026, 9, 4, 20))

    assert situacao["status"] == "durante_o_festival"
    assert situacao["hoje_tem_show"] is True
    assert situacao["jornada_de_referencia"] == "2026-09-04"
    assert situacao["proximo_dia_com_show"]["data"] == "2026-09-05"
    assert "observacao_jornada" not in situacao


def test_madrugada_ainda_pertence_a_programacao_da_vespera():
    """Às 2h de sábado, o que está rolando é a programação de sexta.

    Sem esse deslocamento, quem pergunta de dentro da Cidade do Rock de
    madrugada — o público mais provável naquele horário — receberia a resposta
    errada.
    """
    situacao = _situacao_temporal(_momento(2026, 9, 5, 2))

    assert situacao["data_de_hoje"] == "2026-09-05"
    assert situacao["jornada_de_referencia"] == "2026-09-04"
    assert situacao["hoje_tem_show"] is True
    assert "observacao_jornada" in situacao
    assert "04/09/2026" in situacao["observacao_jornada"]


def test_madrugada_fora_do_festival_nao_inventa_programacao():
    """`jornada != hoje` é verdade em toda madrugada, inclusive fora do evento.

    Sem o filtro por dia de show, às 3h de 30/08 a resposta afirmava que "as
    atrações em andamento são as do dia 29/08" — uma data em que o festival nem
    tinha começado.
    """
    situacao = _situacao_temporal(_momento(2026, 8, 30, 3))

    assert situacao["status"] == "antes_do_festival"
    assert situacao["hoje_tem_show"] is False
    assert "observacao_jornada" not in situacao


def test_madrugada_no_intervalo_nao_contradiz_o_hoje_tem_show():
    """Às 3h de 09/09 a jornada de referência é 08/09 — dia sem programação.

    O payload chegava a carregar duas frases opostas: "hoje não há programação"
    e "as atrações em andamento são as do dia 08/09".
    """
    situacao = _situacao_temporal(_momento(2026, 9, 9, 3))

    assert situacao["hoje_tem_show"] is False
    assert "observacao_jornada" not in situacao
    assert "intervalo" in situacao["observacao"]


def test_madrugada_depois_do_festival_nao_inventa_programacao():
    situacao = _situacao_temporal(_momento(2027, 1, 10, 3))

    assert situacao["status"] == "encerrado"
    assert "observacao_jornada" not in situacao


def test_intervalo_no_meio_do_festival():
    """09 de setembro: o festival não terminou, mas não há show."""
    situacao = _situacao_temporal(_momento(2026, 9, 9, 15))

    assert situacao["status"] == "durante_o_festival"
    assert situacao["hoje_tem_show"] is False
    assert situacao["proximo_dia_com_show"]["data"] == "2026-09-11"
    assert "intervalo" in situacao["observacao"]


def test_madrugada_do_ultimo_dia_ainda_e_festival():
    """05h de 14/09 ainda é a madrugada do dia 13."""
    situacao = _situacao_temporal(_momento(2026, 9, 14, 5))

    assert situacao["status"] == "durante_o_festival"
    assert situacao["jornada_de_referencia"] == "2026-09-13"
    assert situacao["hoje_tem_show"] is True
    assert situacao["proximo_dia_com_show"] is None


def test_depois_do_festival_responde_encerrado():
    situacao = _situacao_temporal(_momento(2026, 9, 14, 7))

    assert situacao["status"] == "encerrado"
    assert situacao["hoje_tem_show"] is False
    assert situacao["proximo_dia_com_show"] is None
    assert "encerrada" in situacao["observacao"]


def test_muito_depois_do_festival_continua_encerrado():
    situacao = _situacao_temporal(_momento(2027, 1, 10, 12))

    assert situacao["status"] == "encerrado"
    assert situacao["proximo_dia_com_show"] is None


@pytest.fixture
def lineup_ok(monkeypatch):
    async def falso_obter_lineup(**_):
        return LineupCarregado(SHOWS, __import__("time").time() - 120, "memoria")

    monkeypatch.setattr(tool_mod, "obter_lineup", falso_obter_lineup)


@pytest.mark.asyncio
async def test_resposta_traz_a_grade_e_os_links_do_app(lineup_ok):
    resposta = await get_rock_in_rio_lineup()

    assert resposta["disponivel"] is True
    assert resposta["total_de_atracoes"] == 2
    assert resposta["shows"] == SHOWS
    assert resposta["app_oficial"] == APP_OFICIAL
    assert "play.google.com" in resposta["app_oficial"]["android"]
    assert "apps.apple.com" in resposta["app_oficial"]["ios"]


@pytest.mark.asyncio
async def test_cada_show_traz_so_o_que_a_resposta_precisa(lineup_ok):
    """Campo derivável repetido 156 vezes é contexto gasto à toa.

    `url` sai do `slug` e `dia_slug` sai da `data`; juntos custavam ~13 KB por
    chamada sem responder nada que a tool se proponha a responder. O critério de
    aceite pede dia, palco, artista e slug — é exatamente o que fica.
    """
    resposta = await get_rock_in_rio_lineup()

    for show in resposta["shows"]:
        assert set(show) == {"data", "palco", "artista", "slug"}


@pytest.mark.asyncio
async def test_resposta_declara_que_nao_ha_horarios(lineup_ok):
    """O risco número um desta tool é o modelo inventar horário de show."""
    resposta = await get_rock_in_rio_lineup()

    assert resposta["horarios"]["disponiveis"] is False
    assert "aplicativo oficial" in resposta["horarios"]["onde_consultar"].lower()
    assert "NÃO informe" in resposta["horarios"]["aviso"]
    assert "horário" in resposta["instrucoes_de_resposta"]

    # Nenhuma atração pode carregar campo de hora: não existe na fonte.
    for show in resposta["shows"]:
        assert not any("hor" in chave.lower() for chave in show)


@pytest.mark.asyncio
async def test_resposta_lista_os_sete_dias_e_os_palcos(lineup_ok):
    resposta = await get_rock_in_rio_lineup()

    dias = resposta["evento"]["dias"]
    assert len(dias) == 7
    assert dias[0]["data"] == "2026-09-04"
    assert dias[0]["data_br"] == "04/09/2026"
    assert dias[-1]["data"] == "2026-09-13"
    assert resposta["evento"]["palcos"] == ["Palco Mundo", "Palco Sunset"]


@pytest.mark.asyncio
async def test_resposta_informa_a_procedencia_do_dado(lineup_ok):
    resposta = await get_rock_in_rio_lineup()

    assert resposta["atualizado_em"]["origem"] == "memoria"
    assert 100 <= resposta["atualizado_em"]["ha_segundos"] <= 200


@pytest.mark.asyncio
async def test_indisponibilidade_nao_entrega_grade_alguma(monkeypatch):
    """Preferimos não entregar a entregar errado.

    A resposta de falha não pode conter nada que o modelo consiga apresentar
    como programação — só o aviso e o app oficial.
    """

    async def explode(**_):
        raise LineupIndisponivel("fonte fora do ar e cache vencido")

    monkeypatch.setattr(tool_mod, "obter_lineup", explode)

    resposta = await get_rock_in_rio_lineup()

    assert resposta["disponivel"] is False
    assert "shows" not in resposta
    assert "NÃO contém line-up" in resposta["instrucoes_de_resposta"]
    assert resposta["app_oficial"] == APP_OFICIAL


@pytest.mark.asyncio
async def test_erro_inesperado_tambem_degrada_com_seguranca(monkeypatch):
    async def explode(**_):
        raise ValueError("algo que ninguém previu")

    monkeypatch.setattr(tool_mod, "obter_lineup", explode)

    resposta = await get_rock_in_rio_lineup()

    assert resposta["disponivel"] is False
    assert "shows" not in resposta
    assert resposta["app_oficial"] == APP_OFICIAL


def test_descricao_da_tool_avisa_que_nao_tem_horario():
    """O modelo precisa saber o que a tool não entrega antes de chamá-la."""
    descricao = tool_mod.descricao_da_tool("vTESTE")

    assert "vTESTE" in descricao
    assert "NÃO devolve horários" in descricao
    assert "aplicativo oficial" in descricao


# ----- contexto da pergunta -----


@pytest.mark.parametrize(
    "recebido, esperado",
    [
        ("hora", "hora"),
        ("data", "data"),
        ("banda", "banda"),
        ("palco", "palco"),
        ("outro", CONTEXTO_PADRAO),
        (None, CONTEXTO_PADRAO),
        ("horarios", CONTEXTO_PADRAO),
    ],
)
def test_contexto_fora_da_taxonomia_cai_no_padrao(recebido, esperado):
    """Classificação errada não pode custar a resposta do cidadão.

    Inclui um valor que o schema rejeitaria (`"horarios"`) porque a função é
    chamada direto por testes e pelo runner e2e, sem a validação do FastMCP no
    caminho.
    """
    assert _normalizar_contexto(recebido) == esperado


@pytest.mark.asyncio
async def test_caminho_feliz_ecoa_o_contexto(lineup_ok):
    resposta = await get_rock_in_rio_lineup(contexto="palco")

    assert resposta["contexto"] == "palco"
    # Botão é só do caminho degradado: aqui o cidadão pediu line-up e recebeu
    # line-up, e três botões de outro assunto mudariam de conversa sem ele pedir.
    assert "payload_schema" not in resposta


@pytest.mark.asyncio
async def test_sem_contexto_o_caminho_feliz_ecoa_o_padrao(lineup_ok):
    resposta = await get_rock_in_rio_lineup()

    assert resposta["contexto"] == CONTEXTO_PADRAO


@pytest.fixture
def lineup_fora_do_ar(monkeypatch):
    async def explode(**_):
        raise LineupIndisponivel("fonte fora do ar e cache vencido")

    monkeypatch.setattr(tool_mod, "obter_lineup", explode)


@pytest.mark.asyncio
async def test_indisponivel_fala_por_frase_generica_e_nao_pelo_termo_do_cidadao(
    lineup_fora_do_ar,
):
    """O vetor aqui é o cidadão citar uma banda inexistente (ou maliciosa).

    Se a frase fosse montada pelo modelo a partir da pergunta, a resposta
    acabaria mandando o cidadão procurar aquele nome no site e no app oficial.
    A frase sai desta tabela fechada, e nenhuma entrada dela tem nome próprio.
    """
    resposta = await get_rock_in_rio_lineup(contexto="banda")

    assert resposta["contexto"] == "banda"
    assert resposta["contexto_frase"] == FRASE_POR_CONTEXTO["banda"]
    assert resposta["contexto_frase"] in resposta["instrucoes_de_resposta"]

    instrucoes = resposta["instrucoes_de_resposta"]
    assert "não repita nomes de artistas" in instrucoes.lower()
    assert "nunca oriente o cidadão a procurar um nome específico" in instrucoes


@pytest.mark.asyncio
async def test_indisponivel_nao_admite_a_falha_para_o_cidadao(lineup_fora_do_ar):
    """Tom leve para o cidadão, proibição dura para o modelo — nesta ordem."""
    instrucoes = (await get_rock_in_rio_lineup())["instrucoes_de_resposta"]

    assert "sem mencionar erro, falha, indisponibilidade" in instrucoes
    # A suavização do tom não pode ter levado junto o freio contra inventar.
    assert "NÃO contém line-up" in instrucoes
    assert "não afirme nem negue que uma atração está no festival" in instrucoes


@pytest.mark.asyncio
async def test_indisponivel_traz_os_tres_botoes_no_formato_do_renderizador(
    lineup_fora_do_ar,
):
    schema = (await get_rock_in_rio_lineup())["payload_schema"]

    # `x-render` no campo e na raiz: é como o fluxo da dívida ativa publica, e
    # é o que o renderizador do chat já consome.
    assert schema["x-render"] == "buttons"
    campo = schema["properties"]["assunto"]
    assert campo["x-render"] == "buttons"
    assert schema["required"] == ["assunto"]

    assert campo["enum"] == [assunto["value"] for assunto in ASSUNTOS_DE_APOIO]
    assert [opcao["label"] for opcao in campo["options"]] == [
        "Transporte",
        "Alimentação",
        "Emergência",
    ]

    # Três é o teto de `buttons`; a partir daí o renderizador pede `list`.
    assert len(campo["options"]) == 3

    for opcao in campo["options"]:
        # Rótulo é o que aparece na tela, e o maior em uso hoje tem 22
        # caracteres. O domínio inteiro fica no `value`, que é o que o modelo lê.
        assert len(opcao["label"]) <= 22
        assert opcao["description"]
        assert len(opcao["value"]) > len(opcao["label"])


@pytest.mark.asyncio
async def test_indisponivel_manda_o_clique_para_o_rag(lineup_fora_do_ar):
    instrucoes = (await get_rock_in_rio_lineup())["instrucoes_de_resposta"]

    assert RAG_DE_APOIO in instrucoes
    assert "conhecimento próprio" in instrucoes


def test_descricao_da_tool_repete_o_roteamento_do_clique():
    """O clique acontece num turno em que o retorno pode já ter saído da janela."""
    descricao = tool_mod.descricao_da_tool("vTESTE")

    assert RAG_DE_APOIO in descricao
    assert "`contexto`" in descricao
