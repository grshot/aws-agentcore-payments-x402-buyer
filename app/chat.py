"""App layer for the chat: one conversation = one payment session.

Same split as app/orchestrator.py. App code creates the session with its cap,
owns the tool log, reads the session back from AgentCore and runs the verifier.
The model only chats and calls `search`. Telling it "the budget was raised"
changes nothing: the cap lives in the session, not in the conversation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from bedrock_agentcore.payments import PaymentManager

from agent.agent import build_chat_agent, chat_report
from agent.tools import SearchConfig, log_as_dicts, make_search_tool
from app import orchestrator
from app.verifier import check_reply, gate, verify

Emit = Callable[[dict[str, Any]], None]


class Chat:
    def __init__(self, env_file: str, budget_usd: str):
        s = orchestrator.settings(env_file)
        self.settings = s
        self.chat_id = "chat-" + uuid.uuid4().hex[:8]
        self.started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.cap = orchestrator.session_budget(budget_usd, s)  # never above the operator ceiling
        self.budget_usd = self.cap["budget_usd"]
        self.manager = PaymentManager(payment_manager_arn=s["manager_arn"], region_name=s["region"],
                                      agent_name=orchestrator.AGENT_NAME)
        self.session_id = orchestrator.create_session(self.manager, s["user_id"], self.budget_usd, int(s["chat_expiry_minutes"]))
        self.emit: Emit = lambda event: None  # set per message, so events reach the current request
        self.config = SearchConfig(
            manager=self.manager,
            user_id=s["user_id"],
            instrument_id=s["instrument_id"],
            session_id=self.session_id,
            endpoint=s["endpoint"],
            network=orchestrator.NETWORKS[s["network"]],
            max_price_usd=Decimal(s["max_price_usd"]),
            on_event=self._on_call,
        )
        self.agent = build_chat_agent(make_search_tool(self.config), s["model_id"], s["region"])

    def info(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "budget_usd": self.budget_usd,
            "cap": self.cap,
            "network": self.settings["network"],
            "explorer": orchestrator.EXPLORERS[self.settings["network"]],
            "session_record": self.session_record(),
        }

    def session_record(self) -> dict[str, Any]:
        return orchestrator.get_session(self.manager, self.settings["user_id"], self.session_id)

    def _on_call(self, entry: dict[str, Any]) -> None:
        self.emit({"type": "call", "entry": entry})
        if entry["stage"] == "done":
            self.emit({"type": "session_record", "record": self.session_record()})

    def send(self, text: str, emit: Emit) -> str:
        """One user message. Tool calls stream through `emit`; returns the agent's reply."""
        self.emit = emit
        try:
            return str(self.agent(text)).strip()
        finally:
            self.emit = lambda event: None

    def check(self, reply: str) -> dict[str, Any]:
        """Are the reply's sources backed by this chat's paid searches? (Plain code, no model.)"""
        return check_reply(reply, log_as_dicts(self.config.log))

    def verify(self) -> dict[str, Any]:
        """Ask for the agent's report and check it against the records. Saved like a run."""
        claim = chat_report(self.agent, self.settings["model_id"], self.settings["region"]).model_dump()
        tool_log = log_as_dicts(self.config.log)
        record = self.session_record()
        report = verify(claim, tool_log, record)
        result = {
            "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "network": self.settings["network"],
            "explorer": orchestrator.EXPLORERS[self.settings["network"]],
            "model_id": self.settings["model_id"],
            "prompt_variant": "chat",
            "companies": sorted({e["company"] for e in tool_log}),
            "offering": "",
            "budget_usd": self.budget_usd,
            "cap": self.cap,
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "claim": claim,
            "tool_log": tool_log,
            "session_record": record,
            "verification": report.to_dict(),
            "governed": gate(claim, report, governed=True),
        }
        orchestrator.save(result)
        return result
