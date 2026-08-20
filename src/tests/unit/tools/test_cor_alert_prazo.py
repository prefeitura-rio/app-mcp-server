"""Prazo de espera do registro de alerta do COR (CHATR-103).

Alerta de severidade alta/crítica não passa pelo buffer de agrupamento — vai
direto ao BigQuery, para existir numa tabela consultável durante a ocorrência.
O efeito colateral era a tool passar a esperar um insert com retry: com o
BigQuery fora, três tentativas com backoff, duas vezes (registro e fila de
despacho). Dezenas de segundos de espera para quem está relatando uma
emergência.

O que estes testes prendem:

1. O prazo vale. A tool responde mesmo com a escrita pendurada.
2. O prazo **não** cancela a escrita. É a diferença entre `wait_for` sozinho e
   `wait_for(shield(...))`: cancelar a corrotina antes de a thread do pool
   começar não deixaria rastro nenhum — nem linha no buffer, nem item na DLQ,
   porque ela nunca chegaria a falhar no BigQuery.
3. Quem chama não afirma "registrado" sobre escrita ainda em voo.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _ensure_package(name: str, path: Path) -> None:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg


def _passthrough_interceptor(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


def _carregar_cor_alert(monkeypatch, alias: str, prazo: float):
    """Carrega `src/tools/cor_alert_tools.py` com env sintético.

    `src.utils.background` fica **real** de propósito: a referência forte que
    ele mantém é parte do que se testa aqui. Com o shield descartado no
    estouro do prazo, ele é o único dono da task — sem ele, o coletor de lixo
    poderia levá-la no meio do caminho.
    """
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")

    registros = {"warning": [], "exception": []}

    env_module = types.SimpleNamespace(
        ENVIRONMENT="test",
        GOOGLE_MAPS_API_URL="https://maps.local/geocode",
        GOOGLE_MAPS_API_KEY="chave",
        COR_ALERT_WRITE_DEADLINE_SECONDS=prazo,
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            save_cor_alert_in_bq_background=lambda **_k: asyncio.sleep(0),
            save_cor_alert_to_queue_background=lambda **_k: asyncio.sleep(0),
            get_datetime=lambda: "2026-08-20T10:00:00.000000",
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_a, **_k: None,
                error=lambda *_a, **_k: None,
                warning=lambda msg, *_a, **_k: registros["warning"].append(msg),
                exception=lambda msg, *_a, **_k: registros["exception"].append(msg),
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=_passthrough_interceptor),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=object),
    )

    spec = importlib.util.spec_from_file_location(
        alias, PROJECT_ROOT / "src" / "tools" / "cor_alert_tools.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, registros


@pytest.mark.asyncio
async def test_prazo_estourado_devolve_o_controle_a_tool(monkeypatch):
    """Quem relata uma emergência não pode esperar o BigQuery voltar."""
    module, registros = _carregar_cor_alert(monkeypatch, "cor_prazo_curto", prazo=0.02)

    async def _escrita_lenta():
        await asyncio.sleep(5)

    inicio = asyncio.get_running_loop().time()
    confirmado = await module._registrar_com_prazo(
        _escrita_lenta(), nome="t", descricao="Registro do alerta X"
    )
    decorrido = asyncio.get_running_loop().time() - inicio

    assert confirmado is False
    assert decorrido < 1.0, f"a tool ficou presa por {decorrido:.2f}s"
    assert any("não confirmou" in msg for msg in registros["warning"])


@pytest.mark.asyncio
async def test_prazo_estourado_nao_cancela_a_escrita(monkeypatch):
    """O ponto do `shield`.

    `wait_for` sozinho cancelaria a corrotina, e uma escrita cancelada antes de
    chegar ao BigQuery não deixa nada para trás: nem linha no buffer, nem item
    na DLQ. Seria trocar espera longa por perda silenciosa — exatamente o que o
    CHATR-103 existe para eliminar.
    """
    module, _ = _carregar_cor_alert(monkeypatch, "cor_prazo_shield", prazo=0.02)

    concluidas = []

    async def _escrita_lenta():
        await asyncio.sleep(0.2)
        concluidas.append("gravou")

    confirmado = await module._registrar_com_prazo(
        _escrita_lenta(), nome="t", descricao="Registro do alerta X"
    )

    assert confirmado is False
    assert not concluidas, "a escrita terminou antes do prazo; o teste não prova nada"

    # A escrita segue viva depois de a tool ter respondido.
    for _ in range(100):
        await asyncio.sleep(0.01)
        if concluidas:
            break

    assert concluidas == ["gravou"], "a escrita foi cancelada junto com a espera"


@pytest.mark.asyncio
async def test_escrita_dentro_do_prazo_e_confirmada(monkeypatch):
    """O retorno é o que autoriza o log a dizer 'registrado'."""
    module, _ = _carregar_cor_alert(monkeypatch, "cor_prazo_ok", prazo=5.0)

    gravou = []

    async def _escrita_rapida():
        gravou.append(True)

    confirmado = await module._registrar_com_prazo(
        _escrita_rapida(), nome="t", descricao="Registro do alerta X"
    )

    assert confirmado is True
    assert gravou == [True]


@pytest.mark.asyncio
async def test_falha_da_escrita_nao_derruba_a_resposta_da_tool(monkeypatch):
    """O alerta em si é o que importa entregar; a escrita tem retry e DLQ."""
    module, registros = _carregar_cor_alert(monkeypatch, "cor_prazo_erro", prazo=5.0)

    async def _escrita_que_falha():
        raise RuntimeError("BigQuery recusou")

    confirmado = await module._registrar_com_prazo(
        _escrita_que_falha(), nome="t", descricao="Registro do alerta X"
    )

    assert confirmado is False
    assert registros["exception"], "a falha passou sem registro"


@pytest.mark.asyncio
async def test_task_fica_referenciada_enquanto_a_escrita_corre(monkeypatch):
    """A event loop guarda só referência fraca; sem dono, a task some no GC."""
    from src.utils.background import tarefas_em_voo

    module, _ = _carregar_cor_alert(monkeypatch, "cor_prazo_ref", prazo=0.02)

    em_voo_antes = tarefas_em_voo()

    async def _escrita_lenta():
        await asyncio.sleep(0.2)

    await module._registrar_com_prazo(
        _escrita_lenta(), nome="t", descricao="Registro do alerta X"
    )

    assert tarefas_em_voo() > em_voo_antes, (
        "a escrita ficou sem dono depois de o prazo estourar"
    )

    for _ in range(100):
        await asyncio.sleep(0.01)
        if tarefas_em_voo() == em_voo_antes:
            break

    assert tarefas_em_voo() == em_voo_antes, "a task não foi solta ao terminar"
