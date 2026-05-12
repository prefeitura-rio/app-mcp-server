"""
CLI do agente local pra uso headless (sem TTY).

Diferenças vs interactive_test.py:
  - Não constrói remote_agent (não exige permissão IAM no Reasoning Engine).
  - Não usa input() — recebe mensagem(s) como arg, stdin ou arquivo.
  - Imprime só a resposta final do agente, salvo --verbose.
  - Suporta --json (saída machine-readable) e --script (multi-turn).

Uso (one-shot):
  uv run src/utils/agent/headless_test.py "qual é a previsão do tempo no Rio?"
  echo "..." | uv run src/utils/agent/headless_test.py --stdin
  uv run src/utils/agent/headless_test.py --verbose "..."
  uv run src/utils/agent/headless_test.py --json "..."

Uso (multi-turn — necessário pra workflows como poda):
  uv run src/utils/agent/headless_test.py --script turns.txt
  # turns.txt: uma mensagem por linha; linhas começando com # são ignoradas.
  # O mesmo Agent in-process é usado em todos os turns (InMemorySaver implícito
  # preserva histórico de conversa, que é o que workflows multi-step precisam).
"""

import argparse
import asyncio
import contextlib
import json
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langchain.load.dump import dumpd

from src.utils.agent.prompt import prompt_data  # transitively loads src.config.env
from src.utils.agent.tools import mcp_tools

from engine.agent import Agent


def _build_agent() -> Agent:
    return Agent(
        model="gemini-2.5-flash",
        system_prompt=prompt_data["prompt"],
        temperature=0.7,
        tools=mcp_tools,
        include_thoughts=True,
        thinking_budget=-1,
        otpl_service=f"eai-langgraph-v{prompt_data['version']}",
        use_checkpointer=False,  # → InMemorySaver in-process (engine/agent.py:858)
    )


def _msg_attr(msg, key, default=None):
    """Aceita tanto dict (resposta do Reasoning Engine remoto) quanto
    objeto LangChain (resposta do agente local)."""
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


def _msg_type(msg) -> str:
    if isinstance(msg, dict):
        return msg.get("type", "")
    cls = msg.__class__.__name__.lower()
    if "ai" in cls:
        return "ai"
    if "human" in cls:
        return "human"
    if "tool" in cls:
        return "tool"
    return cls


def _flatten_content(content) -> str:
    """Gemini com include_thoughts retorna content como lista mista:
    [{'type': 'thinking', 'thinking': '...'}, 'resposta final'] ou
    [{'type': 'text', 'text': '...'}]. Extrai só o texto visível."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for chunk in content:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                if chunk.get("type") == "thinking":
                    continue
                text = chunk.get("text") or chunk.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(p for p in parts if p)
    return str(content)


def _final_text(result) -> str:
    """Última AIMessage sem tool_calls → resposta final do agente."""
    messages = result.get("messages", []) if isinstance(result, dict) else []
    for msg in reversed(messages):
        if _msg_type(msg) != "ai":
            continue
        if _msg_attr(msg, "tool_calls"):
            continue
        text = _flatten_content(_msg_attr(msg, "content", ""))
        if text:
            return text
    return "(sem resposta textual do agente)"


def _last_tool_call(result) -> str:
    messages = result.get("messages", []) if isinstance(result, dict) else []
    for msg in reversed(messages):
        if _msg_type(msg) != "ai":
            continue
        tcs = _msg_attr(msg, "tool_calls") or []
        if not tcs:
            continue
        tc = tcs[0]
        name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "?")
        args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
        payload = json.dumps(args, ensure_ascii=False)
        return f"{name}({payload[:200]}{'…' if len(payload) > 200 else ''})"
    return "(nenhuma tool chamada)"


def _verbose_dump(result) -> None:
    messages = result.get("messages", []) if isinstance(result, dict) else []
    for i, msg in enumerate(messages):
        kind = _msg_type(msg)
        content = _msg_attr(msg, "content", "")
        if kind == "ai":
            tool_calls = _msg_attr(msg, "tool_calls") or []
            print(f"\n[{i}] AI")
            for tc in tool_calls:
                name = (
                    tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "?")
                )
                args = (
                    tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                )
                print(
                    f"    → tool_call: {name}({json.dumps(args, ensure_ascii=False)})"
                )
            if isinstance(content, list):
                # Mostra thinking + final text separadamente — README promete "trace
                # passo-a-passo (tool calls + thinking)", então não filtrar.
                for chunk in content:
                    if isinstance(chunk, dict) and chunk.get("type") == "thinking":
                        thinking = chunk.get("thinking", "")
                        if thinking:
                            print(f"    💭 thinking: {thinking}")
                    elif isinstance(chunk, str):
                        if chunk.strip():
                            print(f"    {chunk}")
                    elif isinstance(chunk, dict):
                        text = chunk.get("text") or chunk.get("content")
                        if text:
                            print(f"    {text}")
            elif content:
                print(f"    {content}")
        elif kind == "tool":
            name = _msg_attr(msg, "name", "?")
            status = _msg_attr(msg, "status", "?")
            print(f"\n[{i}] TOOL {name} ({status})")
            print(f"    {content}")
        elif kind == "human":
            print(f"\n[{i}] HUMAN")
            print(f"    {content}")


async def _query(agent: Agent, message: str, thread_id: str) -> dict:
    return await agent.async_query(
        input={"messages": [{"role": "human", "content": message}]},
        config={"configurable": {"thread_id": thread_id}},
        type=None,
    )


def _read_script(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def _format_one_shot(
    result: dict, args: argparse.Namespace, thread_id: str, elapsed: float
) -> None:
    if args.json:
        # dumpd serializa objetos LangChain (BaseMessage, ToolCall, etc) em
        # dicts navegáveis via jq (`.messages[].content`, `.tool_calls`, etc).
        print(json.dumps(dumpd(result), ensure_ascii=False, indent=2))
    elif args.verbose:
        _verbose_dump(result)
        print(f"\n--- thread_id={thread_id} | {elapsed:.2f}s ---")
    else:
        print(_final_text(result))


async def _run_script(
    agent: Agent, turns: list[str], thread_id: str, args: argparse.Namespace
) -> int:
    print(f"=== SCRIPT | thread_id={thread_id} | {len(turns)} turns ===\n")
    failed = False
    for i, turn in enumerate(turns, 1):
        print(f"────── TURN {i} ──────")
        print(f"👤 {turn}")
        t0 = time.time()
        try:
            result = await _query(agent, turn, thread_id)
        except Exception as e:
            print(f"❌ erro no turn {i}: {type(e).__name__}: {e}")
            if args.verbose:
                traceback.print_exc()
            failed = True
            break
        elapsed = time.time() - t0
        if args.verbose:
            _verbose_dump(result)
        else:
            print(f"🤖 {_final_text(result)}")
        print(f"   ⏱ {elapsed:.1f}s | last tool: {_last_tool_call(result)}\n")
    return 1 if failed else 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CLI headless do agente EAI local.")
    p.add_argument(
        "message",
        nargs="?",
        help="Mensagem one-shot (omita pra usar --stdin ou --script).",
    )
    p.add_argument(
        "--stdin", action="store_true", help="Lê uma única mensagem de stdin."
    )
    p.add_argument(
        "--script",
        type=Path,
        default=None,
        help="Arquivo com uma mensagem por linha; roda multi-turn no mesmo Agent.",
    )
    p.add_argument(
        "--thread-id", default=None, help="Thread id custom (default: UUID novo)."
    )
    p.add_argument(
        "--json", action="store_true", help="One-shot only: result dict em JSON."
    )
    p.add_argument(
        "--verbose", action="store_true", help="Imprime trace passo-a-passo."
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    thread_id = args.thread_id or str(uuid.uuid4())

    if args.script:
        if not args.script.exists():
            print(f"erro: script {args.script} não existe", file=sys.stderr)
            return 2
        turns = _read_script(args.script)
        if not turns:
            print(
                f"erro: script {args.script} vazio (sem linhas não-comentadas)",
                file=sys.stderr,
            )
            return 2
        if args.json:
            print("erro: --json não é suportado com --script", file=sys.stderr)
            return 2
        agent = _build_agent()
        return asyncio.run(_run_script(agent, turns, thread_id, args))

    # one-shot
    if args.stdin:
        message = sys.stdin.read().strip()
    elif args.message:
        message = args.message.strip()
    else:
        print(
            "erro: forneça uma mensagem (arg posicional, --stdin ou --script)",
            file=sys.stderr,
        )
        return 2
    if not message:
        print("erro: mensagem vazia", file=sys.stderr)
        return 2

    agent = _build_agent()
    started = datetime.now(timezone.utc)
    # Em --json o pipeline esperado é `... --json | jq .`. Tools que printam
    # pra stdout (ex: IPTUAPIService.__init__) contaminariam a saída JSON,
    # então redirecionamos stdout pra stderr durante a query — só o
    # `json.dumps` final vai pra stdout limpo.
    if args.json:
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(_query(agent, message, thread_id))
    else:
        result = asyncio.run(_query(agent, message, thread_id))
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    _format_one_shot(result, args, thread_id, elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
