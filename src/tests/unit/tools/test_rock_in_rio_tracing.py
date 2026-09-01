"""Testes dos spans internos de latência do Rock in Rio (instrumentação OTel).

Usa um `TracerProvider` local com `InMemorySpanExporter` (molde de
`test_tracing_middleware.py`) para checar nome/atributos/relação pai-filho
dos cinco estágios instrumentados: leitura e escrita/fallback de cache,
busca e parse de página, e ciclo de refresh.
"""

import asyncio
import time
from datetime import date

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from src.observability import tracing
from src.tools.rock_in_rio import cache as cache_mod
from src.tools.rock_in_rio import scraper as scraper_mod
from src.tools.rock_in_rio.cache import LineupIndisponivel, obter_lineup, resetar_cache
from src.tools.rock_in_rio.scraper import LineupInvalido, Show
from src.utils.http_client import InterceptedHTTPClient

SHOW = Show(
    data="2026-09-04",
    dia_slug="04-set",
    palco="Palco Mundo",
    artista="FOO FIGHTERS",
    slug="foo-fighters",
    url="https://rockinrio.com/rio/pt-br/line-up/foo-fighters/",
)


@pytest.fixture(autouse=True)
def cache_limpo(monkeypatch):
    """Isola cada teste: sem cache de processo e sem tocar no Redis real."""
    resetar_cache()

    async def _redis_vazio():
        return None

    async def _redis_ignora(_registro):
        return None

    monkeypatch.setattr(cache_mod, "_ler_redis", _redis_vazio)
    monkeypatch.setattr(cache_mod, "_gravar_redis", _redis_ignora)
    yield
    resetar_cache()


@pytest.fixture
def span_exporter(monkeypatch):
    """Redireciona `get_tracer()` (em `tracing` e em `cache_mod`, os dois
    pontos de onde os spans o resolvem) para um provider local em memória.
    """
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(tracing, "get_tracer", lambda: tracer)
    monkeypatch.setattr(cache_mod, "get_tracer", lambda: tracer)
    return exporter


def _por_nome(spans, nome):
    return [s for s in spans if s.name == nome]


def _semear_memoria(*, idade_s: float) -> None:
    cache_mod._memoria = {
        "gerado_em_epoch": time.time() - idade_s,
        "shows": [SHOW.to_dict()],
    }


def _artista_html(slug: str, nome: str) -> str:
    return (
        f'<a href="{scraper_mod.BASE_URL}/rio/pt-br/line-up/{slug}/">'
        f'<h2 class="dest-1">{nome}<i class="fas"></i></h2></a>'
    )


def _pagina_valida() -> str:
    artistas = "".join(
        _artista_html(f"banda-{i}", f"BANDA {i}")
        for i in range(scraper_mod.MIN_ATRACOES_POR_DIA)
    )
    return (
        '<html><body><section class="resultado">'
        '<div class="data"><span>Palco Mundo</span></div>'
        f"{artistas}"
        "</section></body></html>"
    )


class _RespostaFalsa:
    def __init__(self, texto: str = "", erro: Exception | None = None):
        self.text = texto
        self._erro = erro

    def raise_for_status(self) -> None:
        if self._erro is not None:
            raise self._erro


def _sem_texto_de_excecao(span, mensagem: str) -> None:
    """Garante que nenhum atributo/descrição do span carregue a exceção."""
    assert not span.events
    assert mensagem not in str(span.attributes)
    if span.status.description:
        assert mensagem not in span.status.description


# --- cache-hit ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_gera_um_unico_span_sem_ir_a_fonte(span_exporter):
    _semear_memoria(idade_s=30)

    carregado = await obter_lineup()

    assert carregado.origem == "memoria"
    (span,) = span_exporter.get_finished_spans()
    assert span.name == "rock_in_rio.cache.read"
    assert span.parent is None
    assert span.attributes["rock_in_rio.forced"] is False
    assert span.attributes["rock_in_rio.cache.origem"] == "memoria"
    assert span.attributes["rock_in_rio.cache.stale"] is False
    assert span.attributes["rock_in_rio.show_count"] == 1
    assert span.status.status_code == StatusCode.OK


# --- busca fria (cache frio -> fetch + parse de todos os dias) --------------


@pytest.mark.asyncio
async def test_busca_fria_encadeia_leitura_escrita_busca_e_parse(
    span_exporter, monkeypatch
):
    async def get_falso(_self, _url, **_kwargs):
        return _RespostaFalsa(_pagina_valida())

    monkeypatch.setattr(InterceptedHTTPClient, "get", get_falso)

    carregado = await obter_lineup()

    assert carregado.origem == "site"

    spans = span_exporter.get_finished_spans()
    (leitura,) = _por_nome(spans, "rock_in_rio.cache.read")
    (escrita,) = _por_nome(spans, "rock_in_rio.cache.write")
    (fetch,) = _por_nome(spans, "rock_in_rio.lineup_fetch")
    buscas = _por_nome(spans, "rock_in_rio.scrape.fetch_day")
    parses = _por_nome(spans, "rock_in_rio.scrape.parse_day")

    dias = {slug for slug, _ in scraper_mod.DIAS_DO_EVENTO}
    assert len(buscas) == len(dias)
    assert len(parses) == len(dias)
    assert {s.attributes["rock_in_rio.dia_slug"] for s in buscas} == dias
    assert {s.attributes["rock_in_rio.dia_slug"] for s in parses} == dias

    # Relação pai-filho: leitura -> escrita -> lineup_fetch -> (busca, parse) de cada dia.
    assert escrita.parent.span_id == leitura.context.span_id
    assert fetch.parent.span_id == escrita.context.span_id
    assert all(s.parent.span_id == fetch.context.span_id for s in buscas)
    assert all(s.parent.span_id == fetch.context.span_id for s in parses)

    assert escrita.attributes["rock_in_rio.page_count"] == len(dias)
    # Sucesso não passou pelo caminho de fallback: o atributo nem é setado.
    assert "rock_in_rio.cache.fallback" not in escrita.attributes
    assert leitura.attributes["rock_in_rio.cache.origem"] == "site"
    for span in (leitura, escrita, *buscas, *parses):
        assert span.status.status_code == StatusCode.OK


# --- stale-fallback (fonte cai, cache vencido ainda dentro do teto) ---------


@pytest.mark.asyncio
async def test_fonte_fora_do_ar_cai_para_cache_vencido_sem_marcar_a_leitura_como_erro(
    span_exporter, monkeypatch
):
    async def fonte_quebrada():
        raise ConnectionError("erro de rede simulado")

    monkeypatch.setattr(cache_mod, "buscar_lineup", fonte_quebrada)
    _semear_memoria(idade_s=cache_mod.DEFAULT_MAX_IDADE_S - 300)

    carregado = await obter_lineup(forcar=True)

    assert carregado.origem == "cache_stale"

    spans = span_exporter.get_finished_spans()
    (leitura,) = _por_nome(spans, "rock_in_rio.cache.read")
    (escrita,) = _por_nome(spans, "rock_in_rio.cache.write")

    # A escrita falhou de verdade — status de erro é o registro correto dela,
    # mesmo a leitura tendo servido o cidadão com sucesso via fallback.
    assert escrita.status.status_code == StatusCode.ERROR
    assert escrita.attributes["error.type"] == "ConnectionError"
    assert escrita.attributes["rock_in_rio.cache.fallback"] is True
    _sem_texto_de_excecao(escrita, "erro de rede simulado")

    assert leitura.status.status_code == StatusCode.OK
    assert leitura.attributes["rock_in_rio.cache.origem"] == "cache_stale"
    assert leitura.attributes["rock_in_rio.cache.stale"] is True


@pytest.mark.asyncio
async def test_falha_total_marca_leitura_e_escrita_como_erro(
    span_exporter, monkeypatch
):
    async def fonte_quebrada():
        raise ConnectionError("fonte fora do ar")

    monkeypatch.setattr(cache_mod, "buscar_lineup", fonte_quebrada)

    with pytest.raises(LineupIndisponivel):
        await obter_lineup()

    spans = span_exporter.get_finished_spans()
    (leitura,) = _por_nome(spans, "rock_in_rio.cache.read")
    (escrita,) = _por_nome(spans, "rock_in_rio.cache.write")

    assert escrita.status.status_code == StatusCode.ERROR
    assert escrita.attributes["rock_in_rio.cache.fallback"] is False
    assert leitura.status.status_code == StatusCode.ERROR
    assert leitura.attributes["error.type"] == "LineupIndisponivel"
    _sem_texto_de_excecao(leitura, "fonte fora do ar")
    _sem_texto_de_excecao(escrita, "fonte fora do ar")


# --- falha de parse ------------------------------------------------------------


def test_pagina_malformada_marca_o_span_de_parse_como_erro(span_exporter):
    html = "<html><body>o tema do site mudou</body></html>"

    with pytest.raises(LineupInvalido):
        scraper_mod.parse_dia(html, dia_slug="04-set", data=date(2026, 9, 4))

    (span,) = span_exporter.get_finished_spans()
    assert span.name == "rock_in_rio.scrape.parse_day"
    assert span.attributes["rock_in_rio.dia_slug"] == "04-set"
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "LineupInvalido"
    assert "rock_in_rio.show_count" not in span.attributes
    assert not span.events


# --- fetch_day isolado (sem rede: cliente falso) ----------------------------


@pytest.mark.asyncio
async def test_fetch_day_com_sucesso_marca_span_ok(span_exporter, monkeypatch):
    async def get_falso(_self, _url, **_kwargs):
        return _RespostaFalsa("<html>ok</html>")

    monkeypatch.setattr(InterceptedHTTPClient, "get", get_falso)

    async with InterceptedHTTPClient(user_id="teste", source={}) as cliente:
        texto = await scraper_mod._baixar_dia(cliente, "04-set")

    assert texto == "<html>ok</html>"
    (span,) = span_exporter.get_finished_spans()
    assert span.name == "rock_in_rio.scrape.fetch_day"
    assert span.attributes["rock_in_rio.dia_slug"] == "04-set"
    assert span.status.status_code == StatusCode.OK


@pytest.mark.asyncio
async def test_fetch_day_com_falha_marca_error_type_sem_texto_de_excecao(
    span_exporter, monkeypatch
):
    async def get_falso(_self, _url, **_kwargs):
        return _RespostaFalsa(erro=ConnectionError("timeout upstream simulado"))

    monkeypatch.setattr(InterceptedHTTPClient, "get", get_falso)

    async with InterceptedHTTPClient(user_id="teste", source={}) as cliente:
        with pytest.raises(ConnectionError):
            await scraper_mod._baixar_dia(cliente, "05-set")

    (span,) = span_exporter.get_finished_spans()
    assert span.attributes["rock_in_rio.dia_slug"] == "05-set"
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "ConnectionError"
    _sem_texto_de_excecao(span, "timeout upstream simulado")


# --- laço de refresh -----------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_encadeia_leitura_e_escrita_e_marca_sucesso(
    span_exporter, monkeypatch
):
    async def fonte_ok():
        return [SHOW]

    monkeypatch.setattr(cache_mod, "buscar_lineup", fonte_ok)
    monkeypatch.setattr(cache_mod, "intervalo_refresh_s", lambda: 0.01)

    tarefa = asyncio.create_task(cache_mod.run_refresh_loop())
    await asyncio.sleep(0.03)
    tarefa.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarefa

    spans = span_exporter.get_finished_spans()
    (refresh,) = _por_nome(spans, "rock_in_rio.refresh")[:1]
    (leitura,) = _por_nome(spans, "rock_in_rio.cache.read")[:1]
    (escrita,) = _por_nome(spans, "rock_in_rio.cache.write")[:1]

    assert refresh.status.status_code == StatusCode.OK
    assert leitura.parent.span_id == refresh.context.span_id
    assert escrita.parent.span_id == leitura.context.span_id


@pytest.mark.asyncio
async def test_refresh_marca_erro_sem_derrubar_o_laco(span_exporter, monkeypatch):
    async def fonte_quebrada():
        raise ConnectionError("fora do ar")

    monkeypatch.setattr(cache_mod, "buscar_lineup", fonte_quebrada)
    monkeypatch.setattr(cache_mod, "intervalo_refresh_s", lambda: 0.01)

    tarefa = asyncio.create_task(cache_mod.run_refresh_loop())
    await asyncio.sleep(0.03)
    tarefa.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarefa

    refreshes = _por_nome(span_exporter.get_finished_spans(), "rock_in_rio.refresh")

    assert refreshes
    assert all(s.status.status_code == StatusCode.ERROR for s in refreshes)
    assert all(s.attributes["error.type"] == "LineupIndisponivel" for s in refreshes)
    for span in refreshes:
        _sem_texto_de_excecao(span, "fora do ar")
