"""
Audit log estruturado pra tools com side-effect.

C4 do plano-bot-2026 Fase 0. LGPD + accountability gov exigem que cada
acao irreversivel (abrir Case, registrar denuncia, mandar mensagem, gravar
memoria) seja registrada num log append-only com:

- Snowflake ID (time-ordered)
- Hash do telefone (NUNCA raw)
- action_type canonical
- tool_name
- input_summary (PII-redacted + truncado)
- output_summary (idem)
- success
- trace_id (OTel se disponivel)
- sensitivity (low/medium/high)
- timestamp UTC ISO 8601

O log emitido vai pro logger dedicado `audit_logger` (loguru). Em
producao, o sink desse logger deve ser configurado pra append-only storage
segregado (BigQuery dataset `bot_audit` com retention >=5 anos, write-only
role) -- por enquanto, herda do logger global (stdout). Setup do sink real
e topico de C4 follow-up.

Uso:

    from src.observability.audit_log import audit_log

    @audit_log(action_type="upsert_user_memory", sensitivity="medium")
    async def upsert_memory(user_id: str, memory_bank: dict) -> dict:
        ...

O decorator nao re-levanta a excecao em si (delega ao wrapper interno
caller). Em caso de erro do callee, registra com `success=False` antes de
propagar. Em caso de erro DO PROPRIO logger (sink down, formatador
quebrado), engole silenciosamente -- audit nunca deve quebrar o caller.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

from src.utils.log import logger as _root_logger


# Logger dedicado pro audit. Hoje compartilha sink com o logger global
# (loguru singleton); em producao, configure um sink BigQuery via
# audit_logger.add(...) num bootstrap separado pra atender a politica
# de retention >=5 anos. Mantemos o nome distinto pra grep e pra
# permitir filtros futuros.
audit_logger = _root_logger.bind(channel="audit")


Sensitivity = Literal["low", "medium", "high"]


# --- Snowflake ID --------------------------------------------------------
# Snowflake-lite: 41 bits timestamp ms + 10 bits machine + 12 bits counter.
# Ja existe no ecossistema (prefeitura-rio lib), mas pra evitar
# dependencia circular e overhead, mantemos local.

_SNOWFLAKE_EPOCH_MS = 1_700_000_000_000  # ~2023-11-14, anchor estavel


def _parse_machine_id(raw: Optional[str]) -> int:
    """Parse defensivo do `AUDIT_MACHINE_ID`.

    Operador pode setar string vazia, nao-numerica, negativo, ou valor
    fora de 10 bits. Em qualquer um desses casos, cai pra `0` (default)
    sem quebrar o import do modulo -- audit nunca deve impedir o
    servidor de subir.

    Codex review P2 (gpt-5.3-codex-spark) flagou que `int("")` ou
    `int("foo")` quebrariam o import. Codex review P3 flagou que `-1`
    via bitmask viraria 1023 silenciosamente -- mascara nao filtra
    intencao, so range; rejeitamos out-of-range com warning em vez de
    mapear pra valor valido qualquer.
    """
    if raw is None:
        return 0
    raw_str = raw.strip()
    if not raw_str:
        return 0
    try:
        parsed = int(raw_str)
    except (TypeError, ValueError):
        try:
            _root_logger.warning(
                f"audit_log: AUDIT_MACHINE_ID invalido ({raw_str!r}), "
                "caindo pra 0. Setar valor numerico 0-1023."
            )
        except Exception:
            pass
        return 0
    # Rejeita explicitamente valores fora de 10 bits (0-1023) em vez de
    # bitmask que silenciosamente mapeia -1 -> 1023 ou 1024 -> 0. Operator
    # que setou valor errado deve ver warning, nao continuar com node id
    # diferente do esperado (que quebraria garantia de unicidade entre
    # pods).
    if parsed < 0 or parsed > 0x3FF:
        try:
            _root_logger.warning(
                f"audit_log: AUDIT_MACHINE_ID fora de range ({parsed}); "
                "esperado 0-1023. Caindo pra 0."
            )
        except Exception:
            pass
        return 0
    return parsed


_MACHINE_ID = _parse_machine_id(os.environ.get("AUDIT_MACHINE_ID"))
_snowflake_lock = threading.Lock()
_snowflake_counter = 0
_snowflake_last_ms = 0


def generate_snowflake_id() -> str:
    """Gera ID time-ordered no formato 'snowflake_<int>'.

    63 bits: 41 timestamp_ms + 10 machine + 12 counter. Retorna como string
    pra evitar coercao implicita em sinks que serializam pra JS (perda de
    precisao em ints >2^53).
    """
    global _snowflake_counter, _snowflake_last_ms

    with _snowflake_lock:
        now_ms = int(time.time() * 1000) - _SNOWFLAKE_EPOCH_MS
        if now_ms < 0:
            # Clock drift extremo; clamp pra epoch zero pra nao gerar negativos.
            now_ms = 0

        # Codex review P2 (gpt-5.3-codex-spark) flagou: se o wall-clock
        # andar pra TRAS (NTP step, restore de snapshot, etc), o codigo
        # anterior resetava `_snowflake_last_ms = now_ms` e quebrava a
        # monotonicidade entre ids. Aqui preservamos: nunca permitimos
        # que `now_ms` ande pra tras alem do ultimo emitido. Se for igual,
        # ainda incrementamos counter (mesma logica do `==`). Se for novo
        # (>), avancamos normalmente.
        if now_ms < _snowflake_last_ms:
            now_ms = _snowflake_last_ms  # clamp forward

        if now_ms == _snowflake_last_ms:
            _snowflake_counter = (_snowflake_counter + 1) & 0xFFF  # 12 bits
            if _snowflake_counter == 0:
                # Overflow no mesmo ms (4096 ids/ms): avancamos
                # incondicionalmente pro proximo slot logico (`+1`). Codex
                # review P1 (gpt-5.3-codex-spark) leu o spin loop anterior
                # como infinite -- nao era (`max(fresh, _last+1)` sempre
                # avancava), mas era confuso. Avanco direto e seguro porque
                # a precisao de timestamp em ms ja e arbitrariamente
                # antecipada (`+1ms`) em caso de overflow; o sistema vai
                # alcancar quando o wall-clock realmente passar do epoch
                # extrapolado.
                now_ms = _snowflake_last_ms + 1
                _snowflake_last_ms = now_ms
        else:
            _snowflake_counter = 0
            _snowflake_last_ms = now_ms

        snowflake_int = (now_ms << 22) | (_MACHINE_ID << 12) | _snowflake_counter
        return f"snowflake_{snowflake_int}"


# --- User hash -----------------------------------------------------------

_USER_HASH_SALT = os.environ.get("AUDIT_USER_HASH_SALT", "rio-bot-audit-v1")


def hash_user_id(user_id: Optional[str]) -> str:
    """Hash determinístico dos últimos 8 dígitos do telefone.

    Retorna `"unknown"` quando `user_id` é falsy/inválido. Para qualquer
    string nao-vazia, usa os últimos 8 dígitos (após strip de não-dígitos)
    como source, prefixados por um salt definido em `AUDIT_USER_HASH_SALT`.
    Trunca a 16 chars de hex pra reduzir tamanho do log sem comprometer
    pseudonimização (16 hex = 64 bits collision space — adequado pra audit
    sob uma populacao de ~10M cidadaos).
    """
    if not user_id or not isinstance(user_id, str):
        return "unknown"
    digits_only = re.sub(r"\D", "", user_id)
    if not digits_only:
        # Nao-numerico mas existe (e.g. uuid). Hashia o input inteiro.
        material = user_id
    else:
        material = digits_only[-8:]
    digest = hashlib.sha256(f"{_USER_HASH_SALT}|{material}".encode("utf-8")).hexdigest()
    return f"u_{digest[:16]}"


# --- PII redaction -------------------------------------------------------
# Patterns conservadores: CPF, CEP, telefone E.164 (8+ dígitos seguidos),
# email. Para campos onde sabemos que vem PII (memory.value, descricao
# usuario), o caller deve ter ja truncado/contexto; aqui so adicionamos
# camada defensiva. Em prod, esperamos que o middleware Engine (C3 do
# plano) faca redaction primaria — este e segunda linha.

_CPF_PATTERN = re.compile(r"\b\d{3}[.\s]?\d{3}[.\s]?\d{3}[-\s]?\d{2}\b")
_CEP_PATTERN = re.compile(r"\b\d{5}[-\s]?\d{3}\b")
_PHONE_E164_PATTERN = re.compile(r"\+?\d{10,15}")
_EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def redact_pii(text: str) -> str:
    """Substitui CPF / CEP / telefone / email por placeholders.

    Padrões e ordem importam: CPF/CEP/email primeiro pra nao deixar o
    telefone E.164 (mais ganancioso) capturar substrings de outros padroes.
    """
    if not text or not isinstance(text, str):
        return text
    redacted = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    redacted = _CPF_PATTERN.sub("[REDACTED_CPF]", redacted)
    redacted = _CEP_PATTERN.sub("[REDACTED_CEP]", redacted)
    redacted = _PHONE_E164_PATTERN.sub("[REDACTED_PHONE]", redacted)
    return redacted


# --- Summary helpers -----------------------------------------------------

_DEFAULT_SUMMARY_MAX = 500


def _stringify(value: Any) -> str:
    """Serializacao defensiva: tenta json, cai pra repr."""
    if value is None:
        return "None"
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return repr(value)


def _summarize(value: Any, max_len: int = _DEFAULT_SUMMARY_MAX) -> str:
    """Stringify + truncate + redact. Ordem: stringify → redact → truncate."""
    stringified = _stringify(value)
    redacted = redact_pii(stringified)
    if len(redacted) <= max_len:
        return redacted
    return redacted[: max_len - 3] + "..."


def _trace_id_from_otel() -> Optional[str]:
    """Tenta obter trace id do contexto OTel ativo. Falha silenciosa se
    opentelemetry-api nao estiver carregado ou nao houver span ativo."""
    try:
        from opentelemetry import trace  # type: ignore

        span = trace.get_current_span()
        if span is None:
            return None
        ctx = span.get_span_context()
        if not getattr(ctx, "is_valid", False):
            return None
        return f"{ctx.trace_id:032x}"
    except Exception:
        return None


# --- record_action -------------------------------------------------------


def _normalize_user_hash(value: str) -> str:
    """Garante que o `user_hash` emitido NUNCA carregue PII raw.

    Codex review P3 (gpt-5.3-codex-spark): callers diretos de `record_action`
    podem passar telefone raw como `user_hash` por engano. Detectamos pelo
    prefixo esperado (`u_` ou literal `unknown`); qualquer outro valor
    e tratado como raw e re-hasheado.
    """
    if not isinstance(value, str) or not value:
        return "unknown"
    if value == "unknown" or value.startswith("u_"):
        return value
    # Valor nao normalizado -- assume raw e re-hashia.
    return hash_user_id(value)


def record_action(
    *,
    action_type: str,
    tool_name: str,
    user_hash: str,
    input_summary: str,
    output_summary: str,
    success: bool,
    sensitivity: Sensitivity,
    snowflake_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Emite um audit record estruturado e retorna o dict serializado.

    Esta funcao e o sink "low-level" usado tanto pelo decorator quanto por
    callers que queiram emitir audit fora de uma tool decorada (raro).
    Normaliza `user_hash` defensivamente: se chegar valor que NAO seja
    `unknown` nem prefixo `u_<hex>`, considera-se que e raw e aplica
    `hash_user_id` antes de emitir. Garante que append-only audit logs
    nunca carreguem PII plaintext.

    Em qualquer erro de serializacao/sink, suprime e loga warning -- audit
    nunca derruba o caller.
    """
    if snowflake_id is None:
        snowflake_id = generate_snowflake_id()
    if trace_id is None:
        trace_id = _trace_id_from_otel()

    record: dict[str, Any] = {
        "snowflake_id": snowflake_id,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "action_type": action_type,
        "tool_name": tool_name,
        "user_hash": _normalize_user_hash(user_hash),
        "input_summary": input_summary,
        "output_summary": output_summary,
        "success": success,
        "sensitivity": sensitivity,
        "trace_id": trace_id,
    }

    try:
        # `bind(**record)` poe os campos em `message.record["extra"]`,
        # facilitando sinks estruturados (BigQuery, JSON formatter) e
        # captura em testes sem ter que reparsear a string formatada.
        audit_logger.bind(**record).info("audit_record")
    except Exception as logger_err:  # pragma: no cover - defensive
        # Se o logger morreu (sink-config issue), nao quebramos o caller.
        try:
            _root_logger.warning(
                f"audit_log: falha ao emitir audit record "
                f"({type(logger_err).__name__}): {logger_err}. "
                f"action_type={action_type} tool_name={tool_name}"
            )
        except Exception:
            pass

    return record


# --- Decorator -----------------------------------------------------------


def _resolve_user_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Optional[str]:
    """Extracao conservadora do `user_id` / `user_number` do call.

    Codex review P2 (gpt-5.3-codex-spark) flagou: extracao positional
    heuristica produz user_hash bogus em tools cuja primeira pos-arg NAO
    e user_id (e.g. `build_whatsapp_media_envelope("image", ...)` -- o
    "image" virava o user!). Pra evitar attribution incorreta:

    1. Aceitamos APENAS kwargs nomeados (`user_id`, `user_number`).
    2. Nao tentamos adivinhar via positional.
    3. Tools que recebem user_id positional DEVEM passar `extract_user_id`
       explicito no decorator -- ver exemplo em `cor_alert_tools.py`.

    Resultado: melhor `unknown` honesto do que hash falso.
    """
    for key in ("user_id", "user_number"):
        if key in kwargs and isinstance(kwargs[key], str):
            return kwargs[key]
    return None


def _default_success_from_result(result: Any) -> bool:
    """Inspeciona payload de retorno pra inferir sucesso funcional.

    Muitas tools do MCP retornam dict com `success=False` ou
    `status="error"` em vez de levantar excecao (ver `create_cor_alert`,
    `build_whatsapp_media_envelope`, `register_sgrc_ticket`). Sem essa
    inspecao, todo retorno nao-throwing era marcado `success=True` no
    audit -- mascarando falhas funcionais. Codex review P2
    (gpt-5.3-codex-spark) flagou isso como risco de alerting blind spot.

    Regras (qualquer match retorna False):
    - dict com `success=False`
    - dict com `status` em {"error", "rejected", "failed"}
    - dict com chave `error` (truthy)
    - dict com `api_resposta_sucesso=False` (padrao divida_ativa)

    Em qualquer outro retorno (sucesso, None, lista, scalar), assume True.
    """
    if not isinstance(result, dict):
        return True
    if result.get("success") is False:
        return False
    if isinstance(result.get("status"), str) and result["status"].lower() in {
        "error",
        "rejected",
        "failed",
    }:
        return False
    if result.get("error"):
        return False
    if result.get("api_resposta_sucesso") is False:
        return False
    return True


def audit_log(
    action_type: str,
    sensitivity: Sensitivity = "medium",
    *,
    extract_user_id: Optional[Callable[[tuple, dict], Optional[str]]] = None,
    success_predicate: Optional[Callable[[Any], bool]] = None,
    input_max_len: int = _DEFAULT_SUMMARY_MAX,
    output_max_len: int = _DEFAULT_SUMMARY_MAX,
) -> Callable:
    """Decorator que emite audit record antes/depois da chamada.

    Args:
        action_type: identifier canonical da acao (ex: "open_complaint",
            "send_whatsapp_text", "upsert_user_memory"). Convencao: snake_case.
        sensitivity: "low" / "medium" / "high". Guia retention/alerting
            policies no sink real (BigQuery).
        extract_user_id: callable opcional pra customizar extracao do
            user_id (recebe args, kwargs). Default usa heuristica padrao.
        success_predicate: callable opcional `(result) -> bool` pra inferir
            sucesso a partir do payload. Default (`_default_success_from_result`)
            inspeciona chaves `success`, `status`, `error`,
            `api_resposta_sucesso` -- ver doc da funcao. Passe `lambda _: True`
            pra desligar a inspecao se a tool tem retorno opaco.
        input_max_len: limite de chars do `input_summary`.
        output_max_len: limite de chars do `output_summary`.

    Suporta sync e async (replica padrao do `interceptor`). Sempre re-levanta
    excecoes do callee depois de emitir audit com `success=False`. Audit
    em si nunca interrompe o callee.
    """

    def decorator(func: Callable) -> Callable:
        is_async = asyncio.iscoroutinefunction(func)
        tool_name = func.__name__
        predicate = success_predicate or _default_success_from_result

        def _resolve_success(threw_exception: bool, result: Any) -> bool:
            if threw_exception:
                return False
            try:
                return bool(predicate(result))
            except Exception:  # pragma: no cover - defensive
                # Predicate quebrou; assume sucesso (nao mascarar throw real).
                return True

        def _emit(args: tuple, kwargs: dict, output: Any, success: bool) -> None:
            try:
                resolver = extract_user_id or _resolve_user_id
                raw_user = resolver(args, kwargs)
                # Sanitizes os args pra log: remove `self`/`cls` se for
                # method, mas como aqui nao temos contexto de classe,
                # passamos args + kwargs juntos como dict.
                try:
                    args_dict = {f"arg{i}": a for i, a in enumerate(args)}
                except Exception:
                    args_dict = {"args": "<unserializable>"}
                input_payload = {**args_dict, **kwargs}
                record_action(
                    action_type=action_type,
                    tool_name=tool_name,
                    user_hash=hash_user_id(raw_user),
                    input_summary=_summarize(input_payload, max_len=input_max_len),
                    output_summary=_summarize(output, max_len=output_max_len),
                    success=success,
                    sensitivity=sensitivity,
                )
            except Exception as audit_err:  # pragma: no cover - defensive
                try:
                    _root_logger.warning(
                        f"audit_log decorator: erro ao registrar audit "
                        f"({type(audit_err).__name__}): {audit_err}. "
                        f"tool={tool_name} action={action_type}"
                    )
                except Exception:
                    pass

        if is_async:

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    _emit(
                        args,
                        kwargs,
                        {"error": str(exc), "error_type": type(exc).__name__},
                        success=False,
                    )
                    raise
                _emit(args, kwargs, result, success=_resolve_success(False, result))
                return result

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                _emit(
                    args,
                    kwargs,
                    {"error": str(exc), "error_type": type(exc).__name__},
                    success=False,
                )
                raise
            _emit(args, kwargs, result, success=_resolve_success(False, result))
            return result

        return sync_wrapper

    return decorator
