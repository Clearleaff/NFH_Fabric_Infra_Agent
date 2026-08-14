from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from .agent import InstantiationAgent
from .models import AgentError
from .service import InstantiationChatService


def chat_repl() -> None:
    service = InstantiationChatService()
    session_id = uuid.uuid4().hex
    while True:
        try:
            message = input("> ")
        except EOFError:
            print()
            break
        clean = message.strip()
        if clean.lower() in {"exit", "quit"}:
            break
        if not clean:
            continue
        try:
            response = service.chat(session_id, clean)
        except AgentError as exc:
            print(f"Error: {exc}")
            continue
        except Exception as exc:
            print(f"Error: {exc}")
            continue
        print(response.text)


def main() -> None:
    parser = argparse.ArgumentParser(prog="instantiation-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chat")
    rb = sub.add_parser("rollback")
    rb.add_argument("--state-db", required=True)
    rb.add_argument("--release-id", required=True)
    args = parser.parse_args()
    if args.cmd == "chat":
        chat_repl()
    elif args.cmd == "rollback":
        agent = InstantiationAgent(state_db=Path(args.state_db))
        print(json.dumps(agent.rollback(args.release_id), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
