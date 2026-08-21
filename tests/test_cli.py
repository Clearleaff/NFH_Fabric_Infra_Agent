from __future__ import annotations

import builtins
from types import SimpleNamespace

from nfh_instantiation_agent import cli


def test_chat_repl_reuses_session_and_prints_text(monkeypatch, capsys):
    calls = []

    class FakeService:
        def chat(self, session_id, message):
            calls.append((session_id, message))
            return SimpleNamespace(text=f"handled {message}")

    inputs = iter(["add this bap", "add this catalog to bpp2.local", "quit"])
    monkeypatch.setattr(cli, "InstantiationChatService", FakeService)
    monkeypatch.setattr(builtins, "input", lambda prompt: next(inputs))

    cli.chat_repl()

    assert len(calls) == 2
    assert calls[0][0] == calls[1][0]
    assert calls[0][1] == "add this bap"
    assert calls[1][1] == "add this catalog to bpp2.local"
    assert capsys.readouterr().out == "handled add this bap\nhandled add this catalog to bpp2.local\n"


def test_chat_repl_exits_on_eof(monkeypatch, capsys):
    class FakeService:
        def chat(self, session_id, message):
            raise AssertionError("chat should not be called")

    monkeypatch.setattr(cli, "InstantiationChatService", FakeService)
    monkeypatch.setattr(builtins, "input", lambda prompt: (_ for _ in ()).throw(EOFError))

    cli.chat_repl()

    assert capsys.readouterr().out == "\n"
