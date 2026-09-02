"""Instrumentação do line-up do Rock in Rio (span e métrica).

Estes testes protegem o sinal que faltou em 01/09/2026: o site mudou de
estrutura, a tool ficou fora do ar e o SigNoz não registrou um único erro. Duas
coisas precisam ficar visíveis e continuar visíveis:

1. o ciclo de busca emite um span próprio, com a falha classificada — o laço de
   background não atende requisição, então esse span é raiz e é a única coisa
   que aparece no SigNoz quando o site muda de madrugada;
2. a tool marca `rock_in_rio.degraded` no span da chamada quando devolve
   resposta indisponível — sem isso ela sai como sucesso, porque o middleware
   de tracing só enxerga exceção, e esta tool devolve dicionário de propósito.
"""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from src.observability import metrics
from src.tools.rock_in_rio import cache as cache_mod
from src.tools.rock_in_rio import tool as tool_mod
from src.tools.rock_in_rio.cache import LineupIndisponivel, obter_lineup, resetar_cache
from src.tools.rock_in_rio.scraper import LineupInvalido, Show

SHOW = Show(
    data="2026-09-04",
    dia_slug="04-set",
    palco="Palco Mundo",
    artista="FOO FIGHTERS",
    slug="foo-fighters",
    url="https://rockinrio.com/rio/pt-br/line-up/foo-fighters/",
)


@pytest.fixture
def provider():
    return TracerProvider()


@pytest.fixture
def spans(monkeypatch, provider):
    """Exporta os spans do ciclo de busca para memória.

    Faz o patch em `cache_mod.get_tracer`, e não em `tracing.get_tracer`: o
    módulo importa a função por nome, então trocar no módulo de origem não
    alcançaria a referência já ligada.
    """
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(cache_mod, "get_tracer", lambda: provider.get_tracer("teste"))
    return exporter


@pytest.fixture(autouse=True)
def _isolar(monkeypatch):
    resetar_cache()
    metrics.reset_for_tests()

    async def _sem_redis(*args, **kwargs):
        return None

    monkeypatch.setattr(cache_mod, "_ler_redis", _sem_redis)
    monkeypatch.setattr(cache_mod, "_gravar_redis", _sem_redis)
    yield
    resetar_cache()
    metrics.reset_for_tests()


def _fonte(monkeypatch, *, falha=None):
    async def _buscar():
        if falha is not None:
            raise falha
        return [SHOW]

    monkeypatch.setattr(cache_mod, "buscar_lineup", _buscar)


def _span_do_ciclo(exporter):
    (span,) = [
        s for s in exporter.get_finished_spans() if s.name == "rock_in_rio.lineup_fetch"
    ]
    return span


@pytest.mark.asyncio
async def test_ciclo_bem_sucedido_emite_span_com_a_contagem(monkeypatch, spans):
    _fonte(monkeypatch)

    await obter_lineup()

    span = _span_do_ciclo(spans)
    assert span.attributes["rock_in_rio.success"] is True
    assert span.attributes["rock_in_rio.atracoes"] == 1
    assert span.attributes["rock_in_rio.palcos"] == 1
    assert span.attributes["rock_in_rio.dias"] == 7
    assert span.status.status_code is StatusCode.OK


@pytest.mark.asyncio
async def test_mudanca_de_formato_marca_o_span_como_erro(monkeypatch, spans):
    """É este span que cai na aba Exceptions do SigNoz."""
    _fonte(monkeypatch, falha=LineupInvalido("o site mudou"))

    with pytest.raises(LineupIndisponivel):
        await obter_lineup()

    span = _span_do_ciclo(spans)
    assert span.attributes["rock_in_rio.success"] is False
    assert span.attributes["rock_in_rio.failure_kind"] == "formato"
    assert span.status.status_code is StatusCode.ERROR
    assert [e.name for e in span.events] == ["exception"]


@pytest.mark.asyncio
async def test_falha_de_rede_e_marcada_como_fonte(monkeypatch, spans):
    _fonte(monkeypatch, falha=ConnectionError("fora"))

    with pytest.raises(LineupIndisponivel):
        await obter_lineup()

    assert _span_do_ciclo(spans).attributes["rock_in_rio.failure_kind"] == "fonte"


@pytest.mark.asyncio
async def test_falha_alimenta_o_contador_de_erros_de_dependencia(monkeypatch, spans):
    """A métrica é o que permite alerta e dashboard.

    O span é ponto a ponto e não agrega; sem a série temporal não dá para
    perguntar "quantas vezes isso caiu na última hora".
    """
    reader = InMemoryMetricReader()
    metrics.configure_for_test(reader)
    _fonte(monkeypatch, falha=LineupInvalido("o site mudou"))

    with pytest.raises(LineupIndisponivel):
        await obter_lineup()

    pontos = [
        ponto
        for rm in reader.get_metrics_data().resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "mcp.dependency.errors"
        for ponto in m.data.data_points
    ]
    assert [p.attributes["dependency.name"] for p in pontos] == ["rock_in_rio"]


@pytest.mark.asyncio
async def test_tool_indisponivel_marca_degradacao_no_span_da_chamada(
    monkeypatch, provider
):
    """Sem este atributo a falha sai como `mcp.tool.success = True`.

    Foi exatamente assim que a quebra de 01/09/2026 passou despercebida.
    """
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    async def _indisponivel():
        raise LineupIndisponivel("fonte fora do ar")

    monkeypatch.setattr(tool_mod, "obter_lineup", _indisponivel)

    with provider.get_tracer("teste").start_as_current_span("mcp.tool_call"):
        resposta = await tool_mod.get_rock_in_rio_lineup()

    assert resposta["disponivel"] is False
    (span,) = exporter.get_finished_spans()
    assert span.attributes["rock_in_rio.degraded"] is True
    assert span.attributes["rock_in_rio.motivo"] == "LineupIndisponivel"


@pytest.mark.asyncio
async def test_marcar_degradacao_sem_span_ativo_nao_levanta(monkeypatch):
    """Observabilidade nunca derruba o caminho principal."""
    assert trace.get_current_span().get_span_context().is_valid is False

    tool_mod._marcar_degradacao("LineupIndisponivel")
