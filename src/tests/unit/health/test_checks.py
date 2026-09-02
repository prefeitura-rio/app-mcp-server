"""Testes dos checks de dependência (src/health/checks.py)."""

import sys
import types
from unittest.mock import AsyncMock

import pytest

from src.health import checks
from src.health.models import CheckStatus, HealthCheckError


@pytest.fixture
def fake_env(monkeypatch):
    """Substitui `src.config.env` por um stub.

    Os checks importam o módulo dentro das funções justamente para permitir
    isto: exercitá-los sem exigir o ambiente completo da aplicação.
    """
    env = types.SimpleNamespace(
        IS_LOCAL=False,
        REDIS_URL="redis://localhost:6379/0",
        REDIS_TTL_SECONDS=3600,
        KEYCLOAK_JWKS_URI="",
        KEYCLOAK_ISSUER="",
    )
    monkeypatch.setattr(sys.modules["src.config"], "env", env, raising=False)
    monkeypatch.setitem(sys.modules, "src.config.env", env)
    return env


@pytest.fixture(autouse=True)
def _reset_backend():
    checks.reset_redis_backend()
    yield
    checks.reset_redis_backend()


@pytest.mark.asyncio
async def test_redis_respondendo_vira_up(monkeypatch):
    backend = types.SimpleNamespace(health_check=AsyncMock(return_value=True))
    monkeypatch.setattr(checks, "_get_redis_backend", lambda: backend)

    assert await checks.check_redis() is CheckStatus.UP


@pytest.mark.asyncio
async def test_redis_sem_resposta_levanta_erro_controlado(monkeypatch):
    backend = types.SimpleNamespace(health_check=AsyncMock(return_value=False))
    monkeypatch.setattr(checks, "_get_redis_backend", lambda: backend)

    with pytest.raises(HealthCheckError):
        await checks.check_redis()


@pytest.mark.asyncio
async def test_backend_redis_e_reaproveitado(monkeypatch, fake_env):
    criados = []

    class FakeRedisBackend:
        def __init__(self, redis_url, ttl_seconds=None):
            criados.append(redis_url)

    fake_state = types.ModuleType("src.tools.multi_step_service.core.state")
    fake_state.RedisBackend = FakeRedisBackend
    monkeypatch.setitem(
        sys.modules, "src.tools.multi_step_service.core.state", fake_state
    )

    checks._get_redis_backend()
    checks._get_redis_backend()
    checks._get_redis_backend()

    # Recriar o cliente a cada probe vazaria conexões do pool.
    assert len(criados) == 1


@pytest.mark.asyncio
async def test_keycloak_sem_configuracao_e_pulado(fake_env):
    assert await checks.check_keycloak_jwks() is CheckStatus.SKIPPED


@pytest.mark.asyncio
async def test_tool_registry_com_tools_vira_up():
    mcp = types.SimpleNamespace(get_tools=AsyncMock(return_value={"alguma_tool": 1}))

    check = checks.make_tool_registry_check(mcp)

    assert await check() is CheckStatus.UP


@pytest.mark.asyncio
async def test_tool_registry_vazio_falha():
    # EXCLUDED_TOOLS mal configurado deixa o servidor de pé e sem tools.
    mcp = types.SimpleNamespace(get_tools=AsyncMock(return_value={}))

    check = checks.make_tool_registry_check(mcp)

    with pytest.raises(HealthCheckError):
        await check()


@pytest.mark.asyncio
async def test_tool_registry_usa_list_tools_no_modo_local():
    class LocalMcp:
        """`mcp.server.fastmcp.FastMCP` não tem `get_tools`."""

        async def list_tools(self):
            return ["uma_tool"]

    check = checks.make_tool_registry_check(LocalMcp())

    assert await check() is CheckStatus.UP


@pytest.mark.asyncio
async def test_data_files_ausentes_falham(monkeypatch):
    monkeypatch.setattr(checks.preflight, "check_data_files", lambda: ["arquivo sumiu"])

    with pytest.raises(HealthCheckError):
        await checks.check_data_files()


@pytest.mark.asyncio
async def test_data_files_presentes_viram_up(monkeypatch):
    monkeypatch.setattr(checks.preflight, "check_data_files", list)

    assert await checks.check_data_files() is CheckStatus.UP


def _stub_dlq(monkeypatch, profundidade):
    """Injeta um `src.utils.bigquery` mínimo, só com a leitura de profundidade.

    O módulo real puxa `src.config.env` e o client do BigQuery; o check só
    depende de um número, então trazer tudo isso para o teste só acrescentaria
    acoplamento.
    """

    async def _get_dlq_depth_async():
        return profundidade

    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            get_dlq_depth_async=_get_dlq_depth_async,
            formatar_duracao=lambda s: "PRAZO" if s else "sem prazo",
        ),
    )


@pytest.mark.asyncio
async def test_dlq_vazia_vira_up(monkeypatch):
    _stub_dlq(monkeypatch, {"redis": 0, "poison": 0, "arquivos": 0, "total": 0})
    assert await checks.check_bigquery_dlq() is CheckStatus.UP


@pytest.mark.asyncio
async def test_dlq_com_pendencia_degrada(monkeypatch):
    """A visibilidade que faltava: sem isto, dado parado na DLQ não aparece."""
    _stub_dlq(monkeypatch, {"redis": 7, "poison": 0, "arquivos": 0, "total": 7})
    with pytest.raises(HealthCheckError) as exc:
        await checks.check_bigquery_dlq()
    assert "7" in str(exc.value)


@pytest.mark.asyncio
async def test_dlq_com_poison_diz_onde_ate_quando_e_o_que_fazer(monkeypatch):
    """Degradar a partir do primeiro item só se sustenta se a saída for óbvia.

    O check fica vermelho até alguém agir. Para que "agir" não exija
    investigação prévia, a mensagem carrega a tabela afetada, o prazo até o TTL
    apagar o payload e o comando que resolve — os três dados que o operador
    precisaria descobrir por conta própria.
    """
    _stub_dlq(
        monkeypatch,
        {
            "redis": 1,
            "poison": 2,
            "arquivos": 0,
            "total": 3,
            "poison_tabelas": ["proj.ds.tbl"],
            "poison_expira_em_s": 540000,
        },
    )
    with pytest.raises(HealthCheckError) as exc:
        await checks.check_bigquery_dlq()

    mensagem = str(exc.value)
    assert "proj.ds.tbl" in mensagem
    assert "PRAZO" in mensagem
    assert "--requeue-poison" in mensagem
    assert "--purge-poison" in mensagem


@pytest.mark.asyncio
async def test_poison_sem_metadado_nao_quebra_a_mensagem(monkeypatch):
    """Profundidade de uma versão anterior (ou Redis mudo) não pode virar erro."""
    _stub_dlq(monkeypatch, {"redis": 0, "poison": 1, "arquivos": 0, "total": 1})
    with pytest.raises(HealthCheckError) as exc:
        await checks.check_bigquery_dlq()
    assert "1 item" in str(exc.value)


def test_registro_local_omite_dependencias_de_rede(monkeypatch, fake_env):
    from src.health.registry import HealthRegistry

    fake_env.IS_LOCAL = True
    registry = HealthRegistry()
    mcp = types.SimpleNamespace(get_tools=AsyncMock(return_value={"t": 1}))

    checks.register_default_checks(mcp, registry)

    assert "redis" not in registry.names
    assert "bigquery" not in registry.names
    assert "bigquery_dlq" not in registry.names
    assert "tool_registry" in registry.names


def test_registro_em_producao_inclui_redis_e_bigquery(monkeypatch, fake_env):
    from src.health.registry import HealthRegistry

    fake_env.IS_LOCAL = False
    registry = HealthRegistry()
    mcp = types.SimpleNamespace(get_tools=AsyncMock(return_value={"t": 1}))

    checks.register_default_checks(mcp, registry)

    assert {"redis", "bigquery", "keycloak_jwks", "bigquery_dlq"} <= set(registry.names)


# --------------------------------------------------------------------------
# Line-up do Rock in Rio
#
# O check reporta a FONTE, não o cache: um ciclo falho vira DOWN na hora, mesmo
# com o cidadão ainda sendo atendido pelo cache. A janela em que o cache ainda
# cobre é justamente a janela em que dá para consertar antes de alguém sentir.
# --------------------------------------------------------------------------


@pytest.fixture
def estado_do_lineup():
    """Isola o veredito do line-up entre os testes."""
    from src.tools.rock_in_rio import estado

    estado.resetar()
    yield estado
    estado.resetar()


@pytest.mark.asyncio
async def test_lineup_sem_ciclo_ainda_vira_skipped(estado_do_lineup):
    """Pod recém-subido não é pod quebrado — e SKIPPED não degrada o agregado."""
    assert await checks.check_rock_in_rio_lineup() is CheckStatus.SKIPPED


@pytest.mark.asyncio
async def test_lineup_com_ciclo_bem_sucedido_vira_up(estado_do_lineup):
    estado_do_lineup.registrar_sucesso(atracoes=170, palcos=7)

    assert await checks.check_rock_in_rio_lineup() is CheckStatus.UP


@pytest.mark.asyncio
async def test_lineup_com_formato_quebrado_diz_que_exige_codigo(estado_do_lineup):
    """A mensagem precisa separar "espere" de "alguém precisa agir agora"."""
    estado_do_lineup.registrar_falha(
        falha=estado_do_lineup.FALHA_FORMATO, detalhe="LineupInvalido"
    )

    with pytest.raises(HealthCheckError, match="exige correção de código"):
        await checks.check_rock_in_rio_lineup()


@pytest.mark.asyncio
async def test_lineup_com_fonte_fora_do_ar_reporta_a_classe_do_erro(estado_do_lineup):
    estado_do_lineup.registrar_falha(
        falha=estado_do_lineup.FALHA_FONTE, detalhe="ConnectError"
    )

    with pytest.raises(HealthCheckError, match="fonte do line-up inacessível"):
        await checks.check_rock_in_rio_lineup()


def test_registro_em_producao_inclui_o_lineup(monkeypatch, fake_env):
    from src.health.registry import HealthRegistry

    fake_env.IS_LOCAL = False
    fake_env.EXCLUDED_TOOLS = []
    registry = HealthRegistry()
    mcp = types.SimpleNamespace(get_tools=AsyncMock(return_value={"t": 1}))

    checks.register_default_checks(mcp, registry)

    assert "rock_in_rio_lineup" in registry.names


def test_tool_excluida_nao_registra_o_check(monkeypatch, fake_env):
    """Excluir a tool desliga o laço em `src/app.py`.

    Sem esta guarda, o check ficaria `DOWN` para sempre esperando um ciclo que
    ninguém vai rodar — um alarme permanentemente vermelho, que é o mesmo que
    alarme nenhum.
    """
    from src.health.registry import HealthRegistry

    fake_env.IS_LOCAL = False
    fake_env.EXCLUDED_TOOLS = ["rock_in_rio_lineup"]
    registry = HealthRegistry()
    mcp = types.SimpleNamespace(get_tools=AsyncMock(return_value={"t": 1}))

    checks.register_default_checks(mcp, registry)

    assert "rock_in_rio_lineup" not in registry.names
