"""Sondagem periódica das tabelas externas do BigQuery (CHATR-119).

Por que este módulo existe separado de `checks.py`: a sondagem NÃO pode
acontecer dentro da rodada de `/health/detail`.

Duas restrições forçam isso:

1. `dry_run` não detecta a falha. Medido contra o GCP real: com a credencial
   sem o escopo `auth/drive`, o `dry_run` de uma query sobre
   `equipamentos_instrucoes` volta OK, enquanto a query real falha com
   `403 Permission denied while getting Drive credentials`. O planejamento da
   query não toca o Drive — só a execução toca. Logo, o `check_bigquery` de
   `checks.py`, que usa `dry_run`, jamais pegaria "Spreadsheet not found".
2. A query real custa ~2s, acima do timeout por check (2s) e praticamente
   igual ao teto global da rodada (3s) definidos em `registry.py`.

Por isso a query roda num laço de background, e o check exposto em
`/health/detail` apenas lê o veredito já calculado — sem I/O, sem risco de
pendurar a resposta e sem permitir que um endpoint público martele o BigQuery.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

from src.utils.infisical import getenv_or_action
from src.utils.log import logger

# Tabelas externas (Google Sheets) efetivamente consultadas pelo código.
# `plus_codes.equipamentos_controle_categorias` mora na mesma planilha e
# também é EXTERNAL, mas nenhuma tool a consulta — sondá-la gastaria uma
# query a cada ciclo para reportar algo que não afeta o serviço.
EXTERNAL_TABLES: tuple[str, ...] = ("rj-iplanrio.plus_codes.equipamentos_instrucoes",)

# Intervalo entre sondagens. Lido via `getenv_or_action` (e não em `env.py`)
# pelo mesmo motivo das variáveis de `registry.py`: mantém o módulo
# importável isoladamente nos testes unitários.
DEFAULT_PROBE_INTERVAL_S = 300.0


def _probe_interval_s() -> float:
    raw = getenv_or_action(
        "EXTERNAL_TABLES_PROBE_INTERVAL_S",
        default=str(DEFAULT_PROBE_INTERVAL_S),
        action="ignore",
    )
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            f"EXTERNAL_TABLES_PROBE_INTERVAL_S inválido ({raw!r}); "
            f"usando o padrão de {DEFAULT_PROBE_INTERVAL_S}s."
        )
        return DEFAULT_PROBE_INTERVAL_S

    # Um intervalo curto demais transformaria o probe em carga contínua no
    # BigQuery (cada ciclo lê a planilha inteira).
    if interval < 10.0:
        logger.warning(
            f"EXTERNAL_TABLES_PROBE_INTERVAL_S={interval}s é curto demais; "
            f"elevando para 10s."
        )
        return 10.0
    return interval


# Veredito da última sondagem: tabela → None se OK, ou o nome da classe da
# exceção se falhou. `None` no lugar do dict inteiro significa "ainda não
# sondado" (reportado como SKIPPED, não como falha).
_last_result: Optional[Dict[str, Optional[str]]] = None
_last_probe_at: Optional[float] = None


def reset_state() -> None:
    """Descarta o veredito guardado (usado nos testes)."""
    global _last_result, _last_probe_at
    _last_result = None
    _last_probe_at = None


def last_result() -> Optional[Dict[str, Optional[str]]]:
    """Veredito da última sondagem, ou `None` se nenhuma rodou ainda."""
    return _last_result


def seconds_since_last_probe() -> Optional[float]:
    if _last_probe_at is None:
        return None
    return time.monotonic() - _last_probe_at


def _probe_table(table: str) -> None:
    """Executa a query real contra uma tabela externa. Levanta em caso de falha.

    Usa `get_bigquery_client()` direto em vez de `get_bigquery_result()`: este
    último converte `NotFound` em lista vazia (degradação graciosa desejada nas
    tools, mas que aqui esconderia exatamente o que queremos detectar).

    A coluna é fixa e o `LIMIT 1` é simbólico — o BigQuery lê a planilha
    inteira de qualquer forma (~240 KB para esta tabela). O que importa é
    exercitar o caminho Drive → BigQuery.
    """
    from src.utils.bigquery import get_bigquery_client

    client = get_bigquery_client()
    query_job = client.query(f"SELECT * FROM `{table}` LIMIT 1")
    # `result()` é o que força a execução; sem ele o erro do Drive não aparece.
    list(query_job.result())


async def probe_external_tables() -> Dict[str, Optional[str]]:
    """Sonda todas as tabelas externas e atualiza o veredito guardado."""
    global _last_result, _last_probe_at

    loop = asyncio.get_running_loop()
    results: Dict[str, Optional[str]] = {}
    for table in EXTERNAL_TABLES:
        started = time.monotonic()
        try:
            # O cliente do BigQuery é síncrono; vai para o executor para não
            # bloquear o event loop (mesmo padrão de `checks.check_bigquery`).
            await loop.run_in_executor(None, _probe_table, table)
        except Exception as exc:  # noqa: BLE001 - o probe nunca derruba o laço
            elapsed_ms = int((time.monotonic() - started) * 1000)
            # A exceção completa (que carrega o ID da planilha) fica no log;
            # só o nome da classe entra no veredito, que chega ao
            # `/health/detail` público.
            logger.error(
                f"Tabela externa '{table}' indisponível após {elapsed_ms}ms: {exc!r}"
            )
            results[table] = type(exc).__name__
        else:
            results[table] = None

    _last_result = results
    _last_probe_at = time.monotonic()
    return results


def _failed_tables(results: Dict[str, Optional[str]]) -> list[str]:
    return [table for table, error in results.items() if error is not None]


async def _alert_transition(
    previous: Optional[Dict[str, Optional[str]]], current: Dict[str, Optional[str]]
) -> None:
    """Reporta ao error interceptor apenas na virada disponível → indisponível.

    Sem isto, uma planilha fora do ar geraria um report a cada ciclo pelo
    tempo que durasse a falha. O log de ERROR por ciclo permanece (é em
    `probe_external_tables`); o que é limitado à transição é o alerta.
    """
    from src.utils.error_interceptor import send_general_error

    failed_now = set(_failed_tables(current))
    failed_before = set(_failed_tables(previous)) if previous is not None else set()

    for table in sorted(failed_now - failed_before):
        await send_general_error(
            user_id="system",
            source={"source": "mcp", "tool": "health", "check": "external_tables"},
            error_type="ExternalTableUnavailable",
            error_message=f"Tabela externa indisponível: {table} ({current[table]})",
        )

    for table in sorted(failed_before - failed_now):
        logger.info(f"Tabela externa '{table}' voltou a responder.")


async def run_probe_loop() -> None:
    """Sonda no startup e depois a cada `EXTERNAL_TABLES_PROBE_INTERVAL_S`.

    Só é cancelado pelo lifespan (`asyncio.CancelledError` sai limpo); nenhuma
    outra exceção encerra o laço, para que uma falha isolada não deixe o
    veredito congelado para sempre.
    """
    interval = _probe_interval_s()
    logger.info(
        f"Sonda de tabelas externas iniciada (intervalo de {interval}s): "
        f"{', '.join(EXTERNAL_TABLES)}"
    )

    while True:
        previous = _last_result
        try:
            current = await probe_external_tables()
            await _alert_transition(previous, current)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - o laço nunca morre por um ciclo ruim
            logger.exception("Ciclo da sonda de tabelas externas falhou")

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("Sonda de tabelas externas encerrada.")
            raise
