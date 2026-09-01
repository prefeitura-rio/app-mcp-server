"""Toda rota HTTP servida em produção exige token, exceto as probes.

Este é o teste de regressão do achado A-01. O `RequireAuthMiddleware` nativo do
FastMCP embrulha só a rota do transporte MCP; tudo registrado com
`@mcp.custom_route` entra na aplicação depois, fora daquele wrapper. Foi assim
que `/consulta_debitos` e as quatro rotas de emissão de guia passaram a atender
sem credencial nenhuma.

Por isso o teste não olha uma lista fixa de rotas: ele **enumera a tabela de
rotas da aplicação montada** e exige que cada uma responda 401 sem token, salvo
as que a allowlist libera explicitamente. Uma `custom_route` nova, registrada
por quem não conhecer esta história, cai aqui sozinha.

Nenhuma requisição autenticada é feita: sem token, o middleware corta antes do
handler, então nada aqui alcança PGM, BigQuery ou Redis.
"""

import importlib
import sys

import httpx
import pytest
from loguru import logger as loguru_logger

from src.health import state as health_state
from src.middleware.require_auth import LEGACY_OBSERVE_PATHS, PUBLIC_PATHS


# Rotas isentas por design, com o motivo. `/mcp` é isento *deste* middleware,
# não da autenticação: quem o protege é o wrapper nativo — o que
# `test_mcp_segue_embrulhado_pelo_wrapper_nativo` verifica separadamente.
ISENTAS = set(PUBLIC_PATHS) | {"/mcp"}

# As rotas legadas em observação, congeladas aqui de propósito. A lista existe
# para encolher; se alguém a fizer crescer, este teste falha e a decisão aparece
# na revisão do diff em vez de passar despercebida.
LEGADO_ESPERADO = {
    "/consulta_debitos",
    "/emitir_guia",
    "/emitir_guia_regularizacao",
    "/v2/emitir_guia",
    "/v2/emitir_guia_regularizacao",
}


@pytest.fixture
def app_module(monkeypatch):
    """Importa `src.app` num `sys.modules` limpo, com `IS_LOCAL=false`.

    Mesmo cuidado de `test_app_factory.py`: outros conftests instalam stubs
    permanentes de `src.*`, e `create_app()` mexe no estado de readiness.
    `IS_LOCAL` precisa ser falso porque é ele que decide se existe provider de
    autenticação — com ele ligado, não haveria nada para testar.
    """
    monkeypatch.setenv("IS_LOCAL", "false")
    monkeypatch.setenv("VALID_TOKENS", "token-de-teste")
    monkeypatch.delenv("KEYCLOAK_ISSUER", raising=False)
    monkeypatch.delenv("KEYCLOAK_JWKS_URI", raising=False)

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


def _asgi_app(app_module):
    """A aplicação exatamente como `src/main.py` a serve."""
    return app_module.mcp.http_app(
        path="/mcp",
        transport="http",
        stateless_http=True,
        middleware=app_module.build_http_middleware(),
    )


def _rotas(app):
    for rota in app.routes:
        path = getattr(rota, "path", None)
        if path is None:
            continue
        metodos = sorted((getattr(rota, "methods", None) or {"GET"}) - {"HEAD"})
        yield path, (metodos[0] if metodos else "GET")


async def _corpo_em_chunks(total: int, pedaco: int = 64 * 1024):
    """Corpo sem `Content-Length`.

    O httpx só usa `Transfer-Encoding: chunked` quando o conteúdo é um
    iterável — passar `bytes` faz ele declarar o tamanho, e aí quem recusa é o
    outro caminho do middleware.
    """
    enviados = 0
    while enviados < total:
        atual = min(pedaco, total - enviados)
        yield b"a" * atual
        enviados += atual


@pytest.fixture
def eventos_de_log():
    """Sink temporário do loguru, para afirmar o evento de telemetria."""
    capturados = []
    handler_id = loguru_logger.add(capturados.append, level="WARNING")
    try:
        yield capturados
    finally:
        loguru_logger.remove(handler_id)


def test_o_provider_de_auth_existe(app_module):
    """Sanidade: sem provider o teste inteiro passaria por vacuidade."""
    assert app_module._auth_provider is not None


def test_a_lista_de_legado_nao_cresceu(app_module):
    """Grandfathering é dívida, e dívida precisa aparecer no balanço.

    Acrescentar uma rota a `LEGACY_OBSERVE_PATHS` é dizer "esta atende sem
    token". Que seja um ato deliberado, revisado — não algo que se faz para
    silenciar um teste vermelho.
    """
    assert set(LEGACY_OBSERVE_PATHS) == LEGADO_ESPERADO


def test_o_legado_corresponde_a_rotas_que_existem(app_module):
    """Entrada obsoleta na lista é isenção fantasma: some da revisão porque
    ninguém liga o nome a uma rota real."""
    app = _asgi_app(app_module)
    servidas = {path for path, _ in _rotas(app)}
    orfas = set(LEGACY_OBSERVE_PATHS) - servidas
    assert not orfas, f"em observação mas não servidas: {sorted(orfas)}"


@pytest.mark.asyncio
async def test_nenhuma_rota_nova_atende_sem_token(app_module):
    """A-01. Toda rota que não seja probe, delegada ou legado explícito precisa
    recusar sem token.

    O legado é pulado, e não afirmado como 200: executá-lo faria o handler
    chamar a PGM de verdade. O que garante que ele *está* em observação é
    `test_legado_em_observacao_passa_sem_token`, contra uma app de mentira.
    """
    app = _asgi_app(app_module)

    resultados = {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        for path, metodo in _rotas(app):
            if path in ISENTAS or path in LEGACY_OBSERVE_PATHS:
                continue
            resposta = await c.request(metodo, path, json={})
            resultados[path] = resposta.status_code

    assert resultados, "nenhuma rota enumerada — o teste não estaria testando nada"

    abertas = {p: s for p, s in resultados.items() if s != 401}
    assert not abertas, f"rotas atendendo sem token: {abertas}"


@pytest.mark.asyncio
async def test_health_detail_exige_token(app_module):
    """D-04: é diagnóstico, não probe — e não tem consumidor de contrato."""
    app = _asgi_app(app_module)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resposta = await c.get("/health/detail")
    assert resposta.status_code == 401


@pytest.mark.asyncio
async def test_probes_do_kubelet_seguem_abertas(app_module):
    """Liveness e readiness não podem exigir credencial: o kubelet não tem uma,
    e um 401 aqui derruba o pod."""
    app = _asgi_app(app_module)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        for rota in sorted(PUBLIC_PATHS):
            resposta = await c.get(rota)
            assert resposta.status_code == 200, (
                f"{rota} respondeu {resposta.status_code}"
            )


@pytest.mark.asyncio
async def test_corpo_acima_do_teto_e_recusado_antes_do_handler(app_module):
    """E-02, na aplicação real: 413 sem o corpo virar memória do processo."""
    app = _asgi_app(app_module)
    limite = app_module.env.MAX_REQUEST_BODY_BYTES

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resposta = await c.post("/consulta_debitos", content=b"a" * (limite + 1))

    assert resposta.status_code == 413

    assert resposta.json()["error"] == "payload_too_large"


@pytest.mark.asyncio
async def test_corpo_chunked_acima_do_teto_devolve_413(app_module):
    """O caminho sem `Content-Length` — o único que o teto de fato protege.

    O teste acima manda `content=bytes`, e o httpx declara `Content-Length`: o
    middleware recusa pelo header, antes de ler um byte. Quem não declara
    tamanho cai no outro caminho, e ali o corpo passa pelo `receive` — que é
    onde o handler entra no meio. Os cinco handlers de Dívida Ativa embrulham o
    `await request.json()` em `except Exception`: enquanto o sentinel do
    middleware herdava de `Exception`, esta requisição voltava 500 com
    `{"error": ""}`, sem 413 e sem telemetria.
    """
    app = _asgi_app(app_module)
    limite = app_module.env.MAX_REQUEST_BODY_BYTES

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resposta = await c.post(
            "/consulta_debitos", content=_corpo_em_chunks(limite + 1)
        )

    assert resposta.status_code == 413, (
        f"chunked acima do teto devolveu {resposta.status_code}: {resposta.text!r}"
    )
    assert resposta.json()["error"] == "payload_too_large"


@pytest.mark.asyncio
async def test_estouro_em_chunks_registra_a_telemetria(app_module, eventos_de_log):
    """O evento é o que permite ver o teto sendo batido em produção.

    Sem ele, um cliente empurrando corpo grande em chunks fica indistinguível
    de erro de aplicação no painel — que foi exatamente o efeito de o handler
    engolir o sentinel e devolver 500.
    """
    app = _asgi_app(app_module)
    limite = app_module.env.MAX_REQUEST_BODY_BYTES

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        await c.post("/consulta_debitos", content=_corpo_em_chunks(limite + 1))

    estouros = [linha for linha in eventos_de_log if "request_body_too_large" in linha]
    assert estouros, f"nenhum evento de estouro registrado: {eventos_de_log}"
    assert any("streaming" in linha for linha in estouros), (
        f"o evento saiu sem o motivo 'streaming': {estouros}"
    )


@pytest.mark.asyncio
async def test_nenhuma_rota_com_corpo_escapa_do_teto(app_module):
    """A catraca: rota nova nasce com teto, sem depender do handler dela.

    Mesmo espírito de `test_nenhuma_rota_nova_atende_sem_token` — enumera a
    tabela de rotas em vez de conferir uma lista fixa. Um handler novo que
    embrulhe tudo em `except Exception` (o padrão do arquivo) cai aqui.

    Vai com token válido porque o teto é o middleware mais externo: sem token,
    a camada de autenticação recusaria antes de alguém ler o corpo, e o 401
    esconderia o que se quer medir. Nenhum handler chega a executar — o corpo
    estoura dentro do `await request.json()`, antes de qualquer chamada à PGM.
    """
    app = _asgi_app(app_module)
    limite = app_module.env.MAX_REQUEST_BODY_BYTES
    cabecalhos = {"Authorization": "Bearer token-de-teste"}

    alvos = [
        path for path, metodo in _rotas(app) if metodo == "POST" and path not in ISENTAS
    ]
    assert alvos, "nenhuma rota POST enumerada — o teste não estaria testando nada"

    resultados = {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        for path in alvos:
            resposta = await c.post(
                path, content=_corpo_em_chunks(limite + 1), headers=cabecalhos
            )
            resultados[path] = resposta.status_code

    escaparam = {p: s for p, s in resultados.items() if s != 413}
    assert not escaparam, f"rotas que não devolveram 413 em chunked: {escaparam}"


def test_mcp_segue_embrulhado_pelo_wrapper_nativo(app_module):
    """`RequireAuthOnAllRoutes` isenta `/mcp` porque o FastMCP já o protege.

    Se essa premissa deixar de valer — outra versão do framework, outro path —
    a isenção vira buraco. Este teste é o que amarra as duas pontas.
    """
    app = _asgi_app(app_module)
    endpoints = {
        getattr(r, "path", None): type(
            getattr(r, "endpoint", None) or getattr(r, "app", None)
        ).__name__
        for r in app.routes
    }
    assert endpoints.get("/mcp") == "RequireAuthMiddleware", (
        f"/mcp não está mais embrulhado pelo wrapper nativo: {endpoints.get('/mcp')!r}"
    )
