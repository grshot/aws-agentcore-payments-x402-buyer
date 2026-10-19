"""Local web viewer for the demo: FastAPI serving one HTML page.

    uv run uvicorn ui.server:app --port 8000      # then open http://localhost:8000

The page starts runs or a chat and watches them live. Everything it shows
comes from app/orchestrator.py and app/chat.py: the tool log, the AgentCore
session record and the verifier. Nothing here talks to the model directly.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import orchestrator
from app.chat import Chat

ROOT = Path(__file__).resolve().parent.parent
ENV_FILES = [".env", ".env.testnet", ".env.mainnet"]  # whichever exist; one per network

# Declines are expected (that's the cap working); the tool log records them.
logging.getLogger("bedrock_agentcore.payments").setLevel(logging.CRITICAL)

app = FastAPI(title="AgentCore Payments demo")

# One paid action at a time (a run or a chat message): a double click must not
# start a second paid run.
run_lock = threading.Lock()

# The chat lives in this process only (no AgentCore Memory). One at a time.
chat: Chat | None = None


class RunRequest(BaseModel):
    companies: list[str] = Field(min_length=1, max_length=10)
    offering: str = Field(min_length=1, max_length=200)
    prompt: str = Field(pattern="^(neutral|pressure)$")
    budget_usd: str = Field(pattern=r"^\d+(\.\d+)?$")
    env: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/envs")
def envs() -> list[dict[str, str | None]]:
    """Settings files that exist, with their network. No other values leave the server."""
    found = []
    for name in ENV_FILES:
        if (ROOT / name).exists():
            s = orchestrator.settings(str(ROOT / name))
            found.append({"env": name, "network": s["network"], "budget_usd": s["budget_usd"],
                          "ceiling_usd": orchestrator.operator_ceiling(s)})
    return found


@app.get("/api/wallet")
def wallet(env: str) -> dict[str, Any]:
    return orchestrator.wallet(env_path(env))


@app.get("/api/runs")
def runs() -> list[dict[str, Any]]:
    return orchestrator.saved_runs()


@app.get("/api/runs/{run_id}")
def saved_run(run_id: str) -> dict[str, Any]:
    try:
        return orchestrator.load_run(run_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "no such run")


@app.post("/api/run")
def start_run(req: RunRequest) -> StreamingResponse:
    """Run the agent and stream its events as newline-delimited JSON."""
    env = env_path(req.env)
    return stream_work(lambda emit: orchestrator.run(
        req.companies, req.offering, req.prompt, req.budget_usd, env, on_event=emit))


class ChatStart(BaseModel):
    env: str
    budget_usd: str = Field(pattern=r"^\d+(\.\d+)?$")


class ChatMessage(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@app.post("/api/chat")
def start_chat(req: ChatStart) -> dict[str, Any]:
    """New chat, new payment session with its own cap (created here, by app code)."""
    global chat
    env = env_path(req.env)
    with paid_action():
        chat = Chat(env, req.budget_usd)
        return chat.info()


@app.post("/api/chat/{chat_id}/message")
def chat_message(chat_id: str, req: ChatMessage) -> StreamingResponse:
    """One message. Streams tool calls as they happen, then the agent's reply."""
    current = current_chat(chat_id)

    def work(emit) -> None:
        reply = current.send(req.text, emit)
        emit({"type": "reply", "text": reply, "check": current.check(reply)})

    return stream_work(work)


@app.post("/api/chat/{chat_id}/verify")
def chat_verify(chat_id: str) -> dict[str, Any]:
    current = current_chat(chat_id)
    with paid_action():
        return current.verify()


def current_chat(chat_id: str) -> Chat:
    if chat is None or chat.chat_id != chat_id:
        raise HTTPException(404, "this chat has ended; start a new one")
    return chat


class paid_action:
    """`with paid_action():` holds the one-at-a-time lock, or answers 409."""

    def __enter__(self):
        if not run_lock.acquire(blocking=False):
            raise HTTPException(409, "a run or chat message is already in progress")

    def __exit__(self, *exc):
        run_lock.release()


def stream_work(work) -> StreamingResponse:
    """Run `work(emit)` in a thread and stream its events as newline-delimited JSON."""
    if not run_lock.acquire(blocking=False):
        raise HTTPException(409, "a run or chat message is already in progress")
    events: queue.Queue = queue.Queue()

    def target() -> None:
        try:
            work(events.put)
        except Exception as e:  # show it on the page instead of a silent stop
            events.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            events.put(None)
            run_lock.release()

    threading.Thread(target=target, daemon=True).start()

    def stream() -> Iterator[str]:
        while (event := events.get()) is not None:
            yield json.dumps(event, default=str) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def env_path(env: str) -> str:
    if env not in ENV_FILES or not (ROOT / env).exists():
        raise HTTPException(400, "unknown settings file")
    return str(ROOT / env)
