import traceback
import asyncio
import json
import logging
from sys import argv
from datetime import datetime, timezone

import vertexai
from vertexai import agent_engines

# Configure logging to show INFO level messages
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from src.config import env
from src.utils.agent.prompt import prompt_data
from src.utils.agent.utils import gerar_conversa_aleatoria
from src.utils.agent.tools import mcp_tools

from engine.agent import Agent

import uuid

print(f"mcp_tools len: {len(mcp_tools)}")

vertexai.init(
    project=env.PROJECT_ID,
    location=env.LOCATION,
    staging_bucket=env.GCS_BUCKET,
)


def get_agent():
    return agent_engines.get(
        f"projects/{env.PROJECT_NUMBER}/locations/{env.LOCATION}/reasoningEngines/{env.REASONING_ENGINE_ID}"
    )


lista_de_mensagens = gerar_conversa_aleatoria(num_mensagens=10, tamanho_content=100)

user_id = str(uuid.uuid4())  # Unique user ID for the session


# Initialize agents
remote_agent = get_agent()
prompt_version = prompt_data["version"]
local_agent = Agent(
    model="gemini-2.5-flash",
    system_prompt=prompt_data["prompt"],
    temperature=0.7,
    tools=mcp_tools,
    include_thoughts=True,
    thinking_budget=-1,
    otpl_service=f"eai-langgraph-v{prompt_version}",
)


def parse_agent_response(response, is_local=False, start_time=None):
    """Parse the agent response and show all steps"""
    print("\n" + "=" * 60)
    print("🤖 AGENT EXECUTION STEPS")
    print("=" * 60)

    if is_local:
        # Local agent returns LangChain message objects directly
        messages = response.get("messages", [])

        previous_timestamp = None
        total_execution_time = None

        # Calcular tempo total se start_time foi fornecido
        if start_time and messages:
            # Pegar timestamp da última mensagem
            last_message = messages[-1]
            last_timestamp_str = getattr(last_message, "additional_kwargs", {}).get(
                "timestamp"
            )
            if last_timestamp_str and last_timestamp_str != "No timestamp":
                try:
                    last_timestamp = datetime.fromisoformat(
                        last_timestamp_str.replace("Z", "+00:00")
                    )
                    total_execution_time = (last_timestamp - start_time).total_seconds()
                except:
                    pass

        for i, message in enumerate(messages):
            msg_type = message.__class__.__name__

            # Extrair timestamp do additional_kwargs se existir
            timestamp_str = getattr(message, "additional_kwargs", {}).get(
                "timestamp", "No timestamp"
            )

            # Calcular tempo desde a mensagem anterior
            time_since_last = None
            if timestamp_str != "No timestamp":
                try:
                    current_timestamp = datetime.fromisoformat(
                        timestamp_str.replace("Z", "+00:00")
                    )
                    if previous_timestamp:
                        time_since_last = (
                            current_timestamp - previous_timestamp
                        ).total_seconds()
                    previous_timestamp = current_timestamp
                except:
                    pass

            if "HumanMessage" in msg_type:
                print(f"\n👤 USER MESSAGE #{i+1}:")
                print(f"   ⏰ Timestamp: {timestamp_str}")
                if time_since_last:
                    print(f"   ⏱️  Time since last: {time_since_last:.3f}s")
                print(f"   {message.content}")

            elif "AIMessage" in msg_type:
                print(f"\n🤖 AI RESPONSE #{i+1}:")
                print(f"   ⏰ Timestamp: {timestamp_str}")
                if time_since_last:
                    print(f"   ⏱️  Time since last: {time_since_last:.3f}s")

                # Check for tool calls
                tool_calls = getattr(message, "tool_calls", [])
                if tool_calls:
                    print("   🔧 TOOL CALLS:")
                    for tool_call in tool_calls:
                        tool_name = tool_call.get("name", "unknown")
                        tool_args = tool_call.get("args", {})
                        print(f"      📞 Calling: {tool_name}")
                        print(f"      📋 Arguments: {json.dumps(tool_args, indent=8)}")

                # Show AI content if any
                thinking_content = ""
                final_content = ""
                if message.content:
                    if isinstance(message.content, list):
                        for msg in message.content:
                            if isinstance(msg, dict):
                                if msg.get("type") == "thinking":
                                    thinking_content += msg.get("thinking", "")
                                elif msg.get("type") == "text":
                                    final_content += msg.get("text", "")
                            else:
                                final_content += msg
                    else:
                        final_content = message.content

                if thinking_content.strip == "":
                    print(f"   💬 Response: {final_content}")
                else:
                    print(f"   📝 Thinking: {thinking_content}")
                    print(f"   💬 Response: {final_content}")

                # Show usage metadata
                usage = getattr(message, "usage_metadata", {})
                if usage:
                    total_tokens = usage.get("total_tokens", 0)
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    print(
                        f"   📊 Tokens: {input_tokens} in, {output_tokens} out, {total_tokens} total"
                    )

            elif "ToolMessage" in msg_type:
                print(f"\n🔧 TOOL RESPONSE #{i+1}:")
                tool_name = getattr(message, "name", "unknown")
                tool_content = message.content
                print(f"   ⏰ Timestamp: {timestamp_str}")
                if time_since_last:
                    print(f"   ⏱️  Time since last: {time_since_last:.3f}s")
                print(f"   🛠️  Tool: {tool_name}")
                print(f"   📄 Response: {tool_content}")

        # Mostrar tempo total no final
        if total_execution_time:
            print(f"\n📈 EXECUTION SUMMARY:")
            print(f"   🎯 Total execution time: {total_execution_time:.3f}s")
            if start_time:
                actual_wall_time = (
                    datetime.now(timezone.utc) - start_time
                ).total_seconds()
                print(f"   🕐 Actual wall clock time: {actual_wall_time:.3f}s")
                print(
                    f"   📊 Efficiency: {(total_execution_time/actual_wall_time*100):.1f}% (message timestamps vs wall clock)"
                )
    else:
        # Remote agent returns direct message objects
        if "messages" not in response:
            print("❌ Unexpected response format")
            return

        messages = response["messages"]

        for i, message in enumerate(messages):
            msg_type = message.get("type", "unknown")
            content = message.get("content", "")

            if msg_type == "human":
                print(f"\n👤 USER MESSAGE #{i+1}:")
                print(f"   {content}")

            elif msg_type == "ai":
                print(f"\n🤖 AI RESPONSE #{i+1}:")

                # Check for tool calls
                tool_calls = message.get("tool_calls", [])
                if tool_calls:
                    print("   🔧 TOOL CALLS:")
                    for tool_call in tool_calls:
                        tool_name = tool_call.get("name", "unknown")
                        tool_args = tool_call.get("args", {})
                        print(f"      📞 Calling: {tool_name}")
                        print(f"      📋 Arguments: {json.dumps(tool_args, indent=8)}")

                # Show AI content if any
                # Show AI content if any
                thinking_content = ""
                final_content = ""
                if content:
                    if isinstance(content, list):
                        for msg in content:
                            if isinstance(msg, dict):
                                if msg.get("type") == "thinking":
                                    thinking_content += msg.get("thinking", "")
                                elif msg.get("type") == "text":
                                    final_content += msg.get("text", "")
                            else:
                                final_content += msg
                    else:
                        final_content = content

                if thinking_content.strip == "":
                    print(f"   💬 Response: {final_content}")
                else:
                    print(f"   📝 Thinking: {thinking_content}")
                    print(f"   💬 Response: {final_content}")

                # Show usage metadata
                usage = message.get("usage_metadata", {})
                if usage:
                    total_tokens = usage.get("total_tokens", 0)
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    print(
                        f"   📊 Tokens: {input_tokens} in, {output_tokens} out, {total_tokens} total"
                    )

            elif msg_type == "tool":
                print(f"\n🔧 TOOL RESPONSE #{i+1}:")
                tool_name = message.get("name", "unknown")
                tool_content = message.get("content", "")
                tool_status = message.get("status", "unknown")

                print(f"   🛠️  Tool: {tool_name}")
                print(f"   📊 Status: {tool_status}")
                print(f"   📄 Response: {tool_content}")


async def interactive_chat(use_local=False):
    """Start an interactive chat session."""
    agent = local_agent if use_local else remote_agent
    agent_name = "Local Agent" if use_local else "Remote Agent"

    print(f"🤖 EAI {agent_name} Interactive Chat - user_id: {user_id}")
    print("=" * 60)
    print("Type 'quit' to exit, 'help' for commands")
    print()

    while True:
        try:
            user_input = input("\n👤 You: ").strip()

            if user_input.lower() == "quit":
                print("👋 Goodbye!")
                break
            elif user_input.lower() == "help":
                print("\n📋 Available commands:")
                print("  - Type your message to chat with the agent")
                print("  - '!ai <message>' to inject a message as if the agent sent it")
                print("  - 'quit' to exit")
                print("  - 'help' to show this help")
                print("  - 'clear' to clear the screen")
                continue
            elif user_input.lower() == "clear":
                print("\n" * 50)
                continue
            elif not user_input:
                continue

            type = None
            # Check if user wants to send a message as AI
            if user_input.startswith("!ai "):
                user_input = user_input.replace("!ai ", "", 1).strip()
                data = {
                    "messages": [{"role": "ai", "content": user_input}],
                }
                type = "history"
            elif user_input.startswith("!fake"):
                # For testing: send a batch of fake messages
                data = {
                    "messages": lista_de_mensagens,
                }
                type = "history"
                print(f"\n🔄 Processing batch of {len(lista_de_mensagens)} messages...")
            else:
                print(f"\n🔄 Processing: {user_input}")
                data = {
                    "messages": [{"role": "human", "content": user_input}],
                }

            config = {"configurable": {"thread_id": user_id}}
            try:
                # Capturar tempo de início
                start_time = datetime.now(timezone.utc)

                # Use async_query for both agents
                if use_local:
                    result = await local_agent.async_query(
                        input=data, config=config, type=type
                    )
                else:
                    result = await remote_agent.async_query(
                        input=data, config=config, type=type
                    )
                # print(result)
                # Parse and display the result

                if type == "history":
                    print("\n✅ History updated successfully.")
                    print(result)
                else:
                    parse_agent_response(
                        result, is_local=use_local, start_time=start_time
                    )

            except Exception as e:
                print(f"\n❌ Error: {str(e)}")
                traceback.print_exc()

        except KeyboardInterrupt:
            print("\n\n👋 Interrupted by user. Goodbye!")
            break
        except Exception as e:
            print(f"\n❌ Unexpected error: {str(e)}")


if __name__ == "__main__":
    use_local = len(argv) > 1 and argv[1] == "local"
    asyncio.run(interactive_chat(use_local=use_local))
