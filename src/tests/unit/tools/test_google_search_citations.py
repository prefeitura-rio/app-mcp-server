"""Citações da busca via Gemini com `grounding_metadata` incompleto.

Em nov/2026 o Gemini respondeu com `finish_reason=STOP`, fontes resolvidas e
`grounding_supports = None`: `get_citations` iterava o `None` e o `TypeError` chegava
ao Salesforce como `Error calling tool 'google_search'` (8 vezes de 04 a 19/11).
Campo opcional ausente vale como lista vazia, e uma falha ao montar as citações
devolve o texto do Gemini sem fontes, no mesmo formato de saída da tool.

As respostas usam os tipos reais do SDK (`google.genai.types`), que deixam `None`
em todo campo não preenchido, como chega em produção.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from google.genai import types as genai_types

# Importados de verdade antes dos stubs de `src.config`/`src.utils.log` da fixture,
# pelo mesmo motivo de `test_google_search_retry.py`.
from src.observability import tracing  # noqa: F401
from src.config import gemini_search

PROJECT_ROOT = Path(__file__).resolve().parents[4]
GEMINI_MODULE_PATH = (
    PROJECT_ROOT / "src" / "tools" / "google_search" / "gemini_service.py"
)

TEXTO = "O IPTU 2026 pode ser pago em até 10 cotas."
URI = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
URL = "https://carioca.rio/servicos/iptu"
SEM_FONTE = "não consegui encontrar informações confiáveis"


def _chunk(uri=URI, title="carioca.rio"):
    return genai_types.GroundingChunk(
        web=genai_types.GroundingChunkWeb(uri=uri, title=title, domain="carioca.rio")
    )


def _support(start=0, end=len(TEXTO), indices=(0,), segment=True):
    return genai_types.GroundingSupport(
        segment=genai_types.Segment(start_index=start, end_index=end)
        if segment
        else None,
        grounding_chunk_indices=list(indices),
    )


def _resposta(metadata):
    return genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(parts=[genai_types.Part(text=TEXTO)]),
                finish_reason=genai_types.FinishReason.STOP,
                grounding_metadata=metadata,
            )
        ]
    )


def _metadata(**campos):
    return genai_types.GroundingMetadata(web_search_queries=["iptu 2026 rio"], **campos)


RESOLVIDAS = {URI: {"url": URL, "error": None}}


class _Logger:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *_a, **_k):
        self.warnings.append(msg)

    def info(self, *_a, **_k):
        pass

    debug = error = exception = info


@pytest.fixture
def gemini(monkeypatch):
    """Carrega gemini_service.py com a rede falsa e o pós-processamento real."""
    env_stub = types.SimpleNamespace(
        GEMINI_API_KEY="test-gemini-key",
        GEMINI_SEARCH_CONFIG=gemini_search.DEFAULT_CONFIG,
        GEMINI_SEARCH_RETRY_BASE_SECONDS=2.0,
        GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS=16.0,
        IS_LOCAL=False,
        TYPESENSE_ACTIVE="false",
        TYPESENSE_HUB_SEARCH_URL="",
        TYPESENSE_PARAMETERS="none",
    )
    logger = _Logger()

    async def fake_send_api_error(**_kwargs):
        return True

    monkeypatch.setitem(sys.modules, "src.config", types.SimpleNamespace(env=env_stub))
    monkeypatch.setitem(sys.modules, "src.config.env", env_stub)
    monkeypatch.setitem(
        sys.modules, "src.utils.log", types.SimpleNamespace(logger=logger)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(
            interceptor=lambda *_a, **_k: lambda func: func,
            send_api_error=fake_send_api_error,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=object),
    )

    spec = importlib.util.spec_from_file_location(
        "test_gemini_citations_module", GEMINI_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "test_gemini_citations_module", module)
    spec.loader.exec_module(module)

    async def fake_resolve_urls(urls_to_resolve):
        uris = {c.web.uri for c in urls_to_resolve}
        return {uri: RESOLVIDAS[uri] for uri in uris if uri in RESOLVIDAS}

    monkeypatch.setattr(module, "resolve_urls", fake_resolve_urls)

    def buscar(metadata):
        async def generate_content(**_kwargs):
            return _resposta(metadata)

        module.gemini_service.client = types.SimpleNamespace(
            aio=types.SimpleNamespace(
                models=types.SimpleNamespace(generate_content=generate_content)
            )
        )
        return asyncio.run(module.gemini_service.google_search(query="iptu 2026"))

    return types.SimpleNamespace(module=module, buscar=buscar, logger=logger)


# --- get_citations ---------------------------------------------------------------


def test_citacao_com_metadata_completo(gemini):
    metadata = _metadata(grounding_chunks=[_chunk()], grounding_supports=[_support()])

    citations = gemini.module.get_citations(_resposta(metadata), RESOLVIDAS)

    assert citations == [
        {
            "start_index": 0,
            "end_index": len(TEXTO),
            "segments": [{"label": "carioca.rio", "uri": URI, "url": URL}],
        }
    ]


def test_sem_grounding_metadata_nao_ha_citacao(gemini):
    assert gemini.module.get_citations(_resposta(None), RESOLVIDAS) == []


def test_grounding_supports_none_nao_ha_citacao(gemini):
    """O caso de produção: metadata preenchido, chunks resolvidos, supports None."""
    metadata = _metadata(grounding_chunks=[_chunk()], grounding_supports=None)

    assert gemini.module.get_citations(_resposta(metadata), RESOLVIDAS) == []


def test_grounding_chunks_none_ignora_os_indices(gemini):
    metadata = _metadata(grounding_chunks=None, grounding_supports=[_support()])

    citations = gemini.module.get_citations(_resposta(metadata), RESOLVIDAS)

    assert citations == [{"start_index": 0, "end_index": len(TEXTO), "segments": []}]


def test_suporte_sem_segment_e_pulado(gemini):
    metadata = _metadata(
        grounding_chunks=[_chunk()],
        grounding_supports=[_support(segment=False), _support(start=None, end=5)],
    )

    citations = gemini.module.get_citations(_resposta(metadata), RESOLVIDAS)

    assert [(c["start_index"], c["end_index"]) for c in citations] == [(0, 5)]


def test_segment_sem_end_index_e_pulado(gemini):
    metadata = _metadata(
        grounding_chunks=[_chunk()], grounding_supports=[_support(end=None)]
    )

    assert gemini.module.get_citations(_resposta(metadata), RESOLVIDAS) == []


@pytest.mark.parametrize(
    "chunk",
    [
        genai_types.GroundingChunk(web=None),
        _chunk(uri=None),
        _chunk(uri="nao-resolvida"),
    ],
    ids=["sem-web", "sem-uri", "fora-do-mapa"],
)
def test_chunk_sem_fonte_utilizavel_fica_fora(gemini, chunk):
    metadata = _metadata(
        grounding_chunks=[chunk, _chunk()],
        grounding_supports=[_support(indices=[0, 1, 7])],
    )

    citations = gemini.module.get_citations(_resposta(metadata), RESOLVIDAS)

    assert citations[0]["segments"] == [
        {"label": "carioca.rio", "uri": URI, "url": URL}
    ]


# --- _resposta_com_fontes (pela tool inteira) -------------------------------------


def test_busca_com_citacoes_mantem_o_formato(gemini):
    result = gemini.buscar(
        _metadata(grounding_chunks=[_chunk()], grounding_supports=[_support()])
    )

    assert result["success"] is True
    assert result["text"].startswith(TEXTO + " [1]")
    assert URL in result["text"]
    assert result["sources"] == [
        {"label": "carioca.rio", "uri": URI, "url": URL, "index": 1}
    ]
    assert result["web_search_queries"] == ["iptu 2026 rio"]


def test_busca_sem_grounding_metadata_responde_sem_fonte(gemini):
    result = gemini.buscar(None)

    assert result["success"] is True
    assert SEM_FONTE in result["text"]
    assert result["sources"] == []


def test_busca_com_grounding_supports_none_nao_quebra(gemini):
    """Regressão do `TypeError: 'NoneType' object is not iterable` de nov/2026."""
    result = gemini.buscar(
        _metadata(grounding_chunks=[_chunk()], grounding_supports=None)
    )

    assert result["success"] is True
    assert result["text"].startswith(TEXTO)
    assert result["sources"] == []
    assert result["web_search_queries"] == ["iptu 2026 rio"]


def test_busca_com_grounding_chunks_none_responde_sem_fonte(gemini):
    result = gemini.buscar(
        _metadata(grounding_chunks=None, grounding_supports=[_support()])
    )

    assert result["success"] is True
    assert SEM_FONTE in result["text"]
    assert result["sources"] == []


def test_busca_com_suporte_sem_segment_usa_os_demais(gemini):
    result = gemini.buscar(
        _metadata(
            grounding_chunks=[_chunk()],
            grounding_supports=[_support(segment=False), _support()],
        )
    )

    assert result["success"] is True
    assert [s["url"] for s in result["sources"]] == [URL]


def test_falha_ao_montar_citacoes_devolve_o_texto_sem_fontes(gemini, monkeypatch):
    """Qualquer outro formato inesperado: a tool responde, sem citação, com aviso."""

    def quebra(**_kwargs):
        raise TypeError("'NoneType' object is not iterable")

    monkeypatch.setattr(gemini.module, "get_citations", quebra)

    result = gemini.buscar(
        _metadata(grounding_chunks=[_chunk()], grounding_supports=[_support()])
    )

    assert result["success"] is True
    assert result["text"] == TEXTO
    assert result["sources"] == []
    assert result["web_search_queries"] == ["iptu 2026 rio"]
    assert set(result) >= {"id", "model", "query", "tokens_metadata", "finish_reason"}
    assert any("Falha ao montar as citações" in w for w in gemini.logger.warnings)
