"""Comportamento do `RequireAuthOnAllRoutes` contra uma app ASGI de mentira.

Isolado de propósito: aqui se testa a regra (quem passa, quem não passa, o que
é isento), sem depender de como `src/app.py` monta o servidor. A garantia de
que a regra está de fato ligada nas rotas reais é outro teste —
`src/tests/unit/app/test_http_auth_coverage.py`.
"""

import httpx
import pytest

from src.middleware.require_auth import (
    LEGACY_OBSERVE_PATHS,
    MODE_OBSERVE,
    RequireAuthOnAllRoutes,
    _bearer_token,
    _client_ip,
)


class _VerificadorFalso:
    """Aceita um único token e registra o que recebeu."""

    def __init__(self, aceito="bom"):
        self.aceito = aceito
        self.vistos = []

    async def verify_token(self, token):
        self.vistos.append(token)
        return object() if token == self.aceito else None


class _VerificadorQueExplode:
    async def verify_token(self, token):
        raise RuntimeError("JWKS fora do ar")


async def _app_alvo(scope, receive, send):
    """Responde 200 e marca que foi alcançada."""
    _app_alvo.alcancada = True
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"chegou"})


@pytest.fixture(autouse=True)
def _zera_marcador():
    _app_alvo.alcancada = False
    yield


def _cliente(**kwargs):
    """Cliente contra o middleware. Sem legado por default: cada teste que
    quiser grandfathering pede `observe_paths` explicitamente, para que a
    intenção fique no próprio teste."""
    kwargs.setdefault("observe_paths", frozenset())
    app = RequireAuthOnAllRoutes(_app_alvo, **kwargs)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


@pytest.mark.asyncio
async def test_sem_token_recusa_e_nao_alcanca_o_handler():
    """O ponto do middleware: o handler não roda. 401 depois de já ter
    consultado a PGM não protegeria nada."""
    async with _cliente(verifier=_VerificadorFalso()) as c:
        r = await c.post("/consulta_debitos", json={})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert _app_alvo.alcancada is False


@pytest.mark.asyncio
async def test_o_desafio_so_traz_error_quando_veio_credencial():
    """RFC 6750 3.1: `error` so entra quando um token foi apresentado e
    recusado.

    Sem esta distincao o servidor responde dois formatos para a mesma
    pergunta: o `/mcp`, protegido pelo middleware nativo do FastMCP, devolve o
    desafio nu para requisicao sem credencial, e este middleware devolveria
    `invalid_token`. Um cliente que le o desafio para decidir se renova o
    token ou se pede um veria coisas diferentes conforme a rota.
    """
    async with _cliente(verifier=_VerificadorFalso()) as c:
        sem = await c.post("/consulta_debitos", json={})
        com = await c.post(
            "/consulta_debitos", json={}, headers={"Authorization": "Bearer ruim"}
        )

    assert sem.status_code == com.status_code == 401
    assert sem.headers["www-authenticate"] == "Bearer"
    assert com.headers["www-authenticate"] == 'Bearer error="invalid_token"'

    # O corpo nao distingue os dois: dizer a quem sonda se o token chegou a
    # ser avaliado entrega informacao de graca.
    assert sem.content == com.content


@pytest.mark.asyncio
async def test_token_invalido_recusa():
    async with _cliente(verifier=_VerificadorFalso()) as c:
        r = await c.post("/emitir_guia", headers={"Authorization": "Bearer ruim"})
    assert r.status_code == 401
    assert _app_alvo.alcancada is False


@pytest.mark.asyncio
async def test_token_valido_atravessa():
    async with _cliente(verifier=_VerificadorFalso()) as c:
        r = await c.post("/emitir_guia", headers={"Authorization": "Bearer bom"})
    assert r.status_code == 200
    assert _app_alvo.alcancada is True


@pytest.mark.asyncio
async def test_esquema_bearer_e_case_insensitive():
    """RFC 6750 define o esquema como case-insensitive; recusar `bearer`
    minúsculo seria rejeitar um cliente correto."""
    async with _cliente(verifier=_VerificadorFalso()) as c:
        r = await c.get("/x", headers={"Authorization": "bearer bom"})
    assert r.status_code == 200


@pytest.mark.parametrize("rota", ["/health", "/health/ready"])
@pytest.mark.asyncio
async def test_probes_do_kubelet_seguem_abertas(rota):
    async with _cliente(verifier=_VerificadorFalso()) as c:
        r = await c.get(rota)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_health_detail_passa_a_exigir_token():
    """D-04: o diagnóstico desenha o mapa das dependências e não é probe."""
    async with _cliente(verifier=_VerificadorFalso()) as c:
        r = await c.get("/health/detail")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_mcp_e_delegado_sem_segunda_verificacao():
    """`/mcp` já é embrulhado pelo `RequireAuthMiddleware` nativo, que devolve
    o 401 no formato da spec. Verificar de novo aqui trocaria essa resposta por
    uma genérica e quebraria a descoberta de OAuth no cliente."""
    verificador = _VerificadorFalso()
    async with _cliente(verifier=verificador) as c:
        r = await c.post("/mcp")
    assert r.status_code == 200
    assert verificador.vistos == []


@pytest.mark.asyncio
async def test_verificador_que_explode_nega():
    """Fail-closed: falha ao verificar nunca vira acesso concedido."""
    async with _cliente(verifier=_VerificadorQueExplode()) as c:
        r = await c.get("/x", headers={"Authorization": "Bearer qualquer"})
    assert r.status_code == 401
    assert _app_alvo.alcancada is False


@pytest.mark.asyncio
async def test_modo_observe_deixa_passar():
    """Janela de rollout: registra o que negaria, sem cortar o consumidor."""
    async with _cliente(verifier=_VerificadorFalso(), mode=MODE_OBSERVE) as c:
        r = await c.post("/consulta_debitos", json={})
    assert r.status_code == 200
    assert _app_alvo.alcancada is True


@pytest.mark.asyncio
async def test_modo_desconhecido_cai_para_enforce():
    """`HTTP_AUTH_MODE=observ` (typo) não pode virar porta aberta."""
    async with _cliente(verifier=_VerificadorFalso(), mode="observ") as c:
        r = await c.post("/consulta_debitos", json={})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "header, esperado",
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("Bearer  abc ", "abc"),
        ("Basic abc", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("abc", None),
    ],
)
def test_extracao_do_bearer(header, esperado):
    assert _bearer_token([(b"authorization", header.encode())]) == esperado


def test_sem_header_authorization():
    assert _bearer_token([(b"content-type", b"application/json")]) is None


# ---------------------------------------------------------------------------
# Grandfathering das rotas legadas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rota", sorted(LEGACY_OBSERVE_PATHS))
@pytest.mark.asyncio
async def test_legado_em_observacao_passa_sem_token(rota):
    """O contrato dos consumidores atuais não pode quebrar: quem chama
    `/consulta_debitos` e `/emitir_guia*` sem token continua sendo atendido."""
    async with _cliente(
        verifier=_VerificadorFalso(), observe_paths=LEGACY_OBSERVE_PATHS
    ) as c:
        r = await c.post(rota, json={})
    assert r.status_code == 200
    assert _app_alvo.alcancada is True


@pytest.mark.asyncio
async def test_rota_nova_nao_herda_a_observacao_do_legado():
    """A parte estrutural: grandfathering vale para as rotas nomeadas, não para
    a aplicação. Uma `custom_route` acrescentada amanhã nasce exigindo token."""
    async with _cliente(
        verifier=_VerificadorFalso(), observe_paths=LEGACY_OBSERVE_PATHS
    ) as c:
        r = await c.post("/v3/emitir_guia", json={})
    assert r.status_code == 401
    assert _app_alvo.alcancada is False


@pytest.mark.asyncio
async def test_legado_com_token_valido_tambem_passa():
    """Migrar um consumidor não exige mexer no servidor: ele começa a mandar o
    token e a rota continua atendendo, agora autenticada."""
    async with _cliente(
        verifier=_VerificadorFalso(), observe_paths=LEGACY_OBSERVE_PATHS
    ) as c:
        r = await c.post("/consulta_debitos", headers={"Authorization": "Bearer bom"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_lista_vazia_e_o_estado_final():
    """`HTTP_AUTH_OBSERVE_PATHS=""` exige token em tudo — para quando os
    consumidores tiverem migrado."""
    async with _cliente(verifier=_VerificadorFalso(), observe_paths=()) as c:
        r = await c.post("/consulta_debitos", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_modo_global_observe_cobre_ate_rota_nova():
    """Válvula de emergência: quando se descobre em produção que a exigência
    quebrou algo imprevisto, `HTTP_AUTH_MODE=observe` abre tudo de uma vez."""
    async with _cliente(
        verifier=_VerificadorFalso(), mode=MODE_OBSERVE, observe_paths=()
    ) as c:
        r = await c.post("/rota-que-nao-e-legado", json={})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_observacao_registra_evento_com_ip():
    """O log é o que permite migrar: sem saber quem chama sem token, a lista de
    legado nunca encolhe.

    O loguru serializa a mensagem para string antes de chegar ao sink, então o
    teste afirma sobre o texto — que é exatamente o que o operador vai ler.
    """
    from loguru import logger as loguru_logger

    linhas = []
    sink_id = loguru_logger.add(lambda m: linhas.append(str(m)), format="{message}")
    try:
        async with _cliente(
            verifier=_VerificadorFalso(), observe_paths=LEGACY_OBSERVE_PATHS
        ) as c:
            await c.post(
                "/consulta_debitos",
                json={},
                headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
            )
    finally:
        loguru_logger.remove(sink_id)

    assert linhas, "nada foi registrado"
    evento = linhas[-1]
    assert "http_auth_would_deny" in evento
    assert "/consulta_debitos" in evento
    assert "'token_presente': False" in evento
    assert "203.0.113.7" in evento
    # O salto interno informado pelo cliente não vale como identificação.
    assert "10.0.0.1" not in evento


@pytest.mark.parametrize(
    "headers, client, esperado",
    [
        (
            [(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")],
            ("10.0.0.2", 1),
            "203.0.113.7",
        ),
        ([(b"x-forwarded-for", b"  198.51.100.4  ")], None, "198.51.100.4"),
        ([(b"x-forwarded-for", b"")], ("10.0.0.2", 1), "10.0.0.2"),
        ([], ("10.0.0.2", 1), "10.0.0.2"),
        ([], None, None),
    ],
)
def test_extracao_do_ip_de_origem(headers, client, esperado):
    """Só o primeiro salto: os demais são informados pelo cliente."""
    assert _client_ip({"headers": headers, "client": client}) == esperado
