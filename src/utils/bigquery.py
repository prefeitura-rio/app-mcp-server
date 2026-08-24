import asyncio
import atexit
import contextlib
import functools
import hashlib
import random
import re
import signal
import threading
import uuid

# `time` (o módulo) sob alias porque `datetime.time` (a classe) já ocupa o nome
# neste módulo — ver o import de `datetime` mais abaixo e `_normalize_bq_value`.
import time as _time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from google.cloud import bigquery
from google.api_core.exceptions import (
    ClientError,
    GoogleAPIError,
    NotFound,
    TooManyRequests,
)
from google.oauth2 import service_account
from opentelemetry.trace import Status, StatusCode
from typing import List, NamedTuple
import base64
import json
import src.config.env as env
from datetime import datetime, date, time
import pytz
from src.observability.tracing import get_tracer
from src.utils.log import logger
from src.utils.error_interceptor import interceptor
from src.utils.json_utils import CustomJSONEncoder


@functools.lru_cache(maxsize=1)
def get_bigquery_client() -> bigquery.Client:
    """Get the BigQuery client.

    The client is constructed once per process and cached for reuse across all
    call sites.  ``lru_cache`` provides thread-safe initialisation in CPython
    (the GIL ensures only one thread executes the body for a given argument
    set), so concurrent callers from ``loop.run_in_executor`` threads will all
    receive the same instance without any additional locking.

    Credential rotation is handled by the Argo Rollouts / Infisical
    ``auto-reload`` annotation, which triggers a full pod restart on secret
    change — so the cached client is always constructed with the latest
    credentials at startup.

    Returns:
        bigquery.Client: The BigQuery client (singleton for the process lifetime)
    """
    credentials = get_gcp_credentials(
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/cloud-platform",
        ]
    )
    return bigquery.Client(credentials=credentials, project=credentials.project_id)


def get_gcp_credentials(scopes: List[str] = None) -> service_account.Credentials:
    """Get the GCP credentials.

    Args:
        scopes (List[str], optional): The scopes to use. Defaults to None.

    Returns:
        service_account.Credentials: The GCP credentials.
    """
    info: dict = json.loads(base64.b64decode(env.GCP_SERVICE_ACCOUNT_CREDENTIALS))
    creds = service_account.Credentials.from_service_account_info(info)
    if scopes:
        creds = creds.with_scopes(scopes)
    return creds


def get_datetime() -> str:
    timestamp = datetime.now(pytz.timezone("America/Sao_Paulo"))
    return timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f")


# ---------------------------------------------------------------------------
# Contadores de escrita.
#
# O critério de aceite do CHATR-118 é que o volume de inserts caia "de forma
# mensurável" — o que exige medir. Sem isto o ganho do batching é indistinguível
# de uma regressão silenciosa (buffer que não escoa também reduz inserts).
# `rows_enqueued / insert_calls` é a taxa de agrupamento efetiva; a diferença
# entre `rows_enqueued` e `rows_written + rows_dropped_to_dlq` é o que está
# parado em memória neste instante.
# ---------------------------------------------------------------------------

_metrics_lock = threading.Lock()
_write_metrics = {
    "rows_enqueued": 0,  # linhas que entraram no buffer
    "rows_written": 0,  # linhas confirmadas no BigQuery
    "insert_calls": 0,  # chamadas de insert_rows_json efetivamente feitas
    "flush_calls": 0,  # execuções do flush (periódico, por tamanho ou shutdown)
    "rows_to_dlq": 0,  # linhas que caíram na DLQ após esgotar o retry
    "rows_evicted": 0,  # linhas expulsas do buffer por estouro de teto
    "rows_replayed": 0,  # linhas devolvidas ao BigQuery a partir da DLQ
    "dlq_items_dropped": 0,  # itens expulsos da DLQ/poison pelo teto
}


def _bump_metric(name: str, amount: int = 1) -> None:
    """Incrementa um contador. Chamado de várias threads, por isso o lock."""
    with _metrics_lock:
        _write_metrics[name] = _write_metrics.get(name, 0) + amount


def get_bigquery_write_metrics() -> dict:
    """Fotografia dos contadores, incluindo o que ainda está em memória.

    O lock do buffer é adquirido com prazo, como em todo o resto do módulo. A
    razão aqui é específica: esta função é servida por `/health/detail`, ou
    seja, roda na event loop. Um `acquire()` sem teto converteria contenção do
    buffer — que acontece justamente durante um flush lento — em rota de
    diagnóstico pendurada, e o diagnóstico ficaria indisponível exatamente no
    momento em que é preciso. Sem o lock, devolvemos o resto dos contadores e
    sinalizamos a ausência em vez de mentir um zero.

    `taxa_agrupamento` é o critério de aceite do CHATR-118 em um número:
    quantas linhas, em média, cada chamada de insert levou. Antes do batching
    valia 1,0 por construção.
    """
    with _metrics_lock:
        snapshot = dict(_write_metrics)

    if _batch_buffer_lock.acquire(timeout=_METRICS_LOCK_TIMEOUT_SECONDS):
        try:
            snapshot["rows_buffered"] = sum(
                len(rows) for rows in _batch_buffer.values()
            )
            snapshot["tables_buffered"] = sum(
                1 for rows in _batch_buffer.values() if rows
            )
        finally:
            _batch_buffer_lock.release()
    else:
        # `None` e não `0`: quem lê precisa distinguir "buffer vazio" de "não
        # deu para medir", sob risco de concluir que não há linha parada.
        snapshot["rows_buffered"] = None
        snapshot["tables_buffered"] = None

    chamadas = snapshot.get("insert_calls") or 0
    snapshot["taxa_agrupamento"] = (
        round(snapshot.get("rows_written", 0) / chamadas, 2) if chamadas else 0.0
    )
    return snapshot


def reset_bigquery_write_metrics() -> None:
    """Zera os contadores. Existe para isolamento entre testes."""
    with _metrics_lock:
        for key in _write_metrics:
            _write_metrics[key] = 0


# ---------------------------------------------------------------------------
# Executor dedicado às escritas.
#
# Simétrico ao pool de leitura (ver `_get_read_executor`), e pelo mesmo motivo
# invertido: `insert_rows_json_with_retry_and_dlq` dorme em `_time.sleep` entre
# as tentativas, então uma indisponibilidade do BigQuery segura cada thread por
# até ~1,5s. No executor default do loop — que é o que estas escritas usavam —
# essas threads são as mesmas de qualquer outra chamada bloqueante do app, e uma
# rajada de falha de log passava a atrasar coisa que nada tem a ver com log.
#
# Pequeno de propósito: escrita de log é assíncrona ao usuário, não precisa de
# paralelismo alto, e um teto baixo mantém a fila visível em vez de espalhar a
# espera por todo o pool.
# ---------------------------------------------------------------------------

_write_executor: ThreadPoolExecutor = None
_write_executor_lock = threading.Lock()


def _get_write_executor() -> ThreadPoolExecutor:
    """Devolve (criando na primeira vez) o pool de threads das escritas."""
    global _write_executor
    if _write_executor is not None:
        return _write_executor
    with _write_executor_lock:
        if _write_executor is None:
            max_workers = int(getattr(env, "BIGQUERY_WRITE_MAX_WORKERS", 4))
            _write_executor = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="bq-write"
            )
    return _write_executor


def _shutdown_write_executor() -> None:
    """Encerra o pool no fim do processo, sem esperar escrita pendurada.

    Roda *depois* do flush final: quem chama é `_stop_batch_flush_thread`, que
    já drenou o buffer de forma síncrona. Aqui só restam threads presas em
    chamada que não voltou, e esperar por elas atrasaria o encerramento do pod
    sem salvar nada.
    """
    global _write_executor
    executor, _write_executor = _write_executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def _drenar_pool_de_escrita(timeout: float) -> None:
    """Espera, com prazo, o que já foi submetido ao pool chegar ao buffer.

    Roda *antes* do flush final, e é o que fecha a última janela de perda do
    caminho de escrita. Uma escrita disparada em background é submetida ao pool
    e só entra no buffer quando a thread de fato executa `save_*_in_bq`. No
    encerramento, o que ainda estava na fila do executor era cancelado por
    `_shutdown_write_executor` (`cancel_futures=True`): a linha não estava no
    buffer, então o flush não a alcançava, e nunca chegou a falhar no BigQuery,
    então também não ia para a DLQ. Sumia em silêncio — a mesma perda que todo
    o resto deste módulo existe para eliminar, um passo depois da proteção.

    O prazo é obrigatório e curto. Esta função roda na thread principal, dentro
    do handler de sinal: esperar sem teto converteria uma escrita pendurada em
    pod que não termina, e o SIGKILL que viria em seguida levaria junto o buffer
    inteiro. Quem não escoou dentro do prazo é cancelado logo depois, como já
    era — o prazo aumenta o que se salva, não garante tudo.

    `shutdown(wait=True)` bloqueia sem opção de teto, por isso a espera vai para
    uma thread auxiliar e o prazo é aplicado no `Event`. Fechar o pool para
    novas submissões aqui é justamente o desejado: neste ponto o processo está
    terminando, e o flush seguinte escreve direto, sem passar pelo executor.
    """
    executor = _write_executor
    if executor is None or timeout <= 0:
        return

    drenado = threading.Event()

    def _esperar() -> None:
        try:
            executor.shutdown(wait=True)
        except Exception:
            pass
        finally:
            drenado.set()

    threading.Thread(target=_esperar, name="bq-write-drain", daemon=True).start()

    if not drenado.wait(timeout):
        logger.warning(
            f"Pool de escrita do BigQuery não escoou em {timeout}s no encerramento; "
            "as escritas ainda na fila serão canceladas. As que já entraram no "
            "buffer seguem para o flush final."
        )


# ---------------------------------------------------------------------------
# Buffer de agrupamento.
#
# O lock é adquirido com prazo em vez de indefinidamente. O motivo é o caminho
# de shutdown: o flush final roda dentro de um handler de sinal, ou seja, na
# thread principal, entre bytecodes de qualquer coisa que estivesse rodando.
# Um `acquire()` sem teto ali converteria contenção momentânea em pod que não
# termina — e o Kubernetes resolveria isso com SIGKILL, que é exatamente o
# cenário de perda que este trabalho existe para eliminar.
# ---------------------------------------------------------------------------

_batch_buffer_lock = threading.Lock()
_batch_buffer: dict = {}

_BUFFER_LOCK_TIMEOUT_SECONDS = 5.0

# Prazo bem mais curto para a leitura de métricas, que roda na event loop
# (`/health/detail`). O buffer nunca é segurado durante I/O — tanto
# `enqueue_bigquery_row` quanto `flush_bigquery_batch_buffer` soltam o lock
# antes de falar com o BigQuery —, então a seção crítica é só CPU e 250ms já é
# folgado. O que este teto compra é o pior caso: a rota de diagnóstico não
# segura a event loop por mais que isso, aconteça o que acontecer no buffer.
_METRICS_LOCK_TIMEOUT_SECONDS = 0.25

# ---------------------------------------------------------------------------
# Background flush thread — drains the batch buffer periodically so rows are
# not stranded in memory when volume is too low to hit the batch_size threshold.
# _start_batch_flush_thread() is called at the bottom of this module, after
# flush_bigquery_batch_buffer is defined.
# ---------------------------------------------------------------------------

_flush_thread: threading.Thread | None = None
_flush_stop_event = threading.Event()


def _flush_interval_seconds() -> float:
    """Intervalo do flush periódico, lido do ambiente a cada início de laço.

    Lido por chamada, e não uma vez em constante de módulo, para que o valor
    valha também quando o módulo é recarregado com um `env` sintético — é assim
    que os testes exercitam o laço sem esperar 30 segundos reais.
    """
    try:
        return float(getattr(env, "BIGQUERY_FLUSH_INTERVAL_SECONDS", 30.0))
    except (TypeError, ValueError):
        return 30.0


def _flush_loop() -> None:
    """Run in a daemon thread; flushes all pending rows every interval."""
    while not _flush_stop_event.wait(timeout=_flush_interval_seconds()):
        try:
            flush_bigquery_batch_buffer()
        except Exception:
            pass  # errors already logged inside flush_bigquery_batch_buffer


def _start_batch_flush_thread() -> None:
    """Start the periodic flush daemon thread (idempotent)."""
    global _flush_thread
    if _flush_thread is not None and _flush_thread.is_alive():
        return
    _flush_stop_event.clear()
    _flush_thread = threading.Thread(
        target=_flush_loop, name="bq-batch-flusher", daemon=True
    )
    _flush_thread.start()


def _stop_batch_flush_thread() -> None:
    """Sinaliza a parada do laço e faz o flush final, síncrono.

    `max_retries=1` no flush de encerramento é deliberado: aqui o orçamento não
    é "conseguir escrever", é "não estourar o `terminationGracePeriod`". Uma
    tentativa que falha cai imediatamente na DLQ, que é recuperável; insistir
    com backoff arriscaria o SIGKILL no meio do caminho e aí não sobraria nem a
    DLQ.

    A ordem dos três passos é o que faz o encerramento não perder linha:
    primeiro deixa o pool escoar (o que estava na fila entra no buffer), depois
    esvazia o buffer, e só então cancela o que sobrou. Invertida, cada etapa
    descartaria o trabalho da anterior.
    """
    _flush_stop_event.set()
    _drenar_pool_de_escrita(float(getattr(env, "BIGQUERY_SHUTDOWN_DRAIN_SECONDS", 3.0)))
    try:
        flush_bigquery_batch_buffer(
            max_retries=1,
            initial_delay=0.0,
            timeout=float(getattr(env, "BIGQUERY_SHUTDOWN_TIMEOUT_SECONDS", 5.0)),
        )
    except Exception:
        pass
    _shutdown_write_executor()


# ---------------------------------------------------------------------------
# Flush no encerramento por sinal.
#
# `atexit` sozinho não cobre o caso real. Como `src/app.py` já documenta, a
# uvicorn instala o próprio handler de SIGTERM e, ao sair de `serve()`,
# restaura o handler anterior e faz `signal.raise_signal()`. Se o handler
# anterior for o default, o processo morre ali — sem passar por `atexit` — e
# tudo que estava no buffer some em silêncio a cada rollout.
#
# A saída é instalar o nosso handler *antes* da uvicorn: é ele que a uvicorn
# guarda como "anterior", restaura e re-levanta. Assim o flush acontece no
# caminho que de fato executa, e em seguida delegamos ao handler que estava lá
# antes de nós, preservando a semântica original de encerramento.
# ---------------------------------------------------------------------------

_previous_signal_handlers: dict = {}
_signal_handlers_installed = False


def _handle_shutdown_signal(signum, frame) -> None:
    """Drena o buffer e devolve o controle ao handler anterior."""
    try:
        _stop_batch_flush_thread()
    except Exception:
        pass  # nunca deixar o encerramento falhar por causa do flush

    previous = _previous_signal_handlers.get(signum, signal.SIG_DFL)
    if callable(previous):
        previous(signum, frame)
        return
    if previous == signal.SIG_IGN:
        return
    # SIG_DFL (ou None, que o CPython usa para "handler default"): restaura o
    # comportamento padrão e re-levanta, para o processo morrer com o mesmo
    # código de saída que teria sem a nossa interposição.
    with contextlib.suppress(Exception):
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)


# Marca o handler como nosso. Serve para reconhecê-lo quando este módulo é
# carregado mais de uma vez no mesmo processo (os testes recarregam o arquivo
# sob outro nome para exercitá-lo com um `env` sintético): sem a marca, cada
# carga encadearia mais um handler sobre o anterior e o encerramento passaria
# por uma pilha de flushes de módulos-fantasma.
_handle_shutdown_signal._bq_flush_handler = True


def _install_shutdown_signal_handlers() -> None:
    """Instala o flush em SIGTERM/SIGINT (idempotente e best-effort).

    `signal.signal` só funciona na thread principal; em worker, em teste sob
    pytest-xdist ou em uso embarcado deste módulo a instalação simplesmente não
    acontece, e o `atexit` volta a ser a única rede — que é o comportamento
    anterior, não uma regressão.
    """
    global _signal_handlers_installed
    if _signal_handlers_installed:
        return
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            atual = signal.getsignal(sig)
            if getattr(atual, "_bq_flush_handler", False):
                # Já há um handler nosso (outra carga deste módulo). Encadear
                # em cima dele não acrescenta nada e só alonga o encerramento.
                continue
            _previous_signal_handlers[sig] = signal.signal(sig, _handle_shutdown_signal)
        except (ValueError, OSError, RuntimeError) as e:
            logger.warning(f"Não foi possível instalar handler para {sig!r}: {e}")
    _signal_handlers_installed = True


_sync_redis_client = None
_sync_redis_lock = threading.Lock()


def _get_sync_redis_client():
    """Return a process-wide synchronous Redis client, or None if unavailable.

    Os timeouts de socket são obrigatórios pelo mesmo motivo do cliente async
    do cache (ver ``get_async_redis_client``), mas com uma consequência pior:
    este cliente roda em thread do executor de escrita, chamado por
    ``_persist_to_dlq`` no caminho de falha de escrita. Sem timeout, um Redis
    que aceita a conexão e para de responder prende a thread indefinidamente —
    e, como a chamada nunca retorna, o fallback em arquivo logo abaixo (que
    existe justamente para quando o Redis não está disponível) nunca chega a
    ser tentado. O resultado é o pior possível: thread perdida *e* registro
    perdido.
    """
    global _sync_redis_client
    if _sync_redis_client is not None:
        return _sync_redis_client
    with _sync_redis_lock:
        if _sync_redis_client is not None:
            return _sync_redis_client
        try:
            import redis as _redis_lib

            redis_url = getattr(env, "REDIS_URL", None)
            if redis_url:
                timeout = getattr(env, "REDIS_DLQ_TIMEOUT_SECONDS", 5.0)
                _sync_redis_client = _redis_lib.Redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=timeout,
                    socket_timeout=timeout,
                )
        except Exception as e:
            logger.warning(f"Could not initialize sync Redis client: {e}")
    return _sync_redis_client


# Nomes de tabela vêm de constantes internas, nunca de entrada de usuário — mas
# o nome vira caminho de arquivo e chave de Redis, e uma travessia de diretório
# aqui seria escrita arbitrária no container. A allowlist custa nada e fecha a
# categoria inteira, sem depender de a origem continuar confiável no futuro.
_TABELA_SEGURA = re.compile(r"[^A-Za-z0-9_.\-]")

DLQ_KEY_PREFIX = "bq_dlq"
DLQ_POISON_KEY_PREFIX = "bq_dlq_poison"


def _sanitize_table_name(table_full_name: str) -> str:
    """Reduz o nome da tabela ao conjunto seguro para chave e nome de arquivo."""
    return _TABELA_SEGURA.sub("_", str(table_full_name))[:200]


def _dlq_key(table_full_name: str) -> str:
    return f"{DLQ_KEY_PREFIX}:{_sanitize_table_name(table_full_name)}"


def _dlq_poison_key(table_full_name: str) -> str:
    return f"{DLQ_POISON_KEY_PREFIX}:{_sanitize_table_name(table_full_name)}"


def _dlq_dir():
    """Diretório do fallback em arquivo da DLQ."""
    from pathlib import Path

    data_dir_path = getattr(env, "DATA_DIR", None) or "scratch"
    return Path(data_dir_path) / "bq_dlq"


def _dlq_file_path(table_full_name: str):
    """Caminho do arquivo de fallback da DLQ para uma tabela."""
    nome = _sanitize_table_name(table_full_name).replace(".", "_")
    return _dlq_dir() / f"dlq_{nome}.jsonl"


# Tamanho suposto de uma linha da DLQ, usado só para decidir *quando* conferir o
# teto — nunca para decidir o que cortar. Ver `_anexar_com_teto`.
_DLQ_LINHA_MEDIA_BYTES = 2048

# Sufixo do arquivo em reprocessamento. Ver `_replay_dlq_arquivos`: renomear
# antes de ler é o que separa o que está sendo drenado do que continua
# chegando, para o rewrite final não apagar linha nova.
_SUFIXO_EM_PROCESSAMENTO = ".processing"


def _anexar_com_teto(caminho, linha: str, rotulo: str) -> int:
    """Acrescenta uma linha ao arquivo mantendo o teto. Devolve o que cortou.

    Mesmo teto do Redis (`BIGQUERY_DLQ_MAX_ITEMS`) e mesma consequência: o que
    for cortado é perda definitiva e sai como `logger.critical`. Sem isto, o
    fallback em arquivo — que roda justamente quando o Redis está fora, ou seja,
    pode rodar por muito tempo — cresceria até encher o disco do container.

    O teto é conferido por tamanho em bytes e aplicado por número de linhas. A
    razão é custo: contar linhas exige ler o arquivo inteiro, e este código roda
    no caminho de falha de escrita, que pode estar sendo exercitado a cada
    requisição. Um `stat()` por append é barato; a leitura completa só acontece
    quando o arquivo passa de `BIGQUERY_DLQ_MAX_ITEMS * _DLQ_LINHA_MEDIA_BYTES`.

    A consequência do atalho é conhecida e aceitável: com linhas bem menores que
    a média suposta, o arquivo pode passar do teto em número de itens antes da
    primeira conferência — mas, nesse caso, ele é pequeno em bytes, que é o que
    ameaça o disco. Nem o limite de disco nem a retenção de dado pessoal (que o
    TTL cobre) ficam expostos.
    """
    max_items = int(getattr(env, "BIGQUERY_DLQ_MAX_ITEMS", 1000))

    caminho.parent.mkdir(parents=True, exist_ok=True)
    with open(caminho, "a", encoding="utf-8") as f:
        f.write(linha + "\n")

    if max_items <= 0:
        return 0

    try:
        if caminho.stat().st_size <= max_items * _DLQ_LINHA_MEDIA_BYTES:
            return 0
    except OSError:
        return 0

    try:
        linhas = [
            existente
            for existente in caminho.read_text(encoding="utf-8").splitlines()
            if existente.strip()
        ]
        if len(linhas) <= max_items:
            return 0
        descartadas = len(linhas) - max_items
        # Corta as mais antigas, como o `LTRIM(-max, -1)` do Redis.
        caminho.write_text("\n".join(linhas[-max_items:]) + "\n", encoding="utf-8")
    except OSError as e:
        logger.error(f"Não foi possível aplicar o teto em {caminho.name}: {e}")
        return 0

    _bump_metric("dlq_items_dropped", descartadas)
    logger.critical(
        f"DLQ em arquivo de {rotulo} atingiu o teto de {max_items} itens: "
        f"{descartadas} item(ns) mais antigo(s) foram DESCARTADOS definitivamente "
        f"({caminho.name}). Reprocesse a DLQ ou aumente BIGQUERY_DLQ_MAX_ITEMS."
    )
    return descartadas


def _alertar_descarte_por_teto(resultado, chave: str, max_items: int) -> int:
    """Registra o que o `LTRIM` do teto cortou da fila. Devolve quantos foram.

    Contrapartida em Redis do `_anexar_com_teto`, e existe pelo mesmo motivo: o
    corte é perda definitiva de dado e precisa aparecer como tal. Sem isto o
    item mais antigo some do servidor sem deixar rastro nenhum — o operador não
    tem como saber que faltou algo, muito menos o quê.

    Como funciona: o RPUSH devolve o tamanho da lista *antes* do LTRIM, e o
    pipeline começa por ele nos três caminhos com teto (gravação na DLQ, envio
    ao poison e reenfileiramento de volta). O que passar de `max_items` nesse
    número é exatamente o que o LTRIM seguinte descartou.

    Deve ser chamado *fora* do `try` do pipeline: aqui só se loga, e uma falha
    neste ponto não pode ser confundida com falha da gravação.
    """
    if max_items <= 0:
        return 0

    tamanho_apos_push = resultado[0] if resultado else 0
    if not isinstance(tamanho_apos_push, int) or tamanho_apos_push <= max_items:
        return 0

    descartados = tamanho_apos_push - max_items
    _bump_metric("dlq_items_dropped", descartados)
    logger.critical(
        f"A fila {chave} atingiu o teto de {max_items} itens: {descartados} "
        f"item(ns) mais antigo(s) foram DESCARTADOS definitivamente. Reprocesse "
        f"a DLQ ou aumente BIGQUERY_DLQ_MAX_ITEMS."
    )
    return descartados


def _expirar_arquivos_dlq() -> None:
    """Remove arquivos de DLQ mais velhos que `BIGQUERY_DLQ_TTL_SECONDS`.

    Contrapartida do `EXPIRE` aplicado às chaves do Redis, e existe pelos dois
    mesmos motivos: impedir que o fallback ocupe disco indefinidamente e limitar
    por quanto tempo o payload — que carrega dado pessoal (`user_id` é telefone,
    alerta do COR tem endereço e coordenada) — fica retido.

    O relógio é o `mtime`, que avança a cada append: um arquivo que ainda recebe
    escrita nunca expira, igual à chave do Redis, cujo TTL é renovado a cada
    gravação. Só some o que parou de ser alimentado e ninguém reprocessou.
    """
    ttl = int(getattr(env, "BIGQUERY_DLQ_TTL_SECONDS", 604800))
    if ttl <= 0:
        return

    dlq_dir = _dlq_dir()
    if not dlq_dir.is_dir():
        return

    limite = _time.time() - ttl
    for arquivo in dlq_dir.glob("dlq_*.jsonl*"):
        try:
            if arquivo.stat().st_mtime >= limite:
                continue
            linhas = sum(
                1
                for linha in arquivo.read_text(encoding="utf-8").splitlines()
                if linha.strip()
            )
            arquivo.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"Não foi possível expirar {arquivo.name}: {e}")
            continue
        logger.critical(
            f"DLQ em arquivo {arquivo.name} expirou após {ttl}s sem escrita nem "
            f"reprocessamento: {linhas} item(ns) DESCARTADOS definitivamente."
        )


def _persist_to_dlq(
    table_full_name: str, json_data: List[dict], error_msg: str
) -> None:
    """
    Persists failed payload to Redis Dead-Letter Queue (DLQ) or fallback file storage.

    Priority:
    1. Redis (reuses process-wide singleton client)
    2. Local .jsonl file under DATA_DIR/bq_dlq/ (always has a safe default path)

    A gravação no Redis leva teto de itens e validade junto, na mesma pipeline.
    O teto protege a instância — que é compartilhada com o cache de queries — de
    virar refém de uma indisponibilidade longa do BigQuery; a validade limita
    por quanto tempo o payload, que carrega dado pessoal (user_id é telefone,
    alerta do COR tem endereço e coordenada), fica retido.
    """
    dlq_item = {
        "table_full_name": table_full_name,
        "failed_at": get_datetime(),
        "error": error_msg,
        "payload": json_data,
    }
    serialized = json.dumps(dlq_item, cls=CustomJSONEncoder)
    _bump_metric("rows_to_dlq", len(json_data))

    pushed = False
    try:
        r = _get_sync_redis_client()
        if r is not None:
            chave = _dlq_key(table_full_name)
            max_items = int(getattr(env, "BIGQUERY_DLQ_MAX_ITEMS", 1000))
            ttl = int(getattr(env, "BIGQUERY_DLQ_TTL_SECONDS", 604800))

            pipe = r.pipeline()
            pipe.rpush(chave, serialized)
            pipe.ltrim(chave, -max_items, -1)
            if ttl > 0:
                pipe.expire(chave, ttl)
            resultado = pipe.execute()
            pushed = True

            _alertar_descarte_por_teto(resultado, chave, max_items)

            logger.error(
                f"Falha definitiva de escrita no BigQuery ({table_full_name}). "
                f"{len(json_data)} registro(s) salvos na DLQ Redis (chave: {chave}). Erro: {error_msg}"
            )
    except Exception as redis_err:
        logger.warning(f"Não foi possível salvar na DLQ do Redis: {redis_err}")

    if not pushed:
        try:
            dlq_file = _dlq_file_path(table_full_name)
            _anexar_com_teto(dlq_file, serialized, table_full_name)
            logger.error(
                f"Falha definitiva de escrita no BigQuery ({table_full_name}). "
                f"{len(json_data)} registro(s) salvos na DLQ em arquivo ({dlq_file}). Erro: {error_msg}"
            )
        except Exception as file_err:
            logger.error(f"CRÍTICO: Falha ao salvar DLQ em arquivo: {file_err}")


class BigQueryRowRejectedError(Exception):
    """Linha recusada pelo BigQuery por conteúdo (schema, tipo, valor).

    Separada das falhas de transporte porque a decisão de reprocessamento é
    oposta: repetir uma linha malformada nunca vai dar certo, então insistir só
    trava a fila atrás dela. Ver `_e_falha_permanente`.
    """


def _e_falha_permanente(exc: BaseException) -> bool:
    """Diz se repetir esta escrita é inútil.

    Erro 4xx do BigQuery (fora 429) e linha recusada por schema não melhoram
    com o tempo. Sem essa distinção, um único payload malformado no início da
    DLQ bloquearia para sempre tudo o que veio depois dele.
    """
    if isinstance(exc, BigQueryRowRejectedError):
        return True
    if isinstance(exc, TooManyRequests):
        return False
    return isinstance(exc, ClientError)


def _insert_rows_json_raw(
    table_full_name: str,
    json_data: List[dict],
    max_retries: int = 3,
    initial_delay: float = 0.5,
    timeout: float = None,
) -> None:
    """Insere no BigQuery com backoff exponencial e propaga a falha.

    Sem DLQ de propósito: é o núcleo compartilhado entre a escrita corrente
    (que encaminha para a DLQ ao falhar) e o reprocessamento da DLQ (que
    precisa deixar o item onde está para tentar de novo depois).

    O `timeout` não é opcional na prática. Sem ele a chamada herda o default do
    transporte e pode simplesmente não voltar — e este mesmo caminho é usado
    pelo flush de encerramento, que roda dentro do handler de sinal: uma
    chamada pendurada ali segura o processo até o SIGKILL, que é exatamente a
    perda que a DLQ existe para evitar.
    """
    if not json_data:
        return

    if timeout is None:
        timeout = float(getattr(env, "BIGQUERY_WRITE_TIMEOUT_SECONDS", 10.0))

    client = get_bigquery_client()
    last_exception = None

    for attempt in range(1, max_retries + 1):
        try:
            _bump_metric("insert_calls")
            errors = client.insert_rows_json(
                table_full_name, json_data, timeout=timeout
            )
            if not errors:
                _bump_metric("rows_written", len(json_data))
                return
            error_msgs = [
                f"Row {e.get('index', '?')}: {e.get('errors', e)}" for e in errors
            ]
            raise BigQueryRowRejectedError(
                f"Erro ao inserir no BigQuery: {'; '.join(error_msgs)}"
            )
        except Exception as e:
            last_exception = e
            logger.warning(
                f"Tentativa {attempt}/{max_retries} de inserção no BigQuery falhou para {table_full_name}: {e}"
            )
            # Falha permanente não melhora com espera; ir direto ao desfecho
            # evita segurar a thread do pool de escrita por nada.
            if _e_falha_permanente(e):
                break
            if attempt < max_retries:
                _time.sleep(initial_delay * (2 ** (attempt - 1)))

    raise last_exception


def insert_rows_json_with_retry_and_dlq(
    table_full_name: str,
    json_data: List[dict],
    max_retries: int = 3,
    initial_delay: float = 0.5,
    timeout: float = None,
) -> None:
    """
    Inserts rows into BigQuery with exponential backoff retries and Dead-Letter Queue (DLQ) fallback.
    """
    try:
        _insert_rows_json_raw(
            table_full_name,
            json_data,
            max_retries=max_retries,
            initial_delay=initial_delay,
            timeout=timeout,
        )
    except Exception as e:
        _persist_to_dlq(table_full_name, json_data, str(e))
        raise


def enqueue_bigquery_row(
    table_full_name: str, row: dict, batch_size: int = None
) -> None:
    """
    Enqueues a row to the BigQuery batch buffer.
    Flushes automatically when pending rows reach batch_size threshold.
    """
    batch_size = (
        batch_size
        if batch_size is not None
        else getattr(env, "BIGQUERY_BATCH_SIZE", 50)
    )
    max_buffered = int(getattr(env, "BIGQUERY_BATCH_MAX_BUFFERED_ROWS", 10000))

    rows_to_flush = None
    evicted: list = []
    if not _batch_buffer_lock.acquire(timeout=_BUFFER_LOCK_TIMEOUT_SECONDS):
        # Não segurar a thread do pool de escrita esperando um lock que não
        # vem: mandar a linha direto ao BigQuery preserva o dado, e perder o
        # agrupamento é o menor dos males neste caminho (que não deve ocorrer).
        logger.warning(
            f"Buffer de lote indisponível para {table_full_name}; escrevendo a linha direto."
        )
        insert_rows_json_with_retry_and_dlq(table_full_name, [row])
        return
    try:
        _batch_buffer.setdefault(table_full_name, []).append(row)
        _bump_metric("rows_enqueued")

        if len(_batch_buffer[table_full_name]) >= batch_size:
            rows_to_flush = _batch_buffer[table_full_name]
            _batch_buffer[table_full_name] = []

        # Teto global: se o escoamento parou, o buffer não pode crescer até
        # derrubar o pod por memória. As linhas mais antigas saem para a DLQ,
        # de onde são recuperáveis — nunca são simplesmente descartadas.
        total = sum(len(rows) for rows in _batch_buffer.values())
        if total > max_buffered:
            excedente = total - max_buffered
            for tabela in list(_batch_buffer):
                if excedente <= 0:
                    break
                linhas = _batch_buffer[tabela]
                if not linhas:
                    continue
                corte = min(excedente, len(linhas))
                evicted.append((tabela, linhas[:corte]))
                _batch_buffer[tabela] = linhas[corte:]
                excedente -= corte
    finally:
        _batch_buffer_lock.release()

    for tabela, linhas in evicted:
        _bump_metric("rows_evicted", len(linhas))
        logger.critical(
            f"Buffer de escrita do BigQuery estourou o teto de {max_buffered} linhas: "
            f"{len(linhas)} linha(s) de {tabela} foram desviadas para a DLQ."
        )
        _persist_to_dlq(tabela, linhas, "buffer de lote excedeu o teto configurado")

    if rows_to_flush:
        insert_rows_json_with_retry_and_dlq(table_full_name, rows_to_flush)


def flush_bigquery_batch_buffer(
    table_full_name: str = None,
    max_retries: int = 3,
    initial_delay: float = 0.5,
    timeout: float = None,
) -> None:
    """
    Flushes pending rows in the batch buffer to BigQuery.
    If table_full_name is specified, flushes only that table. Otherwise flushes all tables.

    `max_retries`/`initial_delay` existem para o flush de encerramento, que
    troca insistência por prazo — ver `_stop_batch_flush_thread`.
    """
    tables_to_flush = {}
    if not _batch_buffer_lock.acquire(timeout=_BUFFER_LOCK_TIMEOUT_SECONDS):
        logger.error(
            "Não foi possível adquirir o lock do buffer para descarregar em lote; "
            "as linhas seguem em memória até a próxima tentativa."
        )
        return
    try:
        if table_full_name:
            if _batch_buffer.get(table_full_name):
                tables_to_flush[table_full_name] = _batch_buffer[table_full_name]
                _batch_buffer[table_full_name] = []
        else:
            for tbl, rows in _batch_buffer.items():
                if rows:
                    tables_to_flush[tbl] = rows
            _batch_buffer.clear()
    finally:
        _batch_buffer_lock.release()

    if not tables_to_flush:
        return

    _bump_metric("flush_calls")
    for tbl, rows in tables_to_flush.items():
        try:
            insert_rows_json_with_retry_and_dlq(
                tbl,
                rows,
                max_retries=max_retries,
                initial_delay=initial_delay,
                timeout=timeout,
            )
        except Exception as e:
            logger.error(f"Erro ao descarregar buffer em lote para {tbl}: {e}")


def _gravar_linha(table_full_name: str, row: dict, use_batch: bool, span=None) -> None:
    """Encaminha a linha ao buffer ou direto ao BigQuery, marcando o span.

    A distinção importa no trace. Em modo `batched`, retornar sem erro
    significa "linha aceita no buffer", não "linha gravada no BigQuery" — a
    confirmação só existe no flush, que acontece depois e em outra thread. Sem
    `bigquery.durable`, o span afirmaria escrita bem-sucedida para uma linha
    que ainda pode terminar na DLQ, e a investigação de um registro faltante
    começaria pelo lugar errado.
    """
    if span is not None:
        span.set_attribute("bigquery.write_mode", "batched" if use_batch else "direct")
    if use_batch:
        enqueue_bigquery_row(table_full_name, row)
        if span is not None:
            span.set_attribute("bigquery.durable", False)
    else:
        insert_rows_json_with_retry_and_dlq(table_full_name, [row])
        if span is not None:
            span.set_attribute("bigquery.durable", True)


@interceptor(source={"source": "mcp", "tool": "bigquery"})
def save_response_in_bq(
    data: dict,
    endpoint: str,
    dataset_id: str,
    table_id: str,
    project_id: str = "rj-iplanrio",
    environment: str = None,
    use_batch: bool = False,
):
    from src.config.env import ENVIRONMENT

    env_value = environment if environment is not None else ENVIRONMENT
    table_full_name = f"{project_id}.{dataset_id}.{table_id}"
    logger.info(f"Salvando resposta no BigQuery: {table_full_name}")
    datetime_to_save = get_datetime()
    data_to_save = {
        "datetime": datetime_to_save,
        "endpoint": endpoint,
        "data": json.dumps(data, cls=CustomJSONEncoder),
        "environment": env_value,
        "data_particao": datetime_to_save.split("T")[0],
    }

    tracer = get_tracer()
    with tracer.start_as_current_span("bigquery.save_response") as span:
        span.set_attribute("bigquery.project_id", project_id)
        span.set_attribute("bigquery.dataset_id", dataset_id)
        span.set_attribute("bigquery.table_id", table_id)
        span.set_attribute("bigquery.endpoint", endpoint)
        span.set_attribute("bigquery.row_count", 1)
        try:
            _gravar_linha(table_full_name, data_to_save, use_batch, span)
            span.set_attribute("bigquery.success", True)
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            span.set_attribute("bigquery.success", False)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(f"Erro ao salvar resposta no BigQuery: {str(e)}")
            raise


async def save_response_in_bq_background(
    data, endpoint, dataset_id, table_id, environment=None
):
    """
    Asynchronous wrapper for saving the response in BigQuery using batching buffer.
    Catches and logs exceptions to prevent crashing background tasks.
    """
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _get_write_executor(),
            save_response_in_bq,
            data,
            endpoint,
            dataset_id,
            table_id,
            "rj-iplanrio",
            environment,
            True,  # use_batch=True
        )
    except Exception:
        logger.exception(
            f"Failed to save response to BigQuery in background for endpoint: {endpoint}"
        )


@interceptor(
    source={"source": "mcp", "tool": "bigquery"},
    extract_user_id=lambda args, kwargs: (
        kwargs.get("user_id") or (args[0] if args else "unknown")
    ),
)
def save_feedback_in_bq(
    user_id: str,
    feedback: str,
    timestamp: str,
    environment: str,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "feedback",
    project_id: str = "rj-iplanrio",
    use_batch: bool = False,
):
    table_full_name = f"{project_id}.{dataset_id}.{table_id}"
    logger.info(f"Salvando feedback no BigQuery: {table_full_name}")

    data_to_save = {
        "user_id": user_id,
        "feedback": feedback,
        "environment": environment,
        "timestamp": timestamp,
        "data_particao": timestamp.split("T")[0],
    }

    tracer = get_tracer()
    with tracer.start_as_current_span("bigquery.save_feedback") as span:
        span.set_attribute("bigquery.project_id", project_id)
        span.set_attribute("bigquery.dataset_id", dataset_id)
        span.set_attribute("bigquery.table_id", table_id)
        span.set_attribute("bigquery.row_count", 1)
        try:
            _gravar_linha(table_full_name, data_to_save, use_batch, span)
            logger.info(f"Feedback salvo no BigQuery: {table_full_name}")
            span.set_attribute("bigquery.success", True)
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            span.set_attribute("bigquery.success", False)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(f"Erro ao salvar feedback no BigQuery: {str(e)}")
            raise


async def save_feedback_in_bq_background(
    user_id: str,
    feedback: str,
    timestamp: str,
    environment: str,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "feedback",
):
    """
    Asynchronous wrapper for saving feedback in BigQuery using batching buffer.
    Catches and logs exceptions to prevent crashing background tasks.
    """
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _get_write_executor(),
            save_feedback_in_bq,
            user_id,
            feedback,
            timestamp,
            environment,
            dataset_id,
            table_id,
            "rj-iplanrio",
            True,  # use_batch=True
        )
    except Exception:
        logger.exception(
            f"Failed to save feedback to BigQuery in background for user: {user_id}"
        )


@interceptor(
    source={"source": "mcp", "tool": "bigquery"},
    extract_user_id=lambda args, kwargs: (
        kwargs.get("user_id") or (args[1] if len(args) > 1 else "unknown")
    ),
)
def save_cor_alert_in_bq(
    alert_id: str,
    user_id: str,
    alert_type: str,
    severity: str,
    description: str,
    address: str,
    latitude: float,
    longitude: float,
    timestamp: str,
    environment: str,
    bairro_raw: str = None,
    bairro_normalizado: str = None,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "cor_alerts",
    project_id: str = "rj-iplanrio",
    use_batch: bool = False,
):
    table_full_name = f"{project_id}.{dataset_id}.{table_id}"
    logger.info(f"Salvando alerta COR no BigQuery: {table_full_name}")

    data_to_save = {
        "alert_id": alert_id,
        "user_id": user_id,
        "alert_type": alert_type,
        "severity": severity,
        "description": description,
        "address": address,
        "latitude": latitude,
        "longitude": longitude,
        "bairro_raw": bairro_raw,
        "bairro_normalizado": bairro_normalizado,
        "created_at": timestamp,
        "environment": environment,
        "data_particao": timestamp.split("T")[0],
    }

    tracer = get_tracer()
    with tracer.start_as_current_span("bigquery.save_cor_alert") as span:
        span.set_attribute("bigquery.project_id", project_id)
        span.set_attribute("bigquery.dataset_id", dataset_id)
        span.set_attribute("bigquery.table_id", table_id)
        span.set_attribute("bigquery.alert_type", alert_type)
        span.set_attribute("bigquery.severity", severity)
        span.set_attribute("bigquery.row_count", 1)
        try:
            _gravar_linha(table_full_name, data_to_save, use_batch, span)
            logger.info(f"Alerta COR salvo no BigQuery: {table_full_name}")
            span.set_attribute("bigquery.success", True)
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            span.set_attribute("bigquery.success", False)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(f"Erro ao salvar alerta COR no BigQuery: {str(e)}")
            raise


# Severidades que não passam pelo buffer de agrupamento. Comparadas já
# normalizadas (sem acento, minúsculas), porque o valor chega da tool como
# texto livre e "Crítica", "critica" e "CRÍTICA" são o mesmo alerta.
_SEVERIDADES_SEM_LOTE = frozenset({"alta", "critica"})


async def save_cor_alert_in_bq_background(
    alert_id: str,
    user_id: str,
    alert_type: str,
    severity: str,
    description: str,
    address: str,
    latitude: float,
    longitude: float,
    timestamp: str,
    environment: str,
    bairro_raw: str = None,
    bairro_normalizado: str = None,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "cor_alerts",
):
    def _normalize(value: str) -> str:
        if not value:
            return ""
        import unicodedata

        normalized = unicodedata.normalize("NFKD", value.strip())
        without_accents = "".join(
            char for char in normalized if not unicodedata.combining(char)
        )
        return " ".join(without_accents.lower().split())

    def _infer_neighborhood_from_address(raw_address: str) -> str:
        if not raw_address:
            return ""
        address_norm = _normalize(raw_address)
        if "jardim america" in address_norm or "jd america" in address_norm:
            return "jardim america"
        if "acari" in address_norm:
            return "acari"
        if "guaratiba" in address_norm:
            return "guaratiba"
        return ""

    raw_candidate = (bairro_raw or "").strip()
    normalized_candidate = _normalize(bairro_normalizado or "")
    inferred_candidate = _infer_neighborhood_from_address(address)

    final_bairro_raw = raw_candidate or inferred_candidate
    final_bairro_normalizado = (
        normalized_candidate or _normalize(final_bairro_raw) or inferred_candidate
    )

    if final_bairro_normalizado == "jd america":
        final_bairro_normalizado = "jardim america"

    # Agrupar é a escolha certa para o volume, não para a emergência. Um alerta
    # de severidade alta/crítica pode esperar até `BIGQUERY_FLUSH_INTERVAL_SECONDS`
    # no buffer antes de existir em qualquer lugar consultável — e é justamente
    # o registro que alguém vai procurar durante a ocorrência. Estes vão direto;
    # baixa/média continuam agrupados, que é onde está o volume e, portanto, o
    # ganho de custo que o CHATR-118 persegue.
    #
    # O despacho ao COR não muda: `save_cor_alert_to_queue_background` já era
    # imediato. O que se fecha aqui é a tabela de registro, que ficava atrás.
    em_lote = _normalize(severity) not in _SEVERIDADES_SEM_LOTE

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _get_write_executor(),
            save_cor_alert_in_bq,
            alert_id,
            user_id,
            alert_type,
            severity,
            description,
            address,
            latitude,
            longitude,
            timestamp,
            environment,
            final_bairro_raw or None,
            final_bairro_normalizado or None,
            dataset_id,
            table_id,
            "rj-iplanrio",
            em_lote,
        )
    except Exception:
        logger.exception(
            f"Failed to save COR alert to BigQuery in background for alert_id: {alert_id}"
        )


def save_cor_alert_to_queue(
    alert_id: str,
    user_id: str,
    alert_type: str,
    severity: str,
    description: str,
    address: str,
    latitude: float,
    longitude: float,
    timestamp: str,
    environment: str,
    bairro_raw: str = None,
    bairro_normalizado: str = None,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "cor_alerts_queue",
    project_id: str = "rj-iplanrio",
    use_batch: bool = False,
):
    table_full_name = f"{project_id}.{dataset_id}.{table_id}"
    logger.info(f"Salvando alerta COR na fila: {table_full_name}")

    data_to_save = {
        "alert_id": alert_id,
        "user_id": user_id,
        "alert_type": alert_type,
        "severity": severity,
        "description": description,
        "address": address,
        "latitude": latitude,
        "longitude": longitude,
        "bairro_raw": bairro_raw,
        "bairro_normalizado": bairro_normalizado,
        "created_at": timestamp,
        "status": "pending",
        "aggregation_group_id": None,
        "sent_at": None,
        "environment": environment,
        "data_particao": timestamp.split("T")[0],
    }

    try:
        _gravar_linha(table_full_name, data_to_save, use_batch)
        logger.info(f"Alerta COR salvo na fila: {alert_id}")
    except Exception as e:
        logger.error(f"Erro ao salvar alerta COR na fila: {str(e)}")
        raise


async def save_cor_alert_to_queue_background(
    alert_id: str,
    user_id: str,
    alert_type: str,
    severity: str,
    description: str,
    address: str,
    latitude: float,
    longitude: float,
    timestamp: str,
    environment: str,
    bairro_raw: str = None,
    bairro_normalizado: str = None,
    dataset_id: str = "brutos_eai_logs",
    table_id: str = "cor_alerts_queue",
):
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _get_write_executor(),
            save_cor_alert_to_queue,
            alert_id,
            user_id,
            alert_type,
            severity,
            description,
            address,
            latitude,
            longitude,
            timestamp,
            environment,
            bairro_raw,
            bairro_normalizado,
            dataset_id,
            table_id,
            "rj-iplanrio",
            False,  # use_batch=False — entrega imediata para o pipeline Prefect
        )
    except Exception:
        logger.exception(
            f"Failed to save COR alert to queue in background for alert_id: {alert_id}"
        )


_async_redis_client = None


async def get_async_redis_client():
    """Get or create process-wide async Redis client instance.

    Os timeouts de socket não são opcionais aqui. Sem eles, um Redis que aceita
    a conexão mas para de responder (partição de rede, não recusa) pendura o
    ``await`` do cache indefinidamente — e a leitura do cache acontece *fora* do
    ``asyncio.wait_for`` que protege a query, então o timeout do BigQuery não
    cobre esse caso. O cache existe para economizar tempo; sem timeout ele vira
    o caminho mais lento possível. Mesmo padrão já usado pelo backend de sessão
    em ``src/tools/multi_step_service/core/state.py``.
    """
    global _async_redis_client
    if _async_redis_client is None:
        try:
            import redis.asyncio as redis

            redis_url = getattr(env, "REDIS_URL", None)

            if redis_url:
                timeout = getattr(env, "REDIS_CACHE_TIMEOUT_SECONDS", 2.0)
                _async_redis_client = redis.Redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=timeout,
                    socket_timeout=timeout,
                )
        except Exception as e:
            logger.warning(f"Could not initialize async Redis client: {e}")
            _async_redis_client = None
    return _async_redis_client


CACHE_KEY_PREFIX = "bq_cache"


# Caracteres que a chave usa como estrutura. Precisam ser escapados dentro dos
# valores, senão um valor consegue se disfarçar de outro. `%` vem primeiro por
# ser o próprio caractere de escape — inverter a ordem tornaria a codificação
# ambígua (`a%20b` literal viraria indistinguível de `a b`).
_ESCAPES_DA_CHAVE = (("%", "%25"), (":", "%3A"), (",", "%2C"), (" ", "%20"))


def _normalize_cache_part(value) -> str:
    """Reduz um valor de chave a texto estável, escapando os separadores.

    Coleções são ordenadas: o chamador só deve passar coleção quando a ordem
    não altera o resultado da query (é o caso de um filtro ``IN UNNEST``, onde
    ``["A","B"]`` e ``["B","A"]`` devolvem o mesmo). Ordenar transforma essas
    duas chamadas em um único acerto de cache em vez de duas entradas.

    O escape é injetivo de propósito. Substituir os separadores por ``_``,
    como antes, fazia valores distintos colapsarem na mesma chave sem qualquer
    sinal: ``"ASSISTENCIA SOCIAL"`` e ``"ASSISTENCIA_SOCIAL"`` viravam a mesma
    entrada, e ``["A,B"]`` virava a mesma que ``["A","B"]``. O desfecho é o
    pior possível para depurar — resposta errada, sem erro e sem log.

    Percent-encoding em vez de hash porque a chave precisa continuar legível e
    varrível por prefixo, que é a alavanca de invalidação por região. `%` não
    tem significado em glob de ``SCAN``, então os padrões não mudam.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(sorted(_normalize_cache_part(v) for v in value))
    texto = str(value)
    for bruto, escapado in _ESCAPES_DA_CHAVE:
        texto = texto.replace(bruto, escapado)
    return texto


def _sql_fingerprint(query: str) -> str:
    """Hash curto do texto da query, usado como parte fixa da chave semântica.

    Sem ele, a chave é definida só pelo namespace mais os parâmetros
    semânticos — e nada garante que esse par identifique *uma* query. Dois
    problemas concretos, ambos silenciosos:

    * Um segundo chamador que reaproveite o mesmo namespace (o caso de
      `equipments_categories`, que hoje não passa nenhuma parte e portanto tem
      a chave inteira igual ao namespace) passa a dividir uma entrada com o
      primeiro: quem gravar antes decide o que o outro lê, por todo o TTL.
    * Um deploy que altere o SQL continua servindo o formato antigo até o TTL
      expirar, porque a chave não se mexeu.

    Com o fingerprint, cada texto de query tem seu próprio espaço e um deploy
    que muda a query invalida sozinho o que mudou. A varredura por prefixo
    continua funcionando — `SCAN MATCH bq_cache:equipments:*` pega as duas
    gerações —, então a invalidação manual descrita em `_generate_cache_key`
    não é afetada.

    8 hexes (32 bits) bastam: o espaço é o punhado de queries do repositório,
    não entrada adversarial.
    """
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:8]


def _generate_cache_key(
    query: str,
    query_parameters: list = None,
    cache_namespace: str = None,
    cache_key_parts: dict = None,
) -> str:
    """Monta a chave do cache.

    Com ``cache_namespace``, a chave é semântica e legível — o que permite
    varrer e invalidar por prefixo::

        bq_cache:equipments:cats=EDUCACAO,SAUDE:plus8=849VVQCH

        SCAN MATCH bq_cache:equipments:*plus8=849VVQCH*   -> uma região
        SCAN MATCH bq_cache:equipments:*                  -> a tool inteira

    A chave *não* carrega versão do SQL (decisão registrada no CHATR-115): um
    deploy que altere o texto da query continua servindo o resultado anterior
    até o TTL expirar. Quando isso não for aceitável, o caminho é apagar o
    namespace com o ``SCAN``/``DEL`` acima como passo do deploy.

    Sem ``cache_namespace`` cai no hash de SQL + parâmetros, que mantém os
    chamadores genéricos funcionando (e nunca colide entre queries distintas).
    """
    if cache_namespace:
        partes = [
            f"{nome}={_normalize_cache_part(valor)}"
            for nome, valor in sorted((cache_key_parts or {}).items())
        ]
        partes.append(f"sql={_sql_fingerprint(query)}")
        return ":".join([CACHE_KEY_PREFIX, cache_namespace, *partes])

    params_str = ""
    if query_parameters:
        parts = []
        for p in query_parameters:
            name = getattr(p, "name", "")
            type_ = getattr(p, "type_", getattr(p, "array_type", ""))
            val = getattr(p, "value", getattr(p, "values", str(p)))
            parts.append(f"{name}:{type_}:{val}")
        params_str = "|".join(parts)
    raw_key = f"{query}:{params_str}"
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return f"{CACHE_KEY_PREFIX}:{key_hash}"


# ---------------------------------------------------------------------------
# Single-flight: uma query por chave, por processo.
#
# Sem isso, quando o TTL de uma região popular expira todas as requisições
# concorrentes daquele instante vão juntas ao BigQuery — exatamente o custo que
# o cache deveria estar cortando. O lock é por processo (não distribuído): com
# poucas réplicas o teto de queries duplicadas passa a ser o número de pods, o
# que resolve o problema sem o round trip e os locks órfãos de um lock no Redis.
# ---------------------------------------------------------------------------

_inflight_locks: dict = {}
_inflight_refs: dict = {}


@contextlib.asynccontextmanager
async def _single_flight(key: str, deadline: float = None):
    """Serializa, dentro do processo, as chamadas que disputam a mesma chave.

    `deadline` é um instante de `loop.time()`, não uma duração: é o mesmo
    prazo que limita a query, compartilhado com ela. Sem esse limite a espera
    aqui não tinha teto nenhum — o `asyncio.wait_for` da query fica *dentro*
    do lock, então ele limitava a query e não a fila. Com uma query lenta na
    frente, a enésima chamada da mesma chave esperava n × timeout, e o
    critério de aceite do CHATR-125 ("não trava o tool call indefinidamente")
    não se sustentava justamente sob a concorrência que o cache existe para
    atender: todo mundo pedindo a mesma região ao mesmo tempo.
    """
    loop = asyncio.get_running_loop()
    lock = _inflight_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _inflight_locks[key] = lock
    _inflight_refs[key] = _inflight_refs.get(key, 0) + 1
    adquirido = False
    try:
        if deadline is None:
            await lock.acquire()
        else:
            restante = deadline - loop.time()
            if restante <= 0:
                raise BigQueryTimeoutError(
                    f"BigQuery single-flight wait timed out for key {key}"
                )
            try:
                # `asyncio.timeout` e não `wait_for`: o `wait_for` embrulha o
                # `acquire()` numa Task separada e cancela *essa* Task quando
                # o prazo estoura, então quem decide o que fazer com um lock
                # que acabou de ser entregue é código fora do nosso escopo.
                # Aqui o cancelamento chega direto neste corpo — ou o
                # `acquire()` retorna e a linha seguinte (sem nenhum `await`
                # no meio) marca `adquirido`, ou ele levanta e não seguramos
                # nada. As duas coisas não têm como divergir, que é o que
                # garante que o `finally` sempre libere o que foi adquirido.
                async with asyncio.timeout(restante):
                    await lock.acquire()
            except asyncio.TimeoutError:
                # `from None`: o TimeoutError do prazo não acrescenta nada ao
                # daqui, e encadear os dois só polui o traceback.
                raise BigQueryTimeoutError(
                    f"BigQuery single-flight wait timed out for key {key}"
                ) from None
        adquirido = True
        yield
    finally:
        if adquirido:
            lock.release()
        _inflight_refs[key] = _inflight_refs.get(key, 1) - 1
        if _inflight_refs[key] <= 0:
            _inflight_refs.pop(key, None)
            _inflight_locks.pop(key, None)


def _ttl_com_jitter(ttl: int) -> int:
    """Encurta o TTL em até 10%, de forma aleatória.

    Entradas gravadas na mesma janela expiram juntas e recriam o pico que o
    single-flight acabou de evitar — só que uma hora depois. O jitter é sempre
    para baixo para que o TTL configurado continue sendo um teto de quão velho
    um dado pode ser servido.
    """
    if ttl <= 1:
        return ttl
    return max(1, int(ttl * random.uniform(0.9, 1.0)))


def _normalize_bq_value(value):
    """Converte um valor do BigQuery para algo que sobrevive a ida e volta em JSON.

    A conversão é *recursiva* de propósito. Um resultado que passa pelo cache é
    serializado e desserializado; um que não passa volta como o objeto Python
    cru. Se a normalização parasse no primeiro nível, um ``TIMESTAMP`` dentro de
    um ``STRUCT`` voltaria como `datetime` em cache miss e como string ISO em
    cache hit — o mesmo tool call devolvendo tipos diferentes conforme o estado
    do cache, que é a classe de bug mais difícil de reproduzir. Normalizando
    aqui, hit e miss devolvem exatamente a mesma estrutura.

    `Decimal` (NUMERIC) vira float e `bytes` (BYTES) vira base64: nenhum dos dois
    é serializável em JSON, então sem isso a gravação no cache falharia em
    silêncio — e a resposta da própria tool também, já que ela também é
    serializada para JSON antes de chegar ao agente.
    """
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {k: _normalize_bq_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_bq_value(v) for v in value]
    return value


class _QueryOutcome(NamedTuple):
    """Resultado da query mais a informação de se ele pode ser cacheado.

    `[]` vindo de uma tabela ausente é indistinguível de `[]` vindo de uma query
    que legitimamente não achou nada — e cachear o primeiro por uma hora congela
    uma falha transitória de infraestrutura em resposta vazia para todo mundo.
    Este flag é o que separa os dois casos.
    """

    rows: List[dict]
    cacheable: bool


class BigQueryTimeoutError(TimeoutError):
    """Estouro de prazo numa leitura do BigQuery.

    Subclasse de `TimeoutError` para não mudar o contrato de quem já captura
    o tipo embutido — mas precisa mesmo ser subclasse, e não o `TimeoutError`
    puro, por causa de `asyncio.futures._convert_future_exc`::

        if exc_class is concurrent.futures.TimeoutError:
            return exceptions.TimeoutError(*exc.args)

    A comparação é de identidade de classe. Uma exceção levantada dentro do
    executor cujo tipo seja *exatamente* `TimeoutError` (que em 3.11+ é o
    mesmo objeto que `concurrent.futures.TimeoutError`) é descartada e
    reconstruída do zero na travessia thread → event loop, junto com
    `__cause__` e `__traceback__`. Ou seja: o `from e` do outro lado da
    fronteira simplesmente não sobrevive. Com uma subclasse, o `else` daquele
    trecho devolve a exceção original intacta.
    """


class BigQueryQueryError(Exception):
    """Falha inesperada ao executar uma leitura no BigQuery.

    Separa o desfecho "não sei o que aconteceu" dos dois que o chamador
    consegue tratar: `GoogleAPIError` (falha conhecida de infraestrutura,
    repassada com o tipo original) e `TimeoutError` (estouro de prazo, venha
    ele do relógio do `wait_for` ou do relógio do `.result()`).

    Antes os três chegavam como `Exception` crua, o que obrigava todo chamador
    a olhar a *mensagem* para decidir o que fazer — e fazia a mesma condição
    (query lenta) chegar ora como `TimeoutError`, ora como `Exception`,
    dependendo de qual dos dois relógios disparasse primeiro.
    """


# ---------------------------------------------------------------------------
# Executor dedicado às leituras.
#
# O `wait_for` libera o `await` no prazo, mas não mata a thread: ela segue
# ocupada até a query terminar sozinha. Com o executor default do loop, essas
# threads presas são as mesmas que `save_response_in_bq_background`,
# `save_feedback_in_bq_background` e `save_cor_alert_in_bq_background` usam —
# ou seja, um BigQuery lento na *leitura* acabava enfileirando a gravação de
# log e de alerta do COR, que não têm nada a ver com o problema.
#
# Pool próprio e limitado: leitura travada só atrapalha leitura. E o limite é
# desejável, não um efeito colateral — quando ele satura, a espera pela vaga
# corre dentro do mesmo deadline da chamada, então o excesso vira TimeoutError
# rápido em vez de fila invisível.
# ---------------------------------------------------------------------------

_read_executor: ThreadPoolExecutor = None
_read_executor_lock = threading.Lock()


def _get_read_executor() -> ThreadPoolExecutor:
    """Devolve (criando na primeira vez) o pool de threads das leituras."""
    global _read_executor
    if _read_executor is not None:
        return _read_executor
    with _read_executor_lock:
        if _read_executor is None:
            max_workers = int(getattr(env, "BIGQUERY_READ_MAX_WORKERS", 8))
            _read_executor = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="bq-read"
            )
    return _read_executor


def _shutdown_read_executor() -> None:
    """Encerra o pool no fim do processo, sem esperar query pendurada."""
    global _read_executor
    executor, _read_executor = _read_executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def _cancelar_job(query_job) -> None:
    """Pede ao BigQuery para cancelar um job cujo resultado ninguém vai mais ler.

    Estourado o prazo, o job continuaria varrendo bytes do lado do Google e
    sendo cobrado por isso — o cliente desistiu, o servidor não sabe disso. O
    cancelamento é best-effort de propósito: já estamos no caminho de erro e
    falhar aqui só substituiria o timeout (que é a informação útil) por outra
    exceção qualquer.
    """
    if query_job is None:
        return
    try:
        query_job.cancel()
    except Exception as e:
        logger.warning(f"Não foi possível cancelar o job do BigQuery: {e}")


def _execute_bigquery_query(
    query: str,
    query_parameters: list = None,
    page_size: int = None,
    timeout_seconds: float = None,
) -> _QueryOutcome:
    """Synchronous execution of BigQuery query, designed to run in thread executor."""
    default_page_size = getattr(env, "GOOGLE_BIGQUERY_PAGE_SIZE", 100)
    default_timeout = getattr(env, "BIGQUERY_TIMEOUT_SECONDS", 10.0)

    page_size = page_size if page_size is not None else default_page_size
    timeout_seconds = (
        timeout_seconds if timeout_seconds is not None else default_timeout
    )
    client = get_bigquery_client()

    tracer = get_tracer()
    query_job = None
    with tracer.start_as_current_span("bigquery.query") as span:
        span.set_attribute("bigquery.page_size", page_size)
        span.set_attribute("bigquery.query_length", len(query))
        try:
            logger.info(f"Executando query no BigQuery: {query[:100]}...")
            job_config = None
            if query_parameters:
                job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)

            # O `timeout` aqui é o da chamada HTTP que *cria* o job, e é
            # distinto do `.result()` logo abaixo, que espera o job terminar.
            # Sem ele, uma submissão pendurada segurava a thread para sempre:
            # o `wait_for` do chamador devolvia o controle no prazo, mas a
            # vaga no pool só voltava quando a API respondesse.
            if job_config is not None:
                query_job = client.query(
                    query, job_config=job_config, timeout=timeout_seconds
                )
            else:
                query_job = client.query(query, timeout=timeout_seconds)

            results = query_job.result(page_size=page_size, timeout=timeout_seconds)

            rows = [
                {key: _normalize_bq_value(value) for key, value in row.items()}
                for row in results
            ]

            logger.info(f"Query executada com sucesso. {len(rows)} linhas retornadas.")
            span.set_attribute("bigquery.row_count", len(rows))
            span.set_attribute("bigquery.success", True)
            span.set_status(Status(StatusCode.OK))
            return _QueryOutcome(rows, cacheable=True)
        except NotFound as e:
            span.set_attribute("bigquery.row_count", 0)
            span.set_attribute("bigquery.table_not_found", True)
            span.set_attribute("bigquery.success", True)
            span.set_status(Status(StatusCode.OK))
            logger.warning(f"Tabela não encontrada no BigQuery: {str(e)}")
            # Degradação: devolve vazio para não derrubar a tool, mas marca como
            # não-cacheável. Tabela ausente costuma ser transitória (recriação,
            # permissão, tabela externa fora do ar) e cachear esse vazio por uma
            # hora transformaria o incidente em resposta errada muito depois de
            # ele ter passado.
            return _QueryOutcome([], cacheable=False)
        except TimeoutError as e:
            # É o que `query_job.result(timeout=...)` levanta quando o job não
            # termina no prazo (em 3.11+ `concurrent.futures.TimeoutError` é o
            # `TimeoutError` embutido). Precisa vir antes do `except Exception`
            # abaixo: sem este bloco, o estouro de prazo *interno* virava
            # `BigQueryQueryError` enquanto o estouro no `wait_for` do chamador
            # virava `TimeoutError` — a mesma condição com dois tipos, e qual
            # deles chegava dependia de qual relógio disparasse primeiro.
            span.set_attribute("bigquery.success", False)
            span.set_attribute("bigquery.timeout", True)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(
                f"Timeout de {timeout_seconds}s ao aguardar o job do BigQuery: {e}"
            )
            _cancelar_job(query_job)
            raise BigQueryTimeoutError(
                f"BigQuery query execution timed out after {timeout_seconds}s"
            ) from e
        except GoogleAPIError as e:
            span.set_attribute("bigquery.success", False)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(f"Erro ao executar query no BigQuery: {str(e)}")
            # Repassa a exceção original em vez de envolvê-la: é o tipo que
            # permite ao chamador separar falha conhecida de infraestrutura
            # (400 de tabela externa, 403 do Drive) de um bug nosso. Envolver
            # em `Exception` apagaria essa distinção.
            raise
        except Exception as e:
            span.set_attribute("bigquery.success", False)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.error(f"Erro ao executar query no BigQuery: {str(e)}")
            # `from e` preserva a causa: `BigQueryQueryError` diz "não era
            # falha conhecida nem prazo", e o traceback original continua
            # disponível para descobrir o que de fato era.
            raise BigQueryQueryError(
                f"Failed to execute BigQuery query: {str(e)}"
            ) from e


_CACHE_MISS = object()
# Distinto de `_CACHE_MISS`: "não tinha" e "não deu para perguntar" levam a
# decisões diferentes na hora de gravar.
_CACHE_UNAVAILABLE = object()


async def _cache_read(cache_key: str, span):
    """Lê do cache. Devolve `_CACHE_MISS` quando não há valor utilizável.

    Qualquer falha do Redis é degradação, não erro: o BigQuery continua sendo a
    fonte da verdade. O que muda em relação a só logar um warning é o atributo
    no span — sem ele, um cache 100% inoperante é indistinguível de um cache
    que só não teve acertos.
    """
    try:
        redis_client = await get_async_redis_client()
        if redis_client is None:
            span.set_attribute("cache.available", False)
            return _CACHE_MISS
        cached_data = await redis_client.get(cache_key)
        if cached_data is None:
            return _CACHE_MISS
        try:
            if isinstance(cached_data, bytes):
                cached_data = cached_data.decode("utf-8")
            return json.loads(cached_data)
        except ValueError as e:
            # `ValueError` cobre os dois modos de corrupção: `JSONDecodeError`
            # (texto que não é JSON) e `UnicodeDecodeError` (bytes que não são
            # UTF-8) são ambos subclasses dele.
            # Valor ilegível é MISS, não indisponibilidade. A diferença importa
            # porque `_CACHE_UNAVAILABLE` faz o chamador pular a gravação: a
            # chave envenenada sobreviveria até o TTL e *toda* requisição pagaria
            # uma query nesse intervalo. Como MISS, a query roda uma vez e
            # sobrescreve o lixo. O Redis aqui está saudável — ele respondeu.
            span.set_attribute("cache.corrupt_value", type(e).__name__)
            logger.warning(
                f"Valor inválido no cache do BigQuery (chave {cache_key}), "
                f"tratando como miss e sobrescrevendo: {e}"
            )
            return _CACHE_MISS
    except Exception as e:
        # Atributo próprio para leitura: quando o Redis está fora, a escrita
        # logo em seguida também falha, e um atributo único faria a segunda
        # apagar a primeira — escondendo justamente onde a degradação começou.
        span.set_attribute("cache.read_error", type(e).__name__)
        logger.warning(f"Erro ao ler cache do Redis para BigQuery: {e}")
        return _CACHE_UNAVAILABLE


async def _cache_write(
    cache_key: str, rows: List[dict], ttl: int, span, restante: float = None
) -> None:
    """Grava no cache, com jitter no TTL. Falha aqui nunca derruba a query.

    `restante` é o que sobrou do orçamento da chamada. Sem esse limite a
    gravação corria *fora* do prazo que `get_bigquery_result` promete: ela
    acontece depois de a query já ter respondido, então com o Redis mudo o
    tool call somava o timeout do BigQuery mais o timeout de socket do Redis
    (medido: +4,0s sobre um orçamento de 5s, porque o cliente ainda tenta
    reconectar). Com o limite, o teto da chamada volta a ser o timeout
    configurado, que era o critério do CHATR-125.

    Estourar o prazo aqui não é erro e não vira exceção: as linhas já estão na
    mão do chamador e serão devolvidas de qualquer jeito. O único efeito é a
    próxima requisição pagar outro miss. Cancelar no meio é seguro — o cliente
    do Redis derruba a conexão em `BaseException` justamente para não devolver
    ao pool uma conexão com resposta pendente.
    """
    if restante is not None and restante <= 0:
        span.set_attribute("cache.write_skipped", "budget_exhausted")
        return

    async def _gravar() -> None:
        redis_client = await get_async_redis_client()
        if redis_client is None:
            return
        serialized = json.dumps(rows, cls=CustomJSONEncoder)
        ttl_efetivo = _ttl_com_jitter(ttl)
        await redis_client.setex(cache_key, ttl_efetivo, serialized)
        span.set_attribute("cache.written", True)
        span.set_attribute("cache.ttl_seconds", ttl_efetivo)

    try:
        # `timeout=None` é "sem limite", então o caminho sem orçamento continua
        # se comportando como antes.
        await asyncio.wait_for(_gravar(), timeout=restante)
    except asyncio.TimeoutError:
        # Precisa vir antes do `except Exception`: `asyncio.TimeoutError` é
        # subclasse de `Exception` e cairia no ramo de erro do Redis, que
        # descreve outra coisa — o Redis pode estar perfeitamente saudável e
        # só não ter sobrado prazo.
        span.set_attribute("cache.write_timeout", True)
        logger.warning(
            f"Orçamento esgotado antes de gravar o cache do BigQuery "
            f"(chave {cache_key}); resultado devolvido sem cachear."
        )
    except Exception as e:
        span.set_attribute("cache.write_error", type(e).__name__)
        logger.warning(f"Erro ao gravar cache no Redis para BigQuery: {e}")


async def _run_query_com_timeout(
    query: str,
    query_parameters: list,
    page_size: int,
    timeout: float,
    orcamento: float = None,
) -> _QueryOutcome:
    """Roda a query no executor de leitura, limitada a `timeout` segundos.

    São dois números porque são duas coisas distintas:

    * `timeout` é o que *sobrou* do prazo — o cache e a fila do single-flight
      já podem ter consumido parte dele. É ele que limita a execução.
    * `orcamento` é o prazo configurado para a chamada inteira, e serve só
      para as mensagens. Quem lê um log ou trata a exceção quer saber que
      estourou o limite de 10s, não que restavam 9.87431s quando a query
      começou.

    O `except` aqui pega os dois relógios — o do `wait_for` e o do
    `.result()`, que chega pela thread — e os converte no mesmo `TimeoutError`
    com a mesma mensagem. Era essa a inconsistência do CHATR-125: a mesma
    condição chegava ao chamador com tipo diferente conforme quem disparasse
    primeiro, e como os dois usam o mesmo prazo, isso era decidido por corrida.
    """
    orcamento = timeout if orcamento is None else orcamento
    if timeout <= 0:
        # O prazo acabou antes de a query começar — normalmente esperando na
        # fila do single-flight. Levantar aqui evita mandar ao BigQuery um
        # trabalho cujo resultado ninguém mais vai esperar.
        raise BigQueryTimeoutError(
            f"BigQuery query execution timed out after {orcamento}s"
        )

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                _get_read_executor(),
                _execute_bigquery_query,
                query,
                query_parameters,
                page_size,
                timeout,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        logger.error(f"Timeout de {orcamento}s excedido ao executar query BigQuery")
        # `from e` preserva a cadeia: quando quem estourou foi o `.result()`,
        # `e` já carrega a exceção original do cliente do BigQuery.
        raise BigQueryTimeoutError(
            f"BigQuery query execution timed out after {orcamento}s"
        ) from e


@interceptor(source={"source": "mcp", "tool": "bigquery"})
async def get_bigquery_result(
    query: str,
    query_parameters: list = None,
    page_size: int = None,
    cache_ttl_seconds: int = None,
    timeout_seconds: float = None,
    cache_namespace: str = None,
    cache_key_parts: dict = None,
) -> List[dict]:
    """
    Executes a BigQuery query with caching and non-blocking executor execution.

    Args:
        query: SQL query to execute
        query_parameters: List of BigQuery QueryParameter objects
        page_size: Number of rows per page (optional, uses env default)
        cache_ttl_seconds: Cache TTL in seconds (0 to bypass cache)
        timeout_seconds: Query execution timeout in seconds
        cache_namespace: Prefixo semântico da chave de cache (ex.: "equipments").
            Sem ele a chave cai no hash do SQL — ver `_generate_cache_key`.
        cache_key_parts: Parâmetros semânticos que identificam a consulta
            (ex.: `{"plus8": ..., "cats": [...]}`).

    Returns:
        List of dictionaries with query results
    """
    default_ttl = getattr(env, "BIGQUERY_CACHE_TTL_SECONDS", 3600)
    default_timeout = getattr(env, "BIGQUERY_TIMEOUT_SECONDS", 10.0)

    ttl = cache_ttl_seconds if cache_ttl_seconds is not None else default_ttl
    timeout = timeout_seconds if timeout_seconds is not None else default_timeout

    # Um único orçamento para a chamada inteira: leitura de cache, espera na
    # fila do single-flight e execução da query saem todos daqui. É o que dá
    # ao tool call um teto de latência igual ao timeout configurado,
    # independente de quantas requisições disputem a mesma chave (CHATR-125).
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    tracer = get_tracer()
    with tracer.start_as_current_span("bigquery.read") as span:
        span.set_attribute("cache.enabled", ttl > 0)
        span.set_attribute("bigquery.timeout_budget_seconds", timeout)

        if ttl <= 0:
            span.set_attribute("cache.hit", False)
            outcome = await _run_query_com_timeout(
                query, query_parameters, page_size, deadline - loop.time(), timeout
            )
            return outcome.rows

        cache_key = _generate_cache_key(
            query, query_parameters, cache_namespace, cache_key_parts
        )
        span.set_attribute("cache.key", cache_key)

        cached = await _cache_read(cache_key, span)
        if cached is not _CACHE_MISS and cached is not _CACHE_UNAVAILABLE:
            span.set_attribute("cache.hit", True)
            logger.info(f"BigQuery cache hit para chave: {cache_key}")
            return cached

        # Redis que acabou de falhar na leitura vai falhar na escrita também, e
        # cada tentativa custa o timeout de socket inteiro. Pular a gravação
        # corta o pior caso pela metade — de outro modo toda requisição paga
        # dois timeouts enquanto o Redis estiver mudo.
        redis_indisponivel = cached is _CACHE_UNAVAILABLE

        # Só uma execução por chave dentro deste processo. Quem chegar junto
        # espera e é atendido pela leitura de cache logo abaixo, em vez de
        # disparar a mesma query em paralelo.
        async with _single_flight(cache_key, deadline):
            if not redis_indisponivel:
                cached = await _cache_read(cache_key, span)
                if cached is not _CACHE_MISS and cached is not _CACHE_UNAVAILABLE:
                    span.set_attribute("cache.hit", True)
                    span.set_attribute("cache.coalesced", True)
                    return cached

            span.set_attribute("cache.hit", False)
            outcome = await _run_query_com_timeout(
                query, query_parameters, page_size, deadline - loop.time(), timeout
            )

            if redis_indisponivel:
                span.set_attribute("cache.write_skipped", "redis_unavailable")
            elif not outcome.cacheable:
                span.set_attribute("cache.write_skipped", "degraded_result")
            else:
                # A gravação sai do mesmo orçamento da chamada: é o que mantém
                # o teto de latência do tool call igual ao timeout configurado.
                await _cache_write(
                    cache_key, outcome.rows, ttl, span, deadline - loop.time()
                )

            return outcome.rows


# ---------------------------------------------------------------------------
# Reprocessamento da DLQ (CHATR-126).
#
# Persistir o payload que falhou só resolve metade do problema: sem um caminho
# de volta, o dado sai de "perdido em silêncio" para "parado numa lista que
# ninguém lê". Aqui estão as duas metades que faltavam — um worker que devolve
# a DLQ ao BigQuery sozinho quando ele volta, e as funções que o CLI de
# operação (`python -m src.utils.bq_dlq_replay`) usa para o caminho manual.
#
# Contrato de entrega: o item só sai da DLQ *depois* de o BigQuery confirmar a
# escrita. Uma queda entre a confirmação e a remoção reprocessa o item na
# próxima varredura, ou seja, duplica. É a troca deliberada — para log,
# feedback e alerta, registro duplicado é um incômodo de análise; registro
# perdido é o defeito que esta história inteira existe para eliminar.
# ---------------------------------------------------------------------------

_DLQ_DRAIN_LOCK_KEY = "bq_dlq_drain:lock"
_DLQ_DRAIN_LOCK_TTL_SECONDS = 120


@contextlib.contextmanager
def _dlq_drain_lock(r):
    """Serializa o drain entre réplicas — dois drenos concorrentes duplicariam.

    Best-effort de propósito: se o Redis não responder ao `SET NX`, o drain
    apenas não roda nesta varredura. Bloquear o worker por causa do lock
    converteria indisponibilidade do Redis em DLQ que nunca escoa.
    """
    token = uuid.uuid4().hex
    adquirido = False
    try:
        adquirido = bool(
            r.set(_DLQ_DRAIN_LOCK_KEY, token, nx=True, ex=_DLQ_DRAIN_LOCK_TTL_SECONDS)
        )
    except Exception as e:
        logger.warning(f"Não foi possível adquirir o lock de drain da DLQ: {e}")
    try:
        yield adquirido
    finally:
        if adquirido:
            # Só libera se o token ainda for nosso: se o TTL expirou e outro
            # processo pegou o lock, apagá-lo aqui soltaria o dele.
            with contextlib.suppress(Exception):
                if r.get(_DLQ_DRAIN_LOCK_KEY) == token:
                    r.delete(_DLQ_DRAIN_LOCK_KEY)


def _dlq_redis_keys(r, table_full_name: str = None) -> List[str]:
    """Chaves de DLQ existentes no Redis, opcionalmente filtradas por tabela."""
    if table_full_name:
        return [_dlq_key(table_full_name)]
    try:
        return sorted(r.scan_iter(match=f"{DLQ_KEY_PREFIX}:*", count=100))
    except Exception as e:
        logger.warning(f"Não foi possível listar as chaves da DLQ: {e}")
        return []


def _marcar_motivo_do_poison(bruto: str, motivo: str) -> str:
    """Anota no item por que ele foi recusado em definitivo.

    O campo `error` do item da DLQ guarda a falha que o levou *para a DLQ*, que
    não é necessariamente a que o tornou irrecuperável: um item pode entrar por
    indisponibilidade do BigQuery e só depois, numa varredura do drain, ser
    recusado por schema. Sem esta anotação, a listagem do poison mostraria ao
    operador a mensagem errada — e a D15 promete exatamente o contrário, que o
    erro exibido nomeie o campo recusado.

    O `error` original é preservado: os dois juntos contam a história completa
    do item. Entrada ilegível volta como está, porque não há onde anotar.
    """
    if not motivo:
        return bruto
    try:
        item = json.loads(bruto)
        if not isinstance(item, dict):
            return bruto
    except (ValueError, TypeError):
        return bruto
    item["poison_error"] = motivo
    item["poison_at"] = get_datetime()
    try:
        return json.dumps(item, cls=CustomJSONEncoder)
    except (TypeError, ValueError):
        return bruto


def _mover_para_poison(
    r, chave: str, bruto: str, table_full_name: str, motivo: str = None
) -> bool:
    """Tira da fila principal um item que nunca vai ser aceito.

    Sem isto, um único payload malformado na cabeça da lista bloquearia para
    sempre tudo o que chegou depois dele — a DLQ inteira ficaria refém de um
    registro só. O item não é descartado: vai para uma chave separada, com o
    mesmo teto e a mesma validade, para inspeção manual.

    Devolve `False` se não conseguiu mover. Quem chama precisa saber: insistir
    sobre um item que não sai da cabeça da lista é laço infinito.
    """
    chave_poison = _dlq_poison_key(table_full_name)
    max_items = int(getattr(env, "BIGQUERY_DLQ_MAX_ITEMS", 1000))
    ttl = int(getattr(env, "BIGQUERY_DLQ_TTL_SECONDS", 604800))
    try:
        pipe = r.pipeline()
        pipe.rpush(chave_poison, _marcar_motivo_do_poison(bruto, motivo))
        pipe.ltrim(chave_poison, -max_items, -1)
        if ttl > 0:
            pipe.expire(chave_poison, ttl)
        # O LPOP vai na mesma pipeline que o RPUSH: numa falha parcial o item
        # fica duplicado (nas duas chaves), nunca ausente das duas.
        pipe.lpop(chave)
        resultado = pipe.execute()
    except Exception as e:
        logger.error(f"Não foi possível mover item para poison ({chave_poison}): {e}")
        return False
    # O poison tem o mesmo teto da DLQ e, portanto, o mesmo risco: mover um item
    # para cá pode expulsar o mais antigo da outra ponta.
    _alertar_descarte_por_teto(resultado, chave_poison, max_items)
    logger.critical(
        f"Item da DLQ de {table_full_name} recusado definitivamente pelo BigQuery "
        f"(schema/conteúdo). Movido para {chave_poison} — exige correção manual."
    )
    return True


def _replay_dlq_redis(
    limite: int, table_full_name: str = None, dry_run: bool = False
) -> dict:
    """Devolve itens da DLQ do Redis ao BigQuery. Bloqueante — use no executor."""
    resumo = {"itens": 0, "linhas": 0, "poison": 0, "pendentes": 0, "erros": []}
    r = _get_sync_redis_client()
    if r is None:
        return resumo

    with _dlq_drain_lock(r) as adquirido:
        if not adquirido:
            logger.info("Drain da DLQ já em andamento em outra réplica; pulando.")
            return resumo

        for chave in _dlq_redis_keys(r, table_full_name):
            if chave == _DLQ_DRAIN_LOCK_KEY:
                continue
            # O laço é limitado por *iterações*, não por itens devolvidos com
            # sucesso. A diferença importa: item que vai para poison ou que vem
            # sem payload não incrementa `itens`, então um contador só de
            # sucesso não faria o laço terminar. E como cada iteração relê a
            # cabeça da lista, qualquer remoção que falhe em silêncio deixaria
            # o mesmo item ali — laço infinito segurando uma thread do pool.
            processados = 0
            while processados < limite and resumo["itens"] < limite:
                processados += 1
                try:
                    brutos = r.lrange(chave, 0, 0)
                except Exception as e:
                    resumo["erros"].append(f"{chave}: {e}")
                    break
                if not brutos:
                    break

                bruto = brutos[0]
                try:
                    item = json.loads(bruto)
                except (ValueError, TypeError):
                    # Entrada ilegível não tem como ser reprocessada, e mantê-la
                    # na cabeça travaria a fila. Vai para poison como as demais.
                    resumo["poison"] += 1
                    if dry_run:
                        break  # nada é consumido em dry-run; sair evita laço infinito
                    if not _mover_para_poison(
                        r,
                        chave,
                        bruto,
                        chave.split(":", 1)[-1],
                        motivo="entrada ilegível (não é JSON)",
                    ):
                        resumo["erros"].append(f"{chave}: falha ao mover para poison")
                        break
                    continue

                tabela = item.get("table_full_name") or chave.split(":", 1)[-1]
                payload = item.get("payload") or []

                if dry_run:
                    resumo["itens"] += 1
                    resumo["linhas"] += len(payload)
                    break  # sem consumir: só reporta a cabeça da fila

                if not payload:
                    try:
                        r.lpop(chave)
                    except Exception as e:
                        resumo["erros"].append(f"{chave}: {e}")
                        break
                    continue

                try:
                    _insert_rows_json_raw(
                        tabela, payload, max_retries=2, initial_delay=0.5
                    )
                except Exception as e:
                    if _e_falha_permanente(e):
                        resumo["poison"] += 1
                        if not _mover_para_poison(
                            r, chave, bruto, tabela, motivo=str(e)
                        ):
                            resumo["erros"].append(
                                f"{tabela}: falha ao mover para poison"
                            )
                            break
                        continue
                    # Falha transitória: deixa tudo como está e tenta na
                    # próxima varredura, quando o BigQuery provavelmente voltou.
                    resumo["erros"].append(f"{tabela}: {e}")
                    break

                # A escrita já foi confirmada: se o LPOP falhar, o item volta na
                # próxima varredura e duplica. É a troca aceita — ver o
                # cabeçalho desta seção.
                try:
                    r.lpop(chave)
                except Exception as e:
                    resumo["erros"].append(f"{chave}: {e}")
                    break
                resumo["itens"] += 1
                resumo["linhas"] += len(payload)
                _bump_metric("rows_replayed", len(payload))

            with contextlib.suppress(Exception):
                resumo["pendentes"] += r.llen(chave)

    return resumo


def _mover_linha_para_poison(trabalho, linha: str, motivo: str) -> None:
    """Guarda numa chave à parte a linha que o BigQuery nunca vai aceitar.

    Espelha o `_mover_para_poison` do Redis. Antes desta função, o caminho em
    arquivo apenas logava e seguia — e, como o arquivo é reescrito ao fim da
    varredura sem as linhas puladas, o payload sumia. Era a perda silenciosa que
    o CHATR-126 existe para eliminar, sobrevivendo justamente no caminho de
    fallback, que roda quando o Redis (a proteção principal) está fora.

    O item não volta ao reprocessamento: `_replay_dlq_arquivos` ignora arquivos
    `.poison.` de propósito. Ele fica para inspeção e correção manual, com o
    mesmo teto e a mesma validade das demais filas.
    """
    # `dlq_<tabela>.jsonl` ou `dlq_<tabela>.jsonl.processing` -> `dlq_<tabela>.poison.jsonl`
    base = trabalho.name.removesuffix(_SUFIXO_EM_PROCESSAMENTO)
    destino = trabalho.with_name(base.replace(".jsonl", ".poison.jsonl", 1))

    try:
        # Mesma anotação do caminho no Redis: quem inspeciona precisa ver a
        # recusa definitiva, não a falha que apenas levou o item à DLQ.
        _anexar_com_teto(destino, _marcar_motivo_do_poison(linha, motivo), destino.stem)
    except OSError as e:
        # Sem destino para o item, o menos ruim é deixar o payload no log: é
        # recuperável por quem estiver lendo, e some do arquivo de qualquer jeito.
        logger.critical(
            f"Não foi possível mover item da DLQ em arquivo para poison "
            f"({destino.name}): {e}. Motivo original: {motivo}. Payload: {linha}"
        )
        return

    logger.critical(
        f"Item da DLQ em arquivo movido para {destino.name} — exige correção "
        f"manual. Motivo: {motivo}"
    )


def _replay_dlq_arquivos(
    limite: int, table_full_name: str = None, dry_run: bool = False
) -> dict:
    """Devolve ao BigQuery o que caiu no fallback em arquivo. Bloqueante."""
    resumo = {"itens": 0, "linhas": 0, "poison": 0, "pendentes": 0, "erros": []}

    if not dry_run:
        # Antes de reprocessar: o que passou da validade não deve ser tentado,
        # nem continuar em disco. Fica aqui porque esta é a única varredura
        # periódica que toca o diretório — o worker de drain a chama por ciclo.
        with contextlib.suppress(Exception):
            _expirar_arquivos_dlq()

    dlq_dir = _dlq_dir()
    if not dlq_dir.is_dir():
        return resumo

    alvo = (
        _sanitize_table_name(table_full_name).replace(".", "_")
        if table_full_name
        else None
    )

    # `.processing` primeiro: são sobras de uma execução anterior que morreu no
    # meio. Ficam à frente para não envelhecerem indefinidamente.
    arquivos = sorted(dlq_dir.glob(f"dlq_*.jsonl{_SUFIXO_EM_PROCESSAMENTO}")) + sorted(
        dlq_dir.glob("dlq_*.jsonl")
    )

    for arquivo in arquivos:
        # `dlq_*.jsonl` também casa com `dlq_<tabela>.poison.jsonl`. Reprocessar
        # o arquivo de poison seria um laço: são exatamente as linhas que o
        # BigQuery já recusou em definitivo, e cada passagem as devolveria a
        # ele para serem recusadas de novo.
        if ".poison." in arquivo.name:
            continue
        if alvo and f"dlq_{alvo}.jsonl" not in arquivo.name:
            continue

        if arquivo.suffix == _SUFIXO_EM_PROCESSAMENTO:
            trabalho = arquivo
        else:
            # Renomear antes de ler é o que evita perder linha: `_persist_to_dlq`
            # continua acrescentando ao nome original, que a partir daqui é um
            # arquivo novo. Sem isso, reescrever o arquivo no fim apagaria tudo
            # que tivesse sido acrescentado durante o reprocessamento.
            trabalho = arquivo.with_name(arquivo.name + _SUFIXO_EM_PROCESSAMENTO)
            if dry_run:
                trabalho = arquivo
            else:
                try:
                    arquivo.rename(trabalho)
                except OSError as e:
                    resumo["erros"].append(f"{arquivo.name}: {e}")
                    continue

        try:
            linhas = [
                linha
                for linha in trabalho.read_text(encoding="utf-8").splitlines()
                if linha.strip()
            ]
        except OSError as e:
            resumo["erros"].append(f"{trabalho.name}: {e}")
            continue

        if dry_run:
            resumo["itens"] += len(linhas)
            for linha in linhas:
                with contextlib.suppress(ValueError, TypeError):
                    resumo["linhas"] += len(json.loads(linha).get("payload") or [])
            continue

        restantes: List[str] = []
        for indice, linha in enumerate(linhas):
            if resumo["itens"] >= limite:
                restantes.extend(linhas[indice:])
                break
            try:
                item = json.loads(linha)
            except (ValueError, TypeError):
                resumo["poison"] += 1
                _mover_linha_para_poison(
                    trabalho, linha, f"linha ilegível em {trabalho.name}"
                )
                continue

            tabela = item.get("table_full_name")
            payload = item.get("payload") or []
            if not tabela or not payload:
                continue
            try:
                _insert_rows_json_raw(tabela, payload, max_retries=2, initial_delay=0.5)
            except Exception as e:
                if _e_falha_permanente(e):
                    resumo["poison"] += 1
                    _mover_linha_para_poison(
                        trabalho, linha, f"recusado definitivamente por {tabela}: {e}"
                    )
                    continue
                resumo["erros"].append(f"{tabela}: {e}")
                restantes.extend(linhas[indice:])
                break
            resumo["itens"] += 1
            resumo["linhas"] += len(payload)
            _bump_metric("rows_replayed", len(payload))

        try:
            if restantes:
                trabalho.write_text("\n".join(restantes) + "\n", encoding="utf-8")
                resumo["pendentes"] += len(restantes)
            else:
                trabalho.unlink(missing_ok=True)
        except OSError as e:
            resumo["erros"].append(f"{trabalho.name}: {e}")

    return resumo


def replay_bigquery_dlq(
    limite: int = None, table_full_name: str = None, dry_run: bool = False
) -> dict:
    """Reprocessa a DLQ (Redis e arquivo) devolvendo o que conseguiu ao BigQuery.

    Bloqueante: chame de thread (o worker usa o executor de escrita) ou de um
    processo de linha de comando, nunca direto da event loop.
    """
    if limite is None:
        limite = int(getattr(env, "BIGQUERY_DLQ_DRAIN_BATCH", 100))

    total = {"itens": 0, "linhas": 0, "poison": 0, "pendentes": 0, "erros": []}

    parcial_redis = _replay_dlq_redis(limite, table_full_name, dry_run)
    # O limite é o orçamento da varredura inteira, não de cada origem: o que o
    # Redis consumiu sai do que sobra para os arquivos.
    restante = limite if dry_run else max(limite - parcial_redis["itens"], 0)
    parcial_arquivos = _replay_dlq_arquivos(restante, table_full_name, dry_run)

    for parcial in (parcial_redis, parcial_arquivos):
        for chave in ("itens", "linhas", "poison", "pendentes"):
            total[chave] += parcial[chave]
        total["erros"].extend(parcial["erros"])
    return total


def get_dlq_depth() -> dict:
    """Quantos itens estão parados na DLQ, por origem. Bloqueante.

    É o que faltava para alguém *descobrir* que há dado parado: até aqui a DLQ
    só aparecia em linha de log no instante da falha, que ninguém revisita.
    """
    profundidade = {
        "redis": 0,
        "poison": 0,
        "arquivos": 0,
        "total": 0,
        # Quais tabelas e quanto tempo resta antes de o TTL apagar o poison.
        # Sem isso, a mensagem do health check dizia que havia item parado sem
        # dizer onde nem até quando — e o operador descobria o prazo só quando
        # ele já tinha vencido, porque a expiração no Redis não deixa rastro.
        "poison_tabelas": [],
        "poison_expira_em_s": None,
    }
    prazos = []

    r = _get_sync_redis_client()
    if r is not None:
        try:
            for chave in r.scan_iter(match=f"{DLQ_KEY_PREFIX}:*", count=100):
                if chave != _DLQ_DRAIN_LOCK_KEY:
                    profundidade["redis"] += r.llen(chave)
            for chave in r.scan_iter(match=f"{DLQ_POISON_KEY_PREFIX}:*", count=100):
                quantos = r.llen(chave)
                if not quantos:
                    continue
                profundidade["poison"] += quantos
                profundidade["poison_tabelas"].append(chave.split(":", 1)[-1])
                with contextlib.suppress(Exception):
                    restante = r.ttl(chave)
                    if isinstance(restante, int) and restante > 0:
                        prazos.append(restante)
        except Exception as e:
            logger.warning(f"Não foi possível medir a profundidade da DLQ: {e}")

    ttl_arquivo = int(getattr(env, "BIGQUERY_DLQ_TTL_SECONDS", 604800))
    with contextlib.suppress(Exception):
        dlq_dir = _dlq_dir()
        if dlq_dir.is_dir():
            for arquivo in dlq_dir.glob("dlq_*.jsonl*"):
                # O arquivo de poison entra no mesmo balde do poison do Redis:
                # é a distinção que `check_bigquery_dlq` usa para dizer "isto
                # escoa sozinho" ou "isto precisa de gente".
                e_poison = ".poison." in arquivo.name
                balde = "poison" if e_poison else "arquivos"
                with open(arquivo, "r", encoding="utf-8") as f:
                    quantos = sum(1 for linha in f if linha.strip())
                profundidade[balde] += quantos
                if e_poison and quantos:
                    profundidade["poison_tabelas"].append(arquivo.name)
                    if ttl_arquivo > 0:
                        restante = arquivo.stat().st_mtime + ttl_arquivo - _time.time()
                        if restante > 0:
                            prazos.append(int(restante))

    profundidade["poison_tabelas"] = sorted(set(profundidade["poison_tabelas"]))
    # O menor prazo é o que importa: é o primeiro dado a sumir.
    profundidade["poison_expira_em_s"] = min(prazos) if prazos else None
    profundidade["total"] = (
        profundidade["redis"] + profundidade["poison"] + profundidade["arquivos"]
    )
    return profundidade


def formatar_duracao(segundos) -> str:
    """Segundos em "6d3h" / "4h12m" / "45s", para mensagem de operação."""
    if segundos is None:
        return "sem prazo"
    segundos = int(segundos)
    if segundos <= 0:
        return "vencido"
    dias, resto = divmod(segundos, 86400)
    horas, resto = divmod(resto, 3600)
    minutos = resto // 60
    if dias:
        return f"{dias}d{horas}h"
    if horas:
        return f"{horas}h{minutos}m"
    if minutos:
        return f"{minutos}m"
    return f"{segundos}s"


async def get_dlq_depth_async() -> dict:
    """`get_dlq_depth` fora da event loop — o cliente Redis aqui é síncrono."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_get_write_executor(), get_dlq_depth)


# ---------------------------------------------------------------------------
# Operação sobre o poison.
#
# Até aqui o poison era write-only: `_mover_para_poison` e
# `_mover_linha_para_poison` escreviam, e nada lia de volta — nem o worker de
# drain, nem o CLI. A única saída era o TTL expirando, em silêncio no caso do
# Redis. É o mesmo defeito que a D3 corrigiu para a DLQ principal ("persistir o
# payload sem caminho de retorno só troca 'dado perdido' por 'dado parado numa
# lista que ninguém lê"), reproduzido um nível abaixo — e com o agravante de
# que o critério de aceite do CHATR-126, "payload fica disponível para
# reprocessamento manual ou automático", não era cumprido para esses itens.
#
# São três operações, todas manuais e explícitas, porque as três exigem um
# julgamento que nenhum worker pode fazer sozinho: ler para diagnosticar,
# devolver à fila depois de corrigir a causa, e descartar quando a conclusão
# for que o payload é irrecuperável.
# ---------------------------------------------------------------------------

_POISON_LIMITE_INSPECAO = 20


def _poison_redis_keys(r, table_full_name: str = None) -> List[str]:
    """Chaves de poison no Redis, opcionalmente filtradas por tabela."""
    if table_full_name:
        return [_dlq_poison_key(table_full_name)]
    try:
        return sorted(r.scan_iter(match=f"{DLQ_POISON_KEY_PREFIX}:*", count=100))
    except Exception as e:
        logger.warning(f"Não foi possível listar as chaves de poison: {e}")
        return []


def _poison_file_paths(table_full_name: str = None) -> list:
    """Arquivos de poison do fallback, opcionalmente filtrados por tabela."""
    dlq_dir = _dlq_dir()
    if not dlq_dir.is_dir():
        return []
    if table_full_name:
        alvo = _sanitize_table_name(table_full_name).replace(".", "_")
        caminho = dlq_dir / f"dlq_{alvo}.poison.jsonl"
        return [caminho] if caminho.is_file() else []
    return sorted(dlq_dir.glob("dlq_*.poison.jsonl"))


def _resumir_item_poison(bruto: str, origem: str, tabela_padrao: str) -> dict:
    """Descreve um item de poison sem expor o conteúdo do payload.

    O payload carrega dado pessoal — `user_id` é telefone, alerta do COR tem
    endereço e coordenada. Esta função é servida por um CLI cuja saída vai para
    o terminal do operador e, com frequência, para o scrollback ou para o log de
    um job. Despejar o payload ali por padrão espalharia o dado justamente para
    fora dos lugares onde ele é controlado (Redis com TTL, tabela do BigQuery).

    O que sai por padrão é o que basta para diagnosticar um erro de schema: a
    mensagem do BigQuery, que nomeia o campo recusado, e a lista de campos
    presentes. Nome de campo é estrutura, não dado da pessoa. O conteúdo só sai
    sob pedido explícito (`--mostrar-payload`).
    """
    resumo = {"origem": origem, "tabela": tabela_padrao, "linhas": 0, "campos": []}
    try:
        item = json.loads(bruto)
    except (ValueError, TypeError):
        resumo["erro"] = "entrada ilegível (não é JSON)"
        return resumo

    payload = item.get("payload") or []
    resumo["tabela"] = item.get("table_full_name") or tabela_padrao
    resumo["failed_at"] = item.get("failed_at")
    # `poison_error` primeiro: é a recusa definitiva, que nomeia o campo a
    # corrigir. `error` é a falha que levou o item à DLQ, e pode ser outra —
    # indisponibilidade transitória, por exemplo, que não diz nada sobre o que
    # fazer agora. Quando os dois coincidem, mostrar um só não perde nada.
    resumo["erro"] = item.get("poison_error") or item.get("error")
    if item.get("poison_error") and item.get("error") != item.get("poison_error"):
        resumo["erro_original"] = item.get("error")
    if item.get("poison_at"):
        resumo["poison_at"] = item.get("poison_at")
    resumo["linhas"] = len(payload)
    campos = set()
    for linha in payload:
        if isinstance(linha, dict):
            campos.update(linha)
    resumo["campos"] = sorted(campos)
    return resumo


def inspecionar_poison(
    limite: int = None, table_full_name: str = None, incluir_payload: bool = False
) -> dict:
    """Lê o poison sem consumir nada. Bloqueante — chame de thread ou de CLI.

    Não remove, não reprocessa e não altera TTL: é a operação que vem antes de
    decidir entre devolver à fila e descartar.
    """
    limite = int(limite if limite is not None else _POISON_LIMITE_INSPECAO)
    resultado = {"itens": [], "total": 0, "erros": []}

    r = _get_sync_redis_client()
    if r is not None:
        for chave in _poison_redis_keys(r, table_full_name):
            try:
                brutos = r.lrange(
                    chave, 0, max(limite - len(resultado["itens"]), 0) - 1
                )
            except Exception as e:
                resultado["erros"].append(f"{chave}: {e}")
                continue
            for bruto in brutos:
                resumo = _resumir_item_poison(bruto, "redis", chave.split(":", 1)[-1])
                resumo["chave"] = chave
                if incluir_payload:
                    with contextlib.suppress(ValueError, TypeError):
                        resumo["payload"] = json.loads(bruto).get("payload")
                resultado["itens"].append(resumo)
                if len(resultado["itens"]) >= limite:
                    break
            if len(resultado["itens"]) >= limite:
                break

    for arquivo in _poison_file_paths(table_full_name):
        if len(resultado["itens"]) >= limite:
            break
        try:
            linhas = [
                linha
                for linha in arquivo.read_text(encoding="utf-8").splitlines()
                if linha.strip()
            ]
        except OSError as e:
            resultado["erros"].append(f"{arquivo.name}: {e}")
            continue
        for linha in linhas:
            resumo = _resumir_item_poison(linha, "arquivo", arquivo.stem)
            resumo["chave"] = arquivo.name
            if incluir_payload:
                with contextlib.suppress(ValueError, TypeError):
                    resumo["payload"] = json.loads(linha).get("payload")
            resultado["itens"].append(resumo)
            if len(resultado["itens"]) >= limite:
                break

    resultado["total"] = len(resultado["itens"])
    return resultado


def reenfileirar_poison(limite: int = None, table_full_name: str = None) -> dict:
    """Devolve itens do poison para a DLQ normal. Bloqueante.

    Existe para o caminho em que a causa foi corrigida — schema ajustado, coluna
    criada — e o payload passa a ser aceitável. O item volta para `bq_dlq:` e o
    worker de drain o entrega na varredura seguinte, sem intervenção adicional.

    Não valida se a causa de fato foi corrigida, porque não tem como: se não
    foi, o drain recusa o item mais uma vez e ele volta ao poison. O laço é
    finito e visível (cada passagem loga), e o custo de errar é uma tentativa
    perdida — bem menor que o de não haver caminho de volta nenhum.
    """
    limite = int(
        limite
        if limite is not None
        else int(getattr(env, "BIGQUERY_DLQ_DRAIN_BATCH", 100))
    )
    resumo = {"itens": 0, "linhas": 0, "pendentes": 0, "descartados": 0, "erros": []}
    max_items = int(getattr(env, "BIGQUERY_DLQ_MAX_ITEMS", 1000))
    ttl = int(getattr(env, "BIGQUERY_DLQ_TTL_SECONDS", 604800))

    r = _get_sync_redis_client()
    if r is not None:
        for chave in _poison_redis_keys(r, table_full_name):
            # Limitado por iterações, e não por sucessos: um item cuja remoção
            # falha em silêncio ficaria na cabeça da lista e o laço nunca
            # terminaria, segurando uma thread do pool. Mesmo motivo do
            # `_replay_dlq_redis`.
            processados = 0
            while processados < limite and resumo["itens"] < limite:
                processados += 1
                try:
                    brutos = r.lrange(chave, 0, 0)
                except Exception as e:
                    resumo["erros"].append(f"{chave}: {e}")
                    break
                if not brutos:
                    break

                bruto = brutos[0]
                tabela = table_full_name
                linhas = 0
                try:
                    item = json.loads(bruto)
                    tabela = item.get("table_full_name") or tabela
                    linhas = len(item.get("payload") or [])
                except (ValueError, TypeError):
                    # Entrada ilegível não tem para onde voltar: reenfileirá-la
                    # só a devolveria ao poison na varredura seguinte. Fica onde
                    # está, para `--purge-poison` ou para o TTL.
                    resumo["erros"].append(f"{chave}: entrada ilegível, mantida")
                    break

                destino = (
                    _dlq_key(tabela)
                    if tabela
                    else chave.replace(DLQ_POISON_KEY_PREFIX, DLQ_KEY_PREFIX, 1)
                )
                try:
                    # RPUSH no destino e LPOP na origem na mesma pipeline, na
                    # mesma ordem do `_mover_para_poison`: numa falha parcial o
                    # item fica nas duas chaves, nunca ausente das duas.
                    pipe = r.pipeline()
                    pipe.rpush(destino, bruto)
                    pipe.ltrim(destino, -max_items, -1)
                    if ttl > 0:
                        pipe.expire(destino, ttl)
                    pipe.lpop(chave)
                    resultado = pipe.execute()
                except Exception as e:
                    resumo["erros"].append(f"{chave}: {e}")
                    break

                # Devolver ao destino pode estourar o teto dele: a DLQ normal
                # segue recebendo enquanto o operador reenfileira. O descarte
                # entra no resumo porque quem rodou o comando é justamente quem
                # precisa saber que a volta custou itens.
                resumo["descartados"] += _alertar_descarte_por_teto(
                    resultado, destino, max_items
                )
                resumo["itens"] += 1
                resumo["linhas"] += linhas

            with contextlib.suppress(Exception):
                resumo["pendentes"] += r.llen(chave)

    for arquivo in _poison_file_paths(table_full_name):
        if resumo["itens"] >= limite:
            with contextlib.suppress(OSError):
                resumo["pendentes"] += len(
                    [
                        linha
                        for linha in arquivo.read_text(encoding="utf-8").splitlines()
                        if linha.strip()
                    ]
                )
            continue
        # Mesma proteção do `_replay_dlq_arquivos`: renomear antes de ler separa
        # o que está sendo devolvido do que continua chegando, para o rewrite
        # final não apagar linha nova. `.poison.` continua no nome, então o
        # reprocessamento normal segue ignorando o arquivo.
        trabalho = arquivo.with_name(arquivo.name + _SUFIXO_EM_PROCESSAMENTO)
        try:
            arquivo.rename(trabalho)
        except OSError as e:
            resumo["erros"].append(f"{arquivo.name}: {e}")
            continue

        try:
            linhas = [
                linha
                for linha in trabalho.read_text(encoding="utf-8").splitlines()
                if linha.strip()
            ]
        except OSError as e:
            resumo["erros"].append(f"{trabalho.name}: {e}")
            continue

        restantes: List[str] = []
        for indice, linha in enumerate(linhas):
            if resumo["itens"] >= limite:
                restantes.extend(linhas[indice:])
                break
            try:
                item = json.loads(linha)
                tabela = item.get("table_full_name")
                qtd = len(item.get("payload") or [])
            except (ValueError, TypeError):
                restantes.append(linha)
                continue
            if not tabela:
                restantes.append(linha)
                continue
            try:
                resumo["descartados"] += _anexar_com_teto(
                    _dlq_file_path(tabela), linha, tabela
                )
            except OSError as e:
                resumo["erros"].append(f"{tabela}: {e}")
                restantes.extend(linhas[indice:])
                break
            resumo["itens"] += 1
            resumo["linhas"] += qtd

        try:
            if restantes:
                trabalho.write_text("\n".join(restantes) + "\n", encoding="utf-8")
                trabalho.rename(arquivo)
                resumo["pendentes"] += len(restantes)
            else:
                trabalho.unlink(missing_ok=True)
        except OSError as e:
            resumo["erros"].append(f"{trabalho.name}: {e}")

    if resumo["itens"]:
        logger.warning(
            f"{resumo['itens']} item(ns) devolvidos do poison para a DLQ "
            f"({resumo['linhas']} linha(s)). Se a causa não tiver sido corrigida, "
            f"o drain vai recusá-los de novo."
        )
    if resumo["descartados"]:
        logger.critical(
            f"O reenfileiramento estourou o teto da DLQ: {resumo['descartados']} "
            f"item(ns) mais antigo(s) foram DESCARTADOS definitivamente. Drene a "
            f"DLQ antes de devolver o resto do poison."
        )
    return resumo


def descartar_poison(table_full_name: str = None) -> dict:
    """Apaga o poison em definitivo. Bloqueante.

    Só existe para a conclusão de que o payload é irrecuperável. Diferente do
    TTL, que faz a mesma coisa em silêncio, aqui o descarte é decidido por
    alguém e fica registrado — daí o `logger.critical` com a contagem.
    """
    resumo = {"itens": 0, "chaves": 0, "erros": []}

    r = _get_sync_redis_client()
    if r is not None:
        for chave in _poison_redis_keys(r, table_full_name):
            try:
                quantos = r.llen(chave)
                if not quantos:
                    continue
                r.delete(chave)
            except Exception as e:
                resumo["erros"].append(f"{chave}: {e}")
                continue
            resumo["itens"] += quantos
            resumo["chaves"] += 1

    for arquivo in _poison_file_paths(table_full_name):
        try:
            quantos = len(
                [
                    linha
                    for linha in arquivo.read_text(encoding="utf-8").splitlines()
                    if linha.strip()
                ]
            )
            arquivo.unlink(missing_ok=True)
        except OSError as e:
            resumo["erros"].append(f"{arquivo.name}: {e}")
            continue
        resumo["itens"] += quantos
        resumo["chaves"] += 1

    if resumo["itens"]:
        logger.critical(
            f"{resumo['itens']} item(ns) do poison DESCARTADOS definitivamente por "
            f"operação manual ({resumo['chaves']} chave(s)/arquivo(s); "
            f"tabela: {table_full_name or 'todas'})."
        )
    return resumo


async def inspecionar_poison_async(
    limite: int = None, table_full_name: str = None, incluir_payload: bool = False
) -> dict:
    """`inspecionar_poison` fora da event loop — o cliente Redis é síncrono."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _get_write_executor(),
        functools.partial(
            inspecionar_poison,
            limite=limite,
            table_full_name=table_full_name,
            incluir_payload=incluir_payload,
        ),
    )


# Espera antes da primeira varredura do drain. Curta o bastante para não deixar
# a DLQ herdada de um pod anterior parada sem motivo, longa o bastante para o
# boot terminar antes. Ver `drain_bigquery_dlq_loop`.
_PRIMEIRA_VARREDURA_SEGUNDOS = 15.0


async def expirar_arquivos_dlq_async() -> None:
    """`_expirar_arquivos_dlq` fora da event loop — a varredura toca o disco.

    Existe para que a expiração não dependa do worker de drain. Ela rodava só
    dentro de `_replay_dlq_arquivos`, o que deixava dois casos sem nenhuma
    limpeza: `BIGQUERY_DLQ_DRAIN_ENABLED=false` e execução local. Nesses casos o
    arquivo de DLQ ficava indefinidamente — e o TTL não é só higiene de disco, é
    o que limita a retenção do dado pessoal que vai no payload (`user_id` é
    telefone; alerta do COR tem endereço e coordenada).

    Chamada no startup do lifespan. Nunca propaga exceção: limpeza de disco não
    pode ser motivo de o servidor não subir.
    """
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_get_write_executor(), _expirar_arquivos_dlq)
    except Exception:
        logger.exception("Falha ao expirar arquivos da DLQ no startup")


async def drain_bigquery_dlq_loop() -> None:
    """Worker que devolve a DLQ ao BigQuery periodicamente.

    Roda como task do lifespan. Todo o trabalho bloqueante (Redis síncrono,
    insert do BigQuery, leitura de arquivo) vai para o pool de escrita: a event
    loop nunca fica presa numa varredura, mesmo quando a DLQ está cheia.

    Nunca deixa uma exceção escapar. Este laço é a única coisa que devolve dado
    à sua tabela de destino; um erro não tratado o mataria em silêncio e a DLQ
    voltaria a ser um depósito sem saída.
    """
    intervalo = float(getattr(env, "BIGQUERY_DLQ_DRAIN_INTERVAL_SECONDS", 300.0))
    loop = asyncio.get_running_loop()

    # A primeira varredura não espera o intervalo cheio. O cenário que a torna
    # urgente é o restart: a DLQ do Redis sobrevive ao pod, então o que ficou
    # parado do processo anterior já está lá quando este sobe, e nada justifica
    # segurá-lo por mais cinco minutos. O intervalo folgado existe para não
    # empilhar tentativa enquanto o BigQuery está fora — preocupação da
    # varredura recorrente, não da primeira.
    #
    # A espera curta, e não zero, dá lugar ao preflight e à sondagem inicial de
    # dependências: começar um drain no mesmo instante do boot faria as duas
    # coisas disputarem o pool de escrita justo quando ele é criado.
    espera = min(_PRIMEIRA_VARREDURA_SEGUNDOS, intervalo)
    logger.info(
        f"Worker de drain da DLQ iniciado (primeira varredura em {espera}s, "
        f"depois a cada {intervalo}s)."
    )

    while True:
        try:
            await asyncio.sleep(espera)
            espera = intervalo
            resumo = await loop.run_in_executor(
                _get_write_executor(), replay_bigquery_dlq
            )
            if resumo["itens"] or resumo["poison"]:
                logger.info(
                    f"Drain da DLQ: {resumo['itens']} item(ns) / {resumo['linhas']} "
                    f"linha(s) devolvidos ao BigQuery; {resumo['poison']} em poison; "
                    f"{resumo['pendentes']} ainda pendente(s)."
                )
            if resumo["erros"]:
                logger.warning(
                    f"Drain da DLQ terminou com {len(resumo['erros'])} erro(s); "
                    f"os itens seguem na fila. Primeiro: {resumo['erros'][0]}"
                )
        except asyncio.CancelledError:
            logger.info("Worker de drain da DLQ encerrado.")
            raise
        except Exception:
            logger.exception("Falha inesperada no worker de drain da DLQ")


# ---------------------------------------------------------------------------
# Module-level initialisation — must come after flush_bigquery_batch_buffer is
# defined so the flush thread can call it safely.
# ---------------------------------------------------------------------------

# Rede de segurança para o encerramento programático (testes, uso embarcado,
# `sys.exit`). Atenção ao que `atexit` NÃO cobre: morte por sinal com handler
# default não passa por aqui — daí `_install_shutdown_signal_handlers()` logo
# abaixo, que é o caminho que de fato executa em produção.
atexit.register(_stop_batch_flush_thread)

# Encerra o pool de leituras no mesmo momento (não espera query pendurada).
atexit.register(_shutdown_read_executor)

# Flush do buffer em SIGTERM/SIGINT. Precisa ser instalado *antes* de a uvicorn
# capturar os sinais: é o handler que ela guarda como "anterior", restaura ao
# sair de `serve()` e re-levanta. Como este módulo é importado por `src.app`,
# que a `src.main` importa antes de `mcp.run()`, a ordem está garantida.
_install_shutdown_signal_handlers()

# Start the periodic background flush thread for the lifetime of this process.
_start_batch_flush_thread()
