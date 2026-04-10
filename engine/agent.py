from typing import Any, Iterator, List, Optional, AsyncIterable
from langchain.load.dump import dumpd
from datetime import datetime, timezone
import json
import asyncio
import ast
from langchain_core.messages import trim_messages

# from langgraph.prebuilt import create_react_agent
# use custom graph without _validate_chat_history
from engine.custom_react_agent import create_react_agent
from langchain_google_vertexai import ChatVertexAI
from langchain_core.tools import BaseTool
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from vertexai.agent_engines import (
    AsyncQueryable,
    AsyncStreamQueryable,
    Queryable,
    StreamQueryable,
)

from os import getenv
from langchain_google_cloud_sql_pg import (
    PostgresEngine,
    PostgresSaver,
)

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from opentelemetry.instrumentation.langchain import LangchainInstrumentor

from engine.log import logger


class Agent(AsyncQueryable, AsyncStreamQueryable, Queryable, StreamQueryable):
    """
    An agent for sync/async/streaming queries with state persisted in PostgreSQL.

    Components are initialized lazily on the first query.

    Use engine.init_checkpoint_table() if the table does not exists
    """

    def __init__(
        self,
        *,
        model: str = "gemini-2.5-flash",
        system_prompt: str = "YOU ALWAYS RESPOND: `SYSTEM PROMPT NOT SET`",
        tools: List[BaseTool] = [],
        temperature: float = 0.7,
        include_thoughts: bool = True,
        thinking_budget: int = -1,
        otpl_service: str = "langgraph-eai-vX",
    ):
        self._model = model
        self._tools = tools or []
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._include_thoughts = include_thoughts
        self._thinking_budget = thinking_budget
        self._otpl_service = otpl_service
        # Database configuration
        self._project_id = getenv("PROJECT_ID", "")
        self._region = getenv("LOCATION", "")
        self._instance_name = getenv("INSTANCE", "")
        self._database_name = getenv("DATABASE", "")
        self._database_user = getenv("DATABASE_USER", "")
        self._database_password = getenv("DATABASE_PASSWORD", "")
        self._otlp_endpoint = getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
        self._otlp_header = getenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "")

        self._graph = None
        self._setup_complete_async = False
        self._setup_complete_sync = False
        self._opentelemetry_setup_complete = False

        # OpenTelemetry tracer e processor para shutdown
        self._tracer = None
        self._batch_processor = None
        self._shutdown_handlers_registered = False

        # Short-term memory limits - lazy loaded from env vars
        self._short_memory_time_limit = None
        self._short_memory_token_limit = None

        # Get user memory tool - lazy loaded
        self._user_memory_tool = None

        # Long-term memory cache to avoid redundant fetches
        # {thread_id: {"data": memory_data, "timestamp": datetime}}
        self._memory_cache = {}
        self._memory_needs_refresh = False  # Flag set when upsert_user_memory is called

    def _set_up_opentelemetry(self):
        if self._opentelemetry_setup_complete:
            return
        provider = TracerProvider(
            resource=Resource.create({"service.name": self._otpl_service}),
            sampler=ALWAYS_ON,  # Garantir 100% de sampling
        )
        otlp_exporter = OTLPSpanExporter(
            endpoint=self._otlp_endpoint,
            headers=(
                dict(
                    header.split("=")
                    for header in self._otlp_header.split(",")
                    if "=" in header
                )
                if self._otlp_header
                else None
            ),
        )

        # Configurar BatchSpanProcessor com parâmetros otimizados para reduzir perda de spans
        self._batch_processor = BatchSpanProcessor(
            otlp_exporter,
            max_queue_size=8192,  # Aumentar buffer (padrão: 2048)
            schedule_delay_millis=1000,  # Flush mais frequente (padrão: 5000)
            export_timeout_millis=10000,  # Timeout menor (padrão: 30000)
            max_export_batch_size=256,  # Lotes menores para reduzir latência (padrão: 512)
        )
        provider.add_span_processor(self._batch_processor)
        trace.set_tracer_provider(provider)

        # Initialize tracer
        self._tracer = trace.get_tracer(__name__)

        LangchainInstrumentor().instrument()

        self._opentelemetry_setup_complete = True

    def _trace_conversation(self, filtered_result: dict, **kwargs):
        """Simple tracing to show user input and model output."""
        if not self._tracer:
            return

        # Extract input message
        input_msg = str(kwargs.get("input", ""))

        # Extract thread_id
        thread_id = (
            kwargs.get("config", {}).get("configurable", {}).get("thread_id", "unknown")
        )

        with self._tracer.start_as_current_span("conversation") as span:
            span.set_attributes(
                {
                    "user.input": input_msg,
                    "model.output": json.dumps(
                        dumpd(filtered_result), ensure_ascii=False, indent=2
                    ),
                    "thread.id": thread_id,
                    "model.name": self._model,
                    "model.temperature": self._temperature,
                }
            )

    def set_up(self):
        """Mark that setup is needed - actual setup happens lazily."""
        self._setup_complete_async = False
        self._setup_complete_sync = False

    def _sanitize_input_messages(self, **kwargs):
        """Sanitizes input messages to prevent Vertex AI errors with integer lists in strings.

        Checks if any message content is a string that ast.literal_eval would parse
        into a list or tuple containing integers (e.g., "1,2,3"). If so, wraps it
        in repr() to ensure it remains a string when processed by Vertex AI.
        """
        if "input" not in kwargs or "messages" not in kwargs["input"]:
            return kwargs

        messages = kwargs["input"]["messages"]
        for message in messages:
            # Handle dicts (common input format)
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    try:
                        parsed = ast.literal_eval(content)
                        if isinstance(parsed, (list, tuple)):
                            has_int = any(isinstance(item, int) for item in parsed)
                            if has_int:
                                # Wrap in repr to ensure it stays a string when Vertex parses it
                                message["content"] = repr(content)
                                logger.info(
                                    f"Sanitized input message: wrapped content in quotes: {message['content']}"
                                )
                    except (ValueError, SyntaxError):
                        pass
            # Handle objects (if input is BaseMessage objects)
            elif hasattr(message, "content") and isinstance(message.content, str):
                try:
                    parsed = ast.literal_eval(message.content)
                    if isinstance(parsed, (list, tuple)):
                        has_int = any(isinstance(item, int) for item in parsed)
                        if has_int:
                            message.content = repr(message.content)
                            logger.info(
                                f"Sanitized input message object: wrapped content in quotes: {message.content}"
                            )
                except (ValueError, SyntaxError):
                    pass
        return kwargs

    def _combined_pre_invoke_hook(self, **kwargs):
        """Centralizes all manipulations on input arguments before invoking the graph."""
        kwargs = self._add_timestamp_to_input_messages(**kwargs)
        kwargs = self._sanitize_input_messages(**kwargs)
        return kwargs

    def _add_timestamp_to_input_messages(self, **kwargs):
        "Adiciona timestamp nas mensagens do usuario antes do invoke"
        msg_datetime = datetime.now(timezone.utc).isoformat()
        for message in kwargs["input"]["messages"]:
            message["additional_kwargs"] = {"timestamp": msg_datetime}
        return kwargs

    def _add_timestamp_to_tool_messages(self, state):
        """Hook para adicionar timestamp nas ToolMessages após execução."""
        messages = state.get("messages", [])
        current_time = datetime.now(timezone.utc).isoformat()
        updates = []

        # Adicionar timestamp apenas nas ToolMessages que não têm
        for message in messages:
            if (
                isinstance(message, ToolMessage)
                and hasattr(message, "additional_kwargs")
                and "timestamp" not in message.additional_kwargs
            ):
                message.additional_kwargs["timestamp"] = current_time
                updates.append(message)

        # Retorna APENAS as mensagens modificadas para evitar duplicação no add_messages
        return {"messages": updates} if updates else {}

    def _add_timestamp_to_ai_message(self, state):
        """Hook para adicionar timestamp na AIMessage (Agent) logo após geração."""
        messages = state.get("messages", [])
        if not messages:
            return {}

        last_message = messages[-1]
        current_time = datetime.now(timezone.utc).isoformat()

        if (
            isinstance(last_message, AIMessage)
            and hasattr(last_message, "additional_kwargs")
            and "timestamp" not in last_message.additional_kwargs
        ):
            last_message.additional_kwargs["timestamp"] = current_time
            # Retorna apenas a mensagem modificada
            return {"messages": [last_message]}

        return {}

    def _get_short_memory_limits(self):
        """Lazy load short-term memory limits from environment variables."""
        if self._short_memory_time_limit is None:
            # Convert days to seconds
            self._short_memory_time_limit = round(
                float(getenv("SHORT_MEMORY_TIME_LIMIT", "7")) * 86400
            )
            logger.info(
                f"Short memory time limit set to {self._short_memory_time_limit} seconds"
            )
        if self._short_memory_token_limit is None:
            self._short_memory_token_limit = int(
                getenv("SHORT_MEMORY_TOKEN_LIMIT", "100")
            )
            logger.info(
                f"Short memory token limit set to {self._short_memory_token_limit} tokens"
            )

        return self._short_memory_time_limit, self._short_memory_token_limit

    def _get_user_memory_tool(self):
        """Lazy load the user memory tool from the tools list."""
        if self._user_memory_tool is None:
            self._user_memory_tool = next(
                (tool for tool in self._tools if tool.name == "get_user_memory"), None
            )
        return self._user_memory_tool

    def _ensure_complete_tool_pairs(self, filtered_messages, full_messages, logger):
        """Ensure that all tool calls have corresponding tool responses and vice versa.

        This prevents errors when token filtering breaks tool call/response pairs.
        Instead of removing orphaned messages, we add missing pairs from the full history.

        Args:
            filtered_messages: Messages after token filtering
            full_messages: All messages from the database (complete history)
            logger: Logger instance

        Returns:
            List of messages with complete tool pairs
        """
        if not filtered_messages:
            return filtered_messages

        # Collect all tool call IDs that have calls in filtered messages
        tool_calls_in_filtered = set()
        for msg in filtered_messages:
            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls"):
                for tool_call in msg.tool_calls:
                    tool_calls_in_filtered.add(tool_call.get("id"))

        # Collect all tool call IDs that have responses in filtered messages
        tool_responses_in_filtered = set()
        for msg in filtered_messages:
            if isinstance(msg, ToolMessage) and hasattr(msg, "tool_call_id"):
                tool_responses_in_filtered.add(msg.tool_call_id)

        # Find orphaned tool calls (call without response)
        orphaned_calls = tool_calls_in_filtered - tool_responses_in_filtered

        # Find orphaned responses (response without call)
        orphaned_responses = tool_responses_in_filtered - tool_calls_in_filtered

        if not orphaned_calls and not orphaned_responses:
            # All tool pairs are complete
            return filtered_messages

        logger.warning(
            f"[Short-Term Memory] Found incomplete tool pairs - "
            f"orphaned calls (missing response): {len(orphaned_calls)}, "
            f"orphaned responses (missing call): {len(orphaned_responses)}"
        )

        # Build a complete message list by adding missing pairs
        complete_messages = list(filtered_messages)

        # Add missing tool responses for orphaned calls
        if orphaned_calls:
            added_count = 0
            for msg in full_messages:
                if isinstance(msg, ToolMessage) and hasattr(msg, "tool_call_id"):
                    if (
                        msg.tool_call_id in orphaned_calls
                        and msg not in complete_messages
                    ):
                        complete_messages.append(msg)
                        added_count += 1
            logger.info(
                f"[Short-Term Memory] Added {added_count} missing ToolMessage(s) to complete tool calls"
            )

        # Add missing tool calls for orphaned responses
        # Instead of removing orphaned responses, find and add the AIMessages that made those calls
        if orphaned_responses:
            added_count = 0
            for msg in full_messages:
                if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls"):
                    for tool_call in msg.tool_calls:
                        tool_call_id = tool_call.get("id")
                        if (
                            tool_call_id in orphaned_responses
                            and msg not in complete_messages
                        ):
                            complete_messages.append(msg)
                            added_count += 1
                            break  # Only add the message once, even if it has multiple relevant tool calls
            logger.info(
                f"[Short-Term Memory] Added {added_count} missing AIMessage(s) with tool calls to complete tool responses"
            )

        # Sort by timestamp to maintain chronological order
        complete_messages.sort(
            key=lambda m: (
                getattr(m, "additional_kwargs", {}).get("timestamp", "")
                if hasattr(m, "additional_kwargs")
                else ""
            )
        )

        return complete_messages

    def _filter_short_term_memory(self, state):
        """Filter messages based on time and token limits for short-term memory.

        This method implements short-term memory by:
        1. Filtering out messages older than SHORT_MEMORY_TIME_LIMIT
        2. Applying token limit using trimMessages
        3. Always preserving system messages

        NOTE: PostgresCheckpointer loads ALL messages from the database for the thread.
        This filter reduces what goes to the LLM (saves tokens/improves performance),
        but the full history remains in the database.

        Args:
            state: The current state containing messages (full history from database)

        Returns:
            dict: Updated state with filtered messages (only recent messages)
        """

        messages = state.get("messages", [])

        # Get limits lazily
        SHORT_MEMORY_TIME_LIMIT, SHORT_MEMORY_TOKEN_LIMIT = (
            self._get_short_memory_limits()
        )

        if not messages:
            return {"messages": []}

        # Log the initial message count (retrieved from database)
        logger.info(
            f"[Short-Term Memory] Loaded {len(messages)} messages from database"
        )

        # Separate system messages (always kept)
        system_messages = [msg for msg in messages if isinstance(msg, SystemMessage)]
        non_system_messages = [
            msg for msg in messages if not isinstance(msg, SystemMessage)
        ]

        logger.info(
            f"[Short-Term Memory] System messages: {len(system_messages)}, Non-system messages: {len(non_system_messages)}"
        )

        if not non_system_messages:
            return {"messages": system_messages}

        # Step 1: Time filtering - remove messages older than time limit
        current_time = datetime.now(timezone.utc)
        time_filtered_messages = []

        for message in non_system_messages:
            timestamp_str = (
                message.additional_kwargs.get("timestamp")
                if hasattr(message, "additional_kwargs")
                else None
            )

            if timestamp_str:
                try:
                    message_time = datetime.fromisoformat(
                        timestamp_str.replace("Z", "+00:00")
                    )
                    age_seconds = (current_time - message_time).total_seconds()

                    if age_seconds <= SHORT_MEMORY_TIME_LIMIT:
                        time_filtered_messages.append(message)
                except (ValueError, AttributeError) as e:
                    logger.warning(
                        f"Invalid timestamp format in message: {timestamp_str}, error: {e}"
                    )
                    # Keep messages with invalid timestamps
                    time_filtered_messages.append(message)
            else:
                # Keep messages without timestamps (e.g., new messages)
                time_filtered_messages.append(message)

        if not time_filtered_messages:
            # If all messages are filtered out, keep at least the last message
            logger.warning(
                f"[Short-Term Memory] All messages filtered by time limit, keeping last message"
            )
            time_filtered_messages = [non_system_messages[-1]]
        else:
            messages_filtered_by_time = len(non_system_messages) - len(
                time_filtered_messages
            )
            if messages_filtered_by_time > 0:
                logger.info(
                    f"[Short-Term Memory] Filtered out {messages_filtered_by_time} messages older than {SHORT_MEMORY_TIME_LIMIT / 86400:.1f} days"
                )

        # Step 2: Apply token limiting using trimMessages
        try:
            # Use trimMessages to limit tokens
            token_filtered_messages = trim_messages(
                time_filtered_messages,
                max_tokens=SHORT_MEMORY_TOKEN_LIMIT,
                strategy="last",
                token_counter=lambda msgs: sum(
                    len(str(m.content)) // 4 for m in msgs
                ),  # Rough token estimation
                start_on="human",
                end_on=["human", "tool"],
            )

            messages_filtered_by_tokens = len(time_filtered_messages) - len(
                token_filtered_messages
            )
            if messages_filtered_by_tokens > 0:
                logger.info(
                    f"[Short-Term Memory] Filtered out {messages_filtered_by_tokens} messages due to {SHORT_MEMORY_TOKEN_LIMIT} token limit"
                )

            # If trim_messages returns empty or if the most recent message alone exceeds the limit
            if not token_filtered_messages:
                logger.error(
                    f"[Short-Term Memory] The most recent message exceeds token limit ({SHORT_MEMORY_TOKEN_LIMIT} tokens). "
                    "Proceeding with just the last message."
                )
                token_filtered_messages = [time_filtered_messages[-1]]

        except Exception as e:
            logger.error(
                f"[Short-Term Memory] Error applying token limit: {e}. Using time filtered messages."
            )
            token_filtered_messages = time_filtered_messages

        # Step 2.5: Check if we need to validate tool pairs (performance optimization)
        # Only check if we have any tool-related messages
        has_tool_messages = any(
            isinstance(msg, (ToolMessage))
            or (
                isinstance(msg, AIMessage)
                and hasattr(msg, "tool_calls")
                and msg.tool_calls
            )
            for msg in token_filtered_messages
        )

        if has_tool_messages:
            # CRITICAL: Ensure we don't have orphaned tool calls or tool messages
            # Check if we have incomplete tool call/response pairs and fix them
            # Pass full messages from database so we can find tool calls/responses that were filtered out
            token_filtered_messages = self._ensure_complete_tool_pairs(
                token_filtered_messages, messages, logger
            )

            # If tool pair validation removed all messages, ensure we have at least one message
            if not token_filtered_messages:
                logger.warning(
                    "[Short-Term Memory] Tool pair validation removed all messages. "
                    "Keeping last HumanMessage to avoid empty context."
                )
                # Find the most recent HumanMessage
                for msg in reversed(time_filtered_messages):
                    if isinstance(msg, HumanMessage):
                        token_filtered_messages = [msg]
                        break
                # If no HumanMessage found, keep the last message
                if not token_filtered_messages:
                    token_filtered_messages = [time_filtered_messages[-1]]
        else:
            logger.debug(
                "[Short-Term Memory] No tool messages found, skipping tool pair validation"
            )

        # Step 3: Combine system messages with filtered messages
        filtered_messages = system_messages + token_filtered_messages

        logger.info(
            f"[Short-Term Memory] Final result: {len(filtered_messages)} messages "
            f"({len(system_messages)} system + {len(token_filtered_messages)} conversation) "
            f"sent to LLM out of {len(messages)} total in database"
        )

        # Use llm_input_messages to pass filtered messages to LLM without updating state
        # This prevents the checkpointer from seeing the filtered messages
        return {"llm_input_messages": filtered_messages}

    async def _fetch_long_term_memory(self, thread_id: str) -> dict:
        """Fetch long-term memory data from HTTP endpoint.

        This method should be implemented to fetch memory data from your
        long-term memory service via HTTP request.

        Args:
            thread_id: The conversation thread identifier

        Returns:
            dict: JSON containing long-term memory data
        """
        logger.info(f"[Long-Term Memory] Fetching memory for thread_id: {thread_id}")

        # Get user memory tool lazily
        user_memory_tool = self._get_user_memory_tool()
        if user_memory_tool is None:
            logger.warning("[Long-Term Memory] User memory tool not found")
            return {}

        result = await user_memory_tool.ainvoke({"user_id": thread_id})

        return result

    def _inject_long_term_memory(self, state, config=None):
        """Inject long-term memory as a SystemMessage.

        This hook:
        1. Extracts thread_id from config
        2. Checks if memory needs refresh (cache miss or upsert_user_memory was called)
        3. Fetches long-term memory data only if needed
        4. Formats it as a SystemMessage
        5. Inserts it after the system prompt but before conversation messages

        The memory SystemMessage is positioned after the system prompt (position 1)
        and will NOT be filtered by short-term memory filters since SystemMessages
        are always preserved.

        Performance optimization: Skips expensive HTTP call if cache is fresh.

        Args:
            state: Current state containing messages
            config: LangGraph configuration with thread_id

        Returns:
            dict: Updated state with memory SystemMessage injected
        """

        messages = state.get("messages", [])

        # Extract thread_id
        thread_id = None
        if config and isinstance(config, dict):
            thread_id = config.get("configurable", {}).get("thread_id")

        if not thread_id:
            logger.warning(
                "[Long-Term Memory] No thread_id found in config, skipping memory injection"
            )
            return {"messages": messages}

        try:
            # Check if we need to fetch memory
            current_time = datetime.now(timezone.utc)
            cache_ttl_seconds = 300  # 5 minutes cache TTL

            cached_entry = self._memory_cache.get(thread_id)
            cache_is_fresh = (
                cached_entry is not None
                and (current_time - cached_entry["timestamp"]).total_seconds()
                < cache_ttl_seconds
            )

            # Only fetch if cache is stale, missing, or refresh flag is set
            if not cache_is_fresh or self._memory_needs_refresh:
                logger.info(
                    f"[Long-Term Memory] Fetching memory (cache_fresh={cache_is_fresh}, needs_refresh={self._memory_needs_refresh})"
                )

                # Fetch memory data
                # Note: We need to run async function in sync context
                try:
                    # Try to get the current event loop
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # We're in an async context, create a task
                        # This is a workaround - ideally the hook should be async
                        logger.warning(
                            "[Long-Term Memory] Cannot fetch memory in running event loop, skipping"
                        )
                        memory_data = None
                    else:
                        memory_data = loop.run_until_complete(
                            self._fetch_long_term_memory(thread_id)
                        )
                except RuntimeError:
                    # No event loop, create one
                    memory_data = asyncio.run(self._fetch_long_term_memory(thread_id))

                # Update cache regardless of whether memory_data exists or not
                # This prevents repeated calls when there's no memory
                self._memory_cache[thread_id] = {
                    "data": memory_data if memory_data else {},
                    "timestamp": current_time,
                }
                self._memory_needs_refresh = False  # Clear refresh flag

                if memory_data:
                    logger.info("[Long-Term Memory] Cache updated with memory data")
                else:
                    logger.info(
                        "[Long-Term Memory] Cache updated with empty memory (no data available)"
                    )
            else:
                logger.info(
                    "[Long-Term Memory] Using cached memory (skipping HTTP call)"
                )
                memory_data = cached_entry["data"]

            # If no memory data, skip injection
            if not memory_data:
                logger.info(
                    "[Long-Term Memory] No memory data returned, skipping injection"
                )
                return {"messages": messages}

            # Format memory as SystemMessage
            memory_content = f"LONG-TERM MEMORY:\n{json.dumps(memory_data, indent=2, ensure_ascii=False)}"
            memory_message = SystemMessage(content=memory_content)

            # Find position to insert memory (after system prompt, before conversation)
            # Position 0: System prompt (if exists)
            # Position 1: Long-term memory (NEW)
            # Position 2+: Conversation messages
            insert_position = 0
            for i, msg in enumerate(messages):
                if isinstance(msg, SystemMessage):
                    # Check if this is a long-term memory message (to avoid duplicates)
                    if msg.content.startswith("LONG-TERM MEMORY:"):
                        # Replace existing memory message
                        messages[i] = memory_message
                        logger.info(
                            "[Long-Term Memory] Updated existing memory message"
                        )
                        return {"messages": messages}
                    insert_position = i + 1
                else:
                    # First non-system message found, insert here
                    break

            # Insert new memory message
            messages.insert(insert_position, memory_message)
            logger.info(
                f"[Long-Term Memory] Injected memory at position {insert_position}"
            )

        except Exception as e:
            logger.error(
                f"[Long-Term Memory] Error fetching/injecting memory: {e}",
                exc_info=True,
            )
            # Continue without memory on error

        return {"messages": messages}

    def _inject_thread_id_in_user_id_params(self, state, config=None):
        """Hook para injetar thread_id em qualquer parâmetro user_id de tool calls.

        Este hook processa todas as tool calls e substitui qualquer parâmetro
        'user_id' pelo thread_id atual, garantindo que todas as ferramentas
        recebam o identificador correto do usuário.

        Args:
            state: Estado do grafo contendo as mensagens
            config: Configuração do LangGraph (pode ser None em alguns contextos)

        Returns:
            dict: Estado atualizado com thread_id injetado em todos os parâmetros user_id
        """
        messages = state.get("messages", [])

        # Múltiplas formas de tentar obter o thread_id
        thread_id = None

        # Método 1: Diretamente do parâmetro config
        if config and isinstance(config, dict):
            configurable = config.get("configurable", {})
            thread_id = configurable.get("thread_id")

        # Método 2: Se config não foi passado, tenta do state (fallback)
        if not thread_id and hasattr(state, "config"):
            state_config = getattr(state, "config", {})
            if isinstance(state_config, dict):
                configurable = state_config.get("configurable", {})
                thread_id = configurable.get("thread_id")

        if thread_id:
            # Processa apenas a última mensagem AI que pode ter tool calls
            for message in reversed(messages):
                if hasattr(message, "tool_calls") and message.tool_calls:
                    for tool_call in message.tool_calls:
                        # Verifica se a tool call tem argumentos e se possui user_id
                        if (
                            "args" in tool_call
                            and isinstance(tool_call["args"], dict)
                            and "user_id" in tool_call["args"]
                        ):
                            # Substitui user_id pelo thread_id
                            tool_call["args"]["user_id"] = thread_id
                    break  # Processa apenas a última mensagem AI

        return {"messages": messages}

    def _combined_pre_model_hook(self, state, config=None):
        # Step 1: Add timestamps to new ToolMessages (safe update, modifies in-place)
        # We invoke this for the side-effect on state['messages'], relying on
        # _inject_long_term_memory or subsequent steps to return the messages list.
        self._add_timestamp_to_tool_messages(state)

        # Step 2: Inject long-term memory as SystemMessage
        state = self._inject_long_term_memory(state, config)

        # Step 3: Apply short-term memory filtering
        # This returns llm_input_messages which should NOT be overwritten
        filtered_state = self._filter_short_term_memory(state)

        # Step 4: Inject thread_id into tool calls
        # Need to work on llm_input_messages if it exists
        if "llm_input_messages" in filtered_state:
            # Create a temporary state with the filtered messages for thread_id injection
            temp_state = {"messages": filtered_state["llm_input_messages"]}
            thread_id_state = self._inject_thread_id_in_user_id_params(
                temp_state, config
            )
            # Return both the filtered messages for LLM and keep any other state updates
            return {
                "llm_input_messages": thread_id_state["messages"],
                **{
                    k: v for k, v in filtered_state.items() if k != "llm_input_messages"
                },
            }
        else:
            # No filtering applied, just inject thread_id normally
            return self._inject_thread_id_in_user_id_params(filtered_state, config)

    def _combined_post_model_hook(self, state, config=None):
        # Check if upsert_user_memory tool was called
        messages = state.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls"):
                for tool_call in msg.tool_calls:
                    if tool_call.get("name") == "upsert_user_memory":
                        self._memory_needs_refresh = True
                        logger.info(
                            "[Long-Term Memory] Detected upsert_user_memory call, flagging for refresh"
                        )
                        break
                break  # Only check the last AI message
        # Add timestamp to the new AIMessage (modifies in-place)
        self._add_timestamp_to_ai_message(state)

        return self._inject_thread_id_in_user_id_params(state, config)

    def _create_react_agent(self, checkpointer: Optional[PostgresSaver] = None):
        """Create and configure the React Agent."""
        llm = ChatVertexAI(
            model_name=self._model,
            temperature=self._temperature,
            include_thoughts=self._include_thoughts,
            thinking_budget=self._thinking_budget,
        )
        # llm_with_tools = llm.bind_tools(tools=self._tools, parallel_tool_calls=False)
        llm_with_tools = llm.bind_tools(tools=self._tools)

        self._graph = create_react_agent(
            model=llm_with_tools,
            tools=self._tools,
            prompt=self._system_prompt,
            checkpointer=checkpointer,
            pre_model_hook=self._combined_pre_model_hook,
            post_model_hook=self._combined_post_model_hook,
        )

    async def _ensure_async_setup(self):
        """Ensure async components are set up."""

        self._set_up_opentelemetry()

        if self._setup_complete_async:
            return
        engine = await PostgresEngine.afrom_instance(
            project_id=self._project_id,
            region=self._region,
            instance=self._instance_name,
            database=self._database_name,
            user=self._database_user,
            password=self._database_password,
            engine_args={"pool_pre_ping": True, "pool_recycle": 300},
        )
        checkpointer = await PostgresSaver.create(engine=engine)
        self._create_react_agent(checkpointer=checkpointer)
        self._setup_complete_async = True

    def _ensure_sync_setup(self):
        """Ensure sync components are set up."""

        self._set_up_opentelemetry()

        if self._setup_complete_sync:
            return self._graph
        engine = PostgresEngine.from_instance(
            project_id=self._project_id,
            region=self._region,
            instance=self._instance_name,
            database=self._database_name,
            user=self._database_user,
            password=self._database_password,
            engine_args={"pool_pre_ping": True, "pool_recycle": 300},
        )

        checkpointer = PostgresSaver.create_sync(engine=engine)
        self._create_react_agent(checkpointer=checkpointer)
        self._setup_complete_sync = True
        return self._graph

    async def async_query(self, **kwargs) -> dict[str, Any] | Any:
        """Asynchronous query execution with filtered current interaction."""
        kwargs = self._combined_pre_invoke_hook(**kwargs)
        await self._ensure_async_setup()
        if self._graph is None:
            raise ValueError(
                "Graph is not initialized. Call _ensure_async_setup first."
            )
        type = kwargs.pop("type", None)
        if type == "history":
            # Bypass filtering for history requests
            try:
                self._graph.update_state(
                    config=kwargs.get("config", {}), values=kwargs.get("input", {})
                )
                return {
                    "status_code": 200,
                    "status": "history updated",
                    "message": None,
                }
            except Exception as e:
                return {"status_code": 500, "status": "error", "message": str(e)}
        result = await self._graph.ainvoke(**kwargs)
        filtered_result = self._filter_current_interaction(result)

        # Simple tracing
        self._trace_conversation(filtered_result, **kwargs)

        return filtered_result

    async def async_stream_query(self, **kwargs) -> AsyncIterable[Any]:
        """Asynchronous streaming query execution with filtered chunks."""
        kwargs = self._combined_pre_invoke_hook(**kwargs)

        async def async_generator() -> AsyncIterable[Any]:
            await self._ensure_async_setup()
            if self._graph is None:
                raise ValueError(
                    "Graph is not initialized. Call _ensure_async_setup first."
                )
            async for chunk in self._graph.astream(**kwargs):
                filtered_chunk = self._filter_streaming_chunk(chunk)
                yield dumpd(filtered_chunk)

        return async_generator()

    def query(self, **kwargs) -> dict[str, Any] | Any:
        """Synchronous query execution with filtered current interaction."""
        kwargs = self._combined_pre_invoke_hook(**kwargs)
        self._ensure_sync_setup()
        if self._graph is None:
            raise ValueError("Graph is not initialized. Call _ensure_sync_setup first.")

        result = self._graph.invoke(**kwargs)
        filtered_result = self._filter_current_interaction(result)

        # Simple tracing
        self._trace_conversation(filtered_result, **kwargs)

        return filtered_result

    def stream_query(self, **kwargs) -> Iterator[dict[str, Any] | Any]:
        """Synchronous streaming query execution with filtered chunks."""
        kwargs = self._combined_pre_invoke_hook(**kwargs)
        self._ensure_sync_setup()
        if self._graph is None:
            raise ValueError("Graph is not initialized. Call _ensure_sync_setup first.")
        for chunk in self._graph.stream(**kwargs):
            filtered_chunk = self._filter_streaming_chunk(chunk)
            yield dumpd(filtered_chunk)

    def _filter_current_interaction(self, result: dict) -> dict:
        """Filters response to include only messages from the last human input."""
        if "messages" not in result or not isinstance(result["messages"], list):
            return result
        messages = result["messages"]
        last_human_index = -1
        for i, msg in reversed(list(enumerate(messages))):
            if isinstance(msg, HumanMessage):
                last_human_index = i
                break
        if last_human_index == -1:
            return result
        filtered_result = result.copy()
        filtered_result["messages"] = messages[last_human_index:]
        return filtered_result

    def _filter_streaming_chunk(self, chunk: dict) -> dict:
        """Applies interaction filter to a streaming chunk if applicable."""
        if isinstance(chunk, dict) and "messages" in chunk:
            return self._filter_current_interaction(chunk)
        return chunk
