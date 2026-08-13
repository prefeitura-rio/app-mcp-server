"""Testes da sonda de tabelas externas (src/health/external_tables.py).

O ponto central coberto aqui é o contrato que separa este módulo de
`checks.py`: a sondagem faz I/O num laço de background e o check só lê o
veredito já calculado.
"""

import asyncio
import sys
import types
from unittest.mock import AsyncMock

import pytest

from src.health import checks, external_tables
from src.health.models import CheckStatus, HealthCheckError

TABELA = external_tables.EXTERNAL_TABLES[0]

# Trecho da mensagem real do BigQuery quando a planilha some. Serve de
# sentinela: o ID não pode vazar para a resposta pública.
ERRO_REAL_DO_BIGQUERY = (
    "400 Error while reading table: rj-iplanrio.plus_codes.equipamentos_instrucoes, "
    "error message: Spreadsheet not found. "
    "File: 1VPnJSf9puDgZ-Ed9MRkpe3Jy38nKxGLp7O9-ydAdm98"
)


@pytest.fixture(autouse=True)
def _reset_state():
    external_tables.reset_state()
    yield
    external_tables.reset_state()


@pytest.fixture
def sem_alerta(monkeypatch):
    """Neutraliza o error interceptor e devolve as chamadas registradas."""
    enviados = []

    async def fake_send_general_error(**kwargs):
        enviados.append(kwargs)
        return True

    fake_module = types.ModuleType("src.utils.error_interceptor")
    fake_module.send_general_error = fake_send_general_error
    monkeypatch.setitem(sys.modules, "src.utils.error_interceptor", fake_module)
    return enviados


def _fake_bigquery(monkeypatch, erro=None):
    """Substitui a query real por um stub que responde ou levanta."""

    def fake_probe(table):
        if erro is not None:
            raise erro

    monkeypatch.setattr(external_tables, "_probe_table", fake_probe)


# --------------------------------------------------------------------------
# probe_external_tables
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_bem_sucedido_registra_veredito_limpo(monkeypatch):
    _fake_bigquery(monkeypatch)

    resultado = await external_tables.probe_external_tables()

    assert resultado == {TABELA: None}
    assert external_tables.last_result() == {TABELA: None}
    assert external_tables.seconds_since_last_probe() is not None


@pytest.mark.asyncio
async def test_probe_com_falha_guarda_apenas_o_nome_da_classe(monkeypatch):
    _fake_bigquery(monkeypatch, erro=RuntimeError(ERRO_REAL_DO_BIGQUERY))

    resultado = await external_tables.probe_external_tables()

    assert resultado == {TABELA: "RuntimeError"}
    # O ID da planilha fica no log, nunca no veredito.
    assert "1VPnJSf9" not in str(resultado)


@pytest.mark.asyncio
async def test_probe_usa_o_executor_e_nao_bloqueia_o_loop(monkeypatch):
    """O cliente do BigQuery é síncrono; o probe precisa sair do event loop."""
    chamadas = []
    loop = asyncio.get_running_loop()
    original = loop.run_in_executor

    def espiao(executor, func, *args):
        chamadas.append(func)
        return original(executor, func, *args)

    monkeypatch.setattr(loop, "run_in_executor", espiao)
    _fake_bigquery(monkeypatch)

    await external_tables.probe_external_tables()

    assert len(chamadas) == len(external_tables.EXTERNAL_TABLES)


# --------------------------------------------------------------------------
# check_external_tables (leitura do veredito, sem I/O)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_antes_do_primeiro_probe_e_skipped():
    # Ausência de sondagem não é falha: o pod acabou de subir.
    assert await checks.check_external_tables() is CheckStatus.SKIPPED


@pytest.mark.asyncio
async def test_check_com_veredito_limpo_vira_up(monkeypatch):
    _fake_bigquery(monkeypatch)
    await external_tables.probe_external_tables()

    assert await checks.check_external_tables() is CheckStatus.UP


@pytest.mark.asyncio
async def test_check_com_falha_expoe_so_o_nome_curto_da_tabela(monkeypatch):
    _fake_bigquery(monkeypatch, erro=RuntimeError(ERRO_REAL_DO_BIGQUERY))
    await external_tables.probe_external_tables()

    with pytest.raises(HealthCheckError) as exc_info:
        await checks.check_external_tables()

    mensagem = str(exc_info.value)
    assert "equipamentos_instrucoes" in mensagem
    # `/health/detail` é público: nem ID de planilha nem projeto/dataset.
    assert "1VPnJSf9" not in mensagem
    assert "rj-iplanrio" not in mensagem


@pytest.mark.asyncio
async def test_check_nao_faz_io(monkeypatch):
    """O check lê memória; se sondasse, quebraria o teto de tempo da rodada."""

    def nao_deve_ser_chamado(table):
        raise AssertionError("o check não pode sondar o BigQuery")

    _fake_bigquery(monkeypatch)
    await external_tables.probe_external_tables()
    monkeypatch.setattr(external_tables, "_probe_table", nao_deve_ser_chamado)

    assert await checks.check_external_tables() is CheckStatus.UP


# --------------------------------------------------------------------------
# Alerta na transição
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alerta_dispara_na_virada_up_para_down(sem_alerta):
    await external_tables._alert_transition({TABELA: None}, {TABELA: "RuntimeError"})

    assert len(sem_alerta) == 1
    assert TABELA in sem_alerta[0]["error_message"]
    assert sem_alerta[0]["error_type"] == "ExternalTableUnavailable"


@pytest.mark.asyncio
async def test_alerta_nao_repete_enquanto_a_falha_persiste(sem_alerta):
    # Sem isto, uma planilha fora do ar geraria um report a cada ciclo.
    await external_tables._alert_transition(
        {TABELA: "RuntimeError"}, {TABELA: "RuntimeError"}
    )

    assert sem_alerta == []


@pytest.mark.asyncio
async def test_primeira_sondagem_ja_falha_dispara_alerta(sem_alerta):
    # `previous is None` = pod recém-iniciado com a tabela já quebrada.
    await external_tables._alert_transition(None, {TABELA: "RuntimeError"})

    assert len(sem_alerta) == 1


@pytest.mark.asyncio
async def test_recuperacao_nao_dispara_alerta(sem_alerta):
    await external_tables._alert_transition({TABELA: "RuntimeError"}, {TABELA: None})

    assert sem_alerta == []


# --------------------------------------------------------------------------
# Laço de background
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_laco_sobrevive_a_um_ciclo_com_excecao(monkeypatch, sem_alerta):
    """Um ciclo ruim não pode congelar o veredito para sempre."""
    ciclos = []

    async def probe_instavel():
        ciclos.append(len(ciclos))
        if len(ciclos) == 1:
            raise RuntimeError("falha inesperada dentro do probe")
        return {TABELA: None}

    monkeypatch.setattr(external_tables, "probe_external_tables", probe_instavel)
    monkeypatch.setattr(external_tables, "_probe_interval_s", lambda: 0)

    task = asyncio.create_task(external_tables.run_probe_loop())
    while len(ciclos) < 3:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(ciclos) >= 3


@pytest.mark.asyncio
async def test_laco_pode_ser_cancelado(monkeypatch):
    monkeypatch.setattr(
        external_tables, "probe_external_tables", AsyncMock(return_value={TABELA: None})
    )
    monkeypatch.setattr(external_tables, "_alert_transition", AsyncMock())
    monkeypatch.setattr(external_tables, "_probe_interval_s", lambda: 3600)

    task = asyncio.create_task(external_tables.run_probe_loop())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------
# Intervalo configurável
# --------------------------------------------------------------------------


def test_intervalo_invalido_cai_no_padrao(monkeypatch):
    monkeypatch.setenv("EXTERNAL_TABLES_PROBE_INTERVAL_S", "nao-e-numero")

    assert external_tables._probe_interval_s() == (
        external_tables.DEFAULT_PROBE_INTERVAL_S
    )


def test_intervalo_curto_demais_e_elevado(monkeypatch):
    # Cada ciclo lê a planilha inteira; sondar de segundo em segundo viraria
    # carga contínua no BigQuery.
    monkeypatch.setenv("EXTERNAL_TABLES_PROBE_INTERVAL_S", "1")

    assert external_tables._probe_interval_s() == 10.0


def test_intervalo_valido_e_respeitado(monkeypatch):
    monkeypatch.setenv("EXTERNAL_TABLES_PROBE_INTERVAL_S", "60")

    assert external_tables._probe_interval_s() == 60.0


# --------------------------------------------------------------------------
# Registro
# --------------------------------------------------------------------------


def test_registro_em_producao_inclui_external_tables(monkeypatch):
    from src.health.registry import HealthRegistry

    env = types.SimpleNamespace(
        IS_LOCAL=False, KEYCLOAK_JWKS_URI="", KEYCLOAK_ISSUER=""
    )
    monkeypatch.setattr(sys.modules["src.config"], "env", env, raising=False)
    monkeypatch.setitem(sys.modules, "src.config.env", env)

    registry = HealthRegistry()
    mcp = types.SimpleNamespace(get_tools=AsyncMock(return_value={"t": 1}))
    checks.register_default_checks(mcp, registry)

    assert "external_tables" in registry.names


def test_registro_local_omite_external_tables(monkeypatch):
    from src.health.registry import HealthRegistry

    env = types.SimpleNamespace(IS_LOCAL=True, KEYCLOAK_JWKS_URI="", KEYCLOAK_ISSUER="")
    monkeypatch.setattr(sys.modules["src.config"], "env", env, raising=False)
    monkeypatch.setitem(sys.modules, "src.config.env", env)

    registry = HealthRegistry()
    mcp = types.SimpleNamespace(get_tools=AsyncMock(return_value={"t": 1}))
    checks.register_default_checks(mcp, registry)

    assert "external_tables" not in registry.names
