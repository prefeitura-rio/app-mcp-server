"""Testes da tool do mapa da Cidade do Rock.

Duas regras sustentam este módulo, e é o que estes testes protegem:

1. **O link é um só, encurtado uma vez por evento.** Nada nele depende de quem
   perguntou, então o caminho normal de uma chamada é ler do Redis e não falar
   com o encurtador. Um teste que deixasse isso passar transformaria cada
   atendimento num link novo.
2. **Encurtador fora do ar não derruba a resposta.** O cidadão recebe a URL
   longa, que funciona, em vez de nada — mesmo desenho de fallback do PDF do
   DARM (CHATR-176).
"""

import datetime as dt

import pytest

import src.utils.bigquery as bigquery_mod
from src.tools.rock_in_rio import mapa as mapa_mod
from src.tools.rock_in_rio.mapa import URL_DO_MAPA, get_mapa_rock_in_rio
from src.tools.rock_in_rio.tool import limite_do_festival
from src.utils.datetime_utils import get_rio_timezone

LINK_CURTO = "https://pref.rio/link/mapa-rock-in-rio"


def _rio(ano: int, mes: int, dia: int, hora: int = 12) -> dt.datetime:
    """Datetime no fuso do Rio, via `localize` — nunca `replace(tzinfo=...)`."""
    return get_rio_timezone().localize(dt.datetime(ano, mes, dia, hora))


# Durante o festival, e antes do limite: é o cenário de quase todo teste aqui.
DURANTE = _rio(2026, 9, 3, 12)


def _congelar(monkeypatch, agora: dt.datetime) -> None:
    """Fixa o `datetime.now` de `mapa.py`.

    Substituir o nome no namespace do módulo é seguro porque `mapa.py` tem
    `from __future__ import annotations`: as anotações `datetime` não são
    avaliadas em runtime, e a única outra referência é a chamada a `.now()`.
    """

    class DatetimeFalso:
        @staticmethod
        def now(tz=None):
            return agora

    monkeypatch.setattr(mapa_mod, "datetime", DatetimeFalso)


class EncurtadorFalso:
    """Substitui `get_short_url`, contando chamadas e guardando os argumentos."""

    def __init__(self, retorno: str | None = LINK_CURTO):
        self.chamadas = 0
        self.kwargs: dict = {}
        self.retorno = retorno

    async def __call__(self, **kwargs):
        self.chamadas += 1
        self.kwargs = kwargs
        return self.retorno


class RedisFalso:
    """Dublê dos dois helpers de Redis, sem passar pelo cliente real."""

    def __init__(self, valor: str | None = None):
        self.valor = valor
        self.gravacoes: list[tuple[str, int]] = []

    async def ler(self):
        return self.valor

    async def gravar(self, link: str, ttl_s: int):
        self.gravacoes.append((link, ttl_s))


@pytest.fixture
def redis(monkeypatch):
    dublê = RedisFalso()
    monkeypatch.setattr(mapa_mod, "_ler_redis", dublê.ler)
    monkeypatch.setattr(mapa_mod, "_gravar_redis", dublê.gravar)
    return dublê


@pytest.fixture
def encurtador(monkeypatch):
    dublê = EncurtadorFalso()
    monkeypatch.setattr(mapa_mod, "get_short_url", dublê)
    # Sem isto o caminho de fallback faria um GET de verdade em pref.rio: teste
    # unitário não vai à rede. Quem exercita a confirmação sobrescreve.
    monkeypatch.setattr(mapa_mod, "_link_ja_publicado", _sem_link_publicado)
    return dublê


async def _sem_link_publicado():
    return None


@pytest.mark.asyncio
async def test_hit_no_redis_nao_chama_o_encurtador(monkeypatch, redis, encurtador):
    _congelar(monkeypatch, DURANTE)
    redis.valor = LINK_CURTO

    resposta = await get_mapa_rock_in_rio()

    assert encurtador.chamadas == 0
    assert redis.gravacoes == []
    assert LINK_CURTO in resposta["mensagem"]
    assert resposta["disponivel"] is True


@pytest.mark.asyncio
async def test_miss_encurta_uma_vez_e_grava_com_ttl_ate_o_fim_do_festival(
    monkeypatch, redis, encurtador
):
    _congelar(monkeypatch, DURANTE)

    resposta = await get_mapa_rock_in_rio()

    assert encurtador.chamadas == 1
    assert LINK_CURTO in resposta["mensagem"]

    ((link, ttl_s),) = redis.gravacoes
    assert link == LINK_CURTO
    # O TTL do Redis e o vencimento do link curto saem do MESMO instante: se um
    # dia divergirem, o cache serve um link já morto até o TTL vencer.
    assert ttl_s == int((limite_do_festival() - DURANTE).total_seconds())
    assert ttl_s > 0


@pytest.mark.asyncio
async def test_parametros_enviados_ao_encurtador(monkeypatch, redis, encurtador):
    _congelar(monkeypatch, DURANTE)

    await get_mapa_rock_in_rio()

    kwargs = encurtador.kwargs
    assert kwargs["url"] == URL_DO_MAPA
    assert kwargs["title"] == "mapa_rock_in_rio"
    assert kwargs["description"] == "Mapa do Rock In Rio"
    assert kwargs["short_path"] == "mapa-rock-in-rio"
    assert kwargs["user_id"] == "sistema"
    # O preview do card no WhatsApp aponta para a própria imagem.
    assert kwargs["image_url"] == URL_DO_MAPA
    # `format_expires_at` exige tzinfo e normaliza para UTC com sufixo Z; o
    # instante é o mesmo do TTL do Redis.
    assert kwargs["expires_at"] == limite_do_festival().astimezone(
        dt.timezone.utc
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_encurtador_fora_do_ar_entrega_url_crua_e_nao_cacheia(
    monkeypatch, redis, encurtador
):
    _congelar(monkeypatch, DURANTE)
    encurtador.retorno = None

    resposta = await get_mapa_rock_in_rio()

    assert resposta["disponivel"] is True
    assert URL_DO_MAPA in resposta["mensagem"]
    # Nada cacheado: a próxima chamada tenta encurtar de novo em vez de servir a
    # URL longa até o fim do festival.
    assert redis.gravacoes == []


@pytest.mark.asyncio
async def test_falha_no_redis_nao_derruba_a_tool(monkeypatch, encurtador):
    """Exercita o try/except real de `_ler_redis`, sem dublar os helpers."""
    _congelar(monkeypatch, DURANTE)

    async def redis_explode():
        raise RuntimeError("redis fora do ar")

    monkeypatch.setattr(bigquery_mod, "get_async_redis_client", redis_explode)

    resposta = await get_mapa_rock_in_rio()

    assert resposta["disponivel"] is True
    assert LINK_CURTO in resposta["mensagem"]


@pytest.mark.asyncio
async def test_depois_do_fim_nao_toca_em_redis_nem_encurtador(
    monkeypatch, redis, encurtador
):
    _congelar(monkeypatch, limite_do_festival() + dt.timedelta(seconds=1))
    redis.valor = LINK_CURTO

    resposta = await get_mapa_rock_in_rio()

    assert resposta["disponivel"] is False
    assert resposta["motivo"] == "evento_encerrado"
    assert encurtador.chamadas == 0
    assert redis.gravacoes == []
    # Nenhum link na resposta: o link curto expira junto com o evento, e
    # oferecer mapa aqui seria oferecer um link morto.
    assert URL_DO_MAPA not in resposta["mensagem"]
    assert "http" not in resposta["mensagem"]


@pytest.mark.asyncio
async def test_no_ultimo_dia_de_madrugada_ainda_entrega_o_mapa(
    monkeypatch, redis, encurtador
):
    """05:59 de 14/09 ainda é a jornada do dia 13 — o festival não acabou."""
    _congelar(monkeypatch, _rio(2026, 9, 14, 5))

    resposta = await get_mapa_rock_in_rio()

    assert resposta["disponivel"] is True
    assert encurtador.chamadas == 1


@pytest.mark.asyncio
async def test_conflito_no_encurtador_recupera_o_link_ja_publicado(
    monkeypatch, redis, encurtador
):
    """O 409 do `short_path` fixo não pode custar o link curto.

    Perder o Redis no meio do festival faz a re-encurtagem bater em
    `short path already exists` para sempre. Sem esta recuperação, o cidadão
    receberia a URL longa até o evento acabar mesmo com o link no ar.
    """
    _congelar(monkeypatch, DURANTE)
    encurtador.retorno = None  # é o que `get_short_url` devolve num 409

    async def publicado():
        return LINK_CURTO

    monkeypatch.setattr(mapa_mod, "_link_ja_publicado", publicado)

    resposta = await get_mapa_rock_in_rio()

    assert LINK_CURTO in resposta["mensagem"]
    assert URL_DO_MAPA not in resposta["mensagem"]
    # E volta para o cache: senão toda chamada seguinte repetiria a confirmação.
    ((link, _),) = redis.gravacoes
    assert link == LINK_CURTO


@pytest.mark.asyncio
async def test_sem_link_publicado_cai_na_url_crua(monkeypatch, redis, encurtador):
    """Encurtador fora do ar de verdade: não há link, e montar um seria pior.

    `{SHORT_API_URL}/link/{SHORT_PATH}` montada às cegas devolveria ao cidadão
    `{"error":"URL not found or expired"}` no navegador.
    """
    _congelar(monkeypatch, DURANTE)
    encurtador.retorno = None

    resposta = await get_mapa_rock_in_rio()

    assert URL_DO_MAPA in resposta["mensagem"]
    assert redis.gravacoes == []
