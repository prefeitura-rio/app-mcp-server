"""Teto de taxa por identidade.

O teste que importa é `test_usuarios_distintos_nao_disputam_o_mesmo_balde`: o
cenário previsto é uma rajada de cidadãos distintos perguntando a mesma coisa,
servida do cache a partir do primeiro. Um teto global cortaria exatamente esse
pico legítimo — e é o modo de falha que a chave por identidade existe para
evitar, não um detalhe de implementação.
"""

from types import SimpleNamespace

import pytest
from fastmcp.server.middleware.rate_limiting import RateLimitError
from loguru import logger as loguru_logger

from src.middleware import rate_limit
from src.middleware.rate_limit import chave_da_requisicao, montar_rate_limit


class _RequisicaoFalsa:
    def __init__(self, headers=None, host=None):
        self.headers = headers or {}
        self.client = SimpleNamespace(host=host) if host else None


class _ContextoHttpFalso:
    def __init__(self, request):
        self._request = request

    def get_http_request(self):
        if self._request is None:
            raise RuntimeError("sem requisição HTTP")
        return self._request


def _contexto(*, argumentos=None, headers=None, host=None, method="tools/call"):
    return SimpleNamespace(
        message=SimpleNamespace(arguments=argumentos),
        fastmcp_context=_ContextoHttpFalso(_RequisicaoFalsa(headers, host)),
        method=method,
    )


@pytest.fixture
def sem_token(monkeypatch):
    monkeypatch.setattr(rate_limit, "get_access_token", lambda: None)


@pytest.fixture
def com_token(monkeypatch):
    def _instalar(subject=None, client_id=None):
        monkeypatch.setattr(
            rate_limit,
            "get_access_token",
            lambda: SimpleNamespace(subject=subject, client_id=client_id),
        )

    return _instalar


# ===== a chave =====


def test_user_id_do_argumento_tem_precedencia(com_token):
    """O cidadão é a unidade que se quer isolar, não a credencial.

    Precedência importa: os consumidores falam com o servidor por uma
    credencial só (o chatbot), então chavear pela credencial jogaria todos os
    cidadãos no mesmo balde — que é o teto global disfarçado.
    """
    com_token(subject="sub-do-chatbot")
    assert chave_da_requisicao(_contexto(argumentos={"user_id": "5521999"})) == (
        "user:5521999"
    )


def test_sem_user_id_cai_na_credencial(com_token):
    """`initialize` e `tools/list` não pertencem a cidadão nenhum."""
    com_token(subject="sub-123")
    assert chave_da_requisicao(_contexto(method="tools/list")) == "cred:sub-123"


def test_client_id_cobre_o_token_estatico(com_token):
    """Token estático de dev/homologação não tem `sub`."""
    com_token(subject=None, client_id="legacy-static-token")
    assert chave_da_requisicao(_contexto()) == "cred:legacy-static-token"


def test_sem_credencial_cai_no_ip_de_origem(sem_token):
    contexto = _contexto(
        headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"}, host="10.0.0.2"
    )
    # Só o primeiro salto: os demais são informados pelo cliente.
    assert chave_da_requisicao(contexto) == "ip:203.0.113.7"


def test_sem_forwarded_usa_o_peer(sem_token):
    assert chave_da_requisicao(_contexto(host="10.0.0.2")) == "ip:10.0.0.2"


def test_sem_nada_vira_anonimo(sem_token):
    contexto = SimpleNamespace(message=None, fastmcp_context=None, method="initialize")
    assert chave_da_requisicao(contexto) == "anonimo"


def test_user_id_vazio_nao_vira_chave(com_token):
    """String vazia agruparia clientes distintos sob a mesma chave."""
    com_token(subject="sub-123")
    assert chave_da_requisicao(_contexto(argumentos={"user_id": "   "})) == (
        "cred:sub-123"
    )


def test_token_que_explode_nao_derruba_a_requisicao(monkeypatch):
    """Fora de requisição HTTP não há token a resolver — não é erro."""

    def explode():
        raise RuntimeError("sem contexto")

    monkeypatch.setattr(rate_limit, "get_access_token", explode)
    assert chave_da_requisicao(_contexto(host="10.0.0.2")) == "ip:10.0.0.2"


# ===== o teto =====


def test_rps_zero_desliga():
    """O default. Ligar sem número de produção troca um risco por outro."""
    assert montar_rate_limit(rps=0) is None
    assert montar_rate_limit(rps=-1) is None


def test_burst_default_e_o_dobro_do_rps():
    assert montar_rate_limit(rps=20).burst_capacity == 40
    assert montar_rate_limit(rps=20, burst=100).burst_capacity == 100


async def _chamar(middleware, contexto):
    async def call_next(_):
        return "ok"

    return await middleware.on_request(contexto, call_next)


@pytest.mark.asyncio
async def test_usuarios_distintos_nao_disputam_o_mesmo_balde(sem_token):
    """500 cidadãos num segundo, todos servidos: nenhum é recusado.

    É o cenário de operação previsto — a mesma pergunta, entregue do cache a
    partir da primeira. Com `global_limit=True`, ou com a chave na credencial,
    o 3º já levaria 429 e todos os seguintes junto.
    """
    middleware = montar_rate_limit(rps=2, burst=2)

    for i in range(500):
        assert await _chamar(
            middleware, _contexto(argumentos={"user_id": f"u{i}"})
        ) == ("ok")

    assert len(middleware.limiters) == 500


@pytest.mark.asyncio
async def test_o_mesmo_usuario_em_laco_e_cortado(sem_token):
    """O que o teto existe para pegar: um consumidor sozinho em laço."""
    middleware = montar_rate_limit(rps=1, burst=2)
    contexto = _contexto(argumentos={"user_id": "u1"})

    assert await _chamar(middleware, contexto) == "ok"
    assert await _chamar(middleware, contexto) == "ok"

    with pytest.raises(RateLimitError):
        await _chamar(middleware, contexto)


@pytest.mark.asyncio
async def test_um_usuario_cortado_nao_corta_os_outros(sem_token):
    """O 'nem os que vierem depois deles': o balde estourado é só o dele."""
    middleware = montar_rate_limit(rps=1, burst=1)
    abusivo = _contexto(argumentos={"user_id": "abusivo"})

    assert await _chamar(middleware, abusivo) == "ok"
    with pytest.raises(RateLimitError):
        await _chamar(middleware, abusivo)

    assert await _chamar(middleware, _contexto(argumentos={"user_id": "outro"})) == "ok"


@pytest.mark.asyncio
async def test_o_estouro_registra_telemetria(sem_token):
    """Sem o evento não dá para saber se o teto está cortando gente."""
    middleware = montar_rate_limit(rps=1, burst=1)
    contexto = _contexto(argumentos={"user_id": "u1"})
    await _chamar(middleware, contexto)

    linhas = []
    sink_id = loguru_logger.add(lambda m: linhas.append(str(m)), format="{message}")
    try:
        with pytest.raises(RateLimitError):
            await _chamar(middleware, contexto)
    finally:
        loguru_logger.remove(sink_id)

    assert linhas, "nada foi registrado"
    evento = linhas[-1]
    assert "mcp_rate_limited" in evento
    assert "user:u1" in evento
    assert "tools/call" in evento


@pytest.mark.asyncio
async def test_o_teto_nunca_e_global(sem_token):
    """`global_limit=True` reintroduziria exatamente o problema."""
    assert montar_rate_limit(rps=10).global_limit is False


# ===== a ligação com o app =====
#
# O teto pode estar perfeito e não estar plugado. Foi assim que as cinco rotas
# de Dívida Ativa ficaram públicas: o middleware existia, só não alcançava
# aquelas rotas.


def _app_com_env(monkeypatch, **variaveis):
    """Importa `src.app` num `sys.modules` limpo, com o ambiente pedido."""
    import importlib
    import sys

    from src.health import state as health_state

    monkeypatch.setenv("IS_LOCAL", "false")
    monkeypatch.setenv("VALID_TOKENS", "token-de-teste")
    for nome, valor in variaveis.items():
        monkeypatch.setenv(nome, valor)

    ready_antes = health_state.is_ready()
    snapshot = {n: m for n, m in sys.modules.items() if n.startswith("src")}
    for nome in list(sys.modules):
        if nome.startswith("src"):
            del sys.modules[nome]
    try:
        return importlib.import_module("src.app"), snapshot, ready_antes
    except BaseException:
        for nome in list(sys.modules):
            if nome.startswith("src"):
                del sys.modules[nome]
        sys.modules.update(snapshot)
        raise


def _restaurar(snapshot, ready_antes):
    import sys

    from src.health import state as health_state

    for nome in list(sys.modules):
        if nome.startswith("src"):
            del sys.modules[nome]
    sys.modules.update(snapshot)
    health_state.set_ready(ready_antes)


def _tetos(app_module):
    """Compara por nome, e não com `isinstance`.

    `_app_com_env` reimporta a árvore `src` inteira, então a classe que chega
    na cadeia é um objeto diferente da que este módulo de teste importou — um
    `isinstance` daria falso negativo e o teste "passaria" sem testar nada.
    """
    return [
        m
        for m in app_module.mcp.middleware
        if type(m).__name__ == "RateLimitPorIdentidade"
    ]


def test_desligado_por_default(monkeypatch):
    """Nenhum teto na cadeia enquanto `MCP_RATE_LIMIT_RPS` não for setado."""
    monkeypatch.delenv("MCP_RATE_LIMIT_RPS", raising=False)
    app_module, snapshot, ready = _app_com_env(monkeypatch)
    try:
        assert _tetos(app_module) == []
    finally:
        _restaurar(snapshot, ready)


def test_a_variavel_liga_o_teto_na_cadeia_do_mcp(monkeypatch):
    app_module, snapshot, ready = _app_com_env(monkeypatch, MCP_RATE_LIMIT_RPS="20")
    try:
        tetos = _tetos(app_module)
        assert len(tetos) == 1, "o teto não chegou na cadeia de middleware do MCP"
        assert tetos[0].max_requests_per_second == 20.0
        assert tetos[0].burst_capacity == 40
        assert tetos[0].global_limit is False
        assert tetos[0].get_client_id.__name__ == "chave_da_requisicao"
    finally:
        _restaurar(snapshot, ready)
