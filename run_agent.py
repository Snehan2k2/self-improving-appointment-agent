"""Chat with the scheduling agent in the terminal.

    uv run python run_agent.py              # uses the active prompt version
    uv run python run_agent.py --version 1  # a specific prompt version
    uv run python run_agent.py --quiet      # hide tool calls
    uv run python run_agent.py --show-prompt  # print the full system prompt the agent gets

Each chat starts from a fresh copy of data/clinic.json. Type 'quit' to exit.
The transcript, tool calls and final appointments are saved to runs/chats/.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from clinic.agent import Agent
from clinic.llm import LLMError, OpenRouterLLM

GREY, RESET = "\033[90m", "\033[0m"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", type=int, default=None, help="prompt version (default: prompts/active.txt)")
    ap.add_argument("--quiet", action="store_true", help="hide tool calls")
    ap.add_argument("--show-prompt", action="store_true", help="print the full system prompt and exit")
    args = ap.parse_args()

    if args.show_prompt:
        from clinic.db import ClinicDB
        from clinic.prompt import build_system_prompt
        print(build_system_prompt(ClinicDB(), args.version))
        return

    try:
        llm = OpenRouterLLM()
    except LLMError as e:
        raise SystemExit(str(e))
    agent = Agent(llm, prompt_version=args.version)
    print(f"Sanjeevani Clinic assistant (prompt v{agent.prompt_version}, model {llm.model}). Type 'quit' to exit.\n")

    try:
        while True:
            text = input("You: ").strip()
            if text.lower() in {"quit", "exit"}:
                break
            if not text:
                continue
            shown = len(agent.trace)
            try:
                reply = agent.respond(text)
            except LLMError as e:
                print(f"[model error] {e}")
                break
            if not args.quiet:
                for t in agent.trace[shown:]:
                    print(f"{GREY}  -> {t['tool']}({json.dumps(t['args'])})\n"
                          f"     {json.dumps(t['result'])[:300]}{RESET}")
            print(f"Agent: {reply}\n")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        save(agent, llm)


def save(agent: Agent, llm: OpenRouterLLM):
    out_dir = Path("runs/chats")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"chat-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({
        "prompt_version": agent.prompt_version,
        "messages": agent.messages,
        "trace": agent.trace,
        "llm_calls": llm.calls,
        "db_changes": agent.tools.db.changes(),
    }, indent=2))
    print(f"saved {path}")


if __name__ == "__main__":
    main()
