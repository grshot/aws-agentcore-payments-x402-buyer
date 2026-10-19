"""The GTM account research agent: a Strands agent with exactly one tool, `search`.

Given target accounts and what we sell, it finds recent public signals per
company and writes an account brief with an outreach angle.

It can't create payment sessions, read the wallet or the session budget, or
see the tool log. All of that is app code (see app/orchestrator.py).
"""

import copy

from strands import Agent
from strands.models import BedrockModel
from strands.tools.executors import SequentialToolExecutor

from agent.prompts import CHAT, PROMPTS, REPORT_REQUEST, task_message
from agent.schema import AccountReport


def build_agent(search_tool, model_id: str, region: str, prompt_variant: str = "neutral") -> Agent:
    return Agent(
        name="GTM Account Research Agent",
        description="Researches target accounts with a paid search and writes outreach briefs.",
        model=BedrockModel(model_id=model_id, region_name=region),
        system_prompt=PROMPTS[prompt_variant],
        tools=[search_tool],                        # the only tool
        structured_output_model=AccountReport,      # final answer must match the schema
        tool_executor=SequentialToolExecutor(),     # one paid call at a time, in order
        callback_handler=None,                      # no streaming to stdout; the app prints
    )


def research(agent: Agent, companies: list[str], offering: str) -> AccountReport:
    """Run the task and return the agent's structured report (its claim)."""
    result = agent(task_message(companies, offering))
    return result.structured_output


# ----------------------------------------------------------------------------
# Chat: the same agent and tool, one conversation per payment session
# ----------------------------------------------------------------------------


def build_chat_agent(search_tool, model_id: str, region: str) -> Agent:
    """The chat agent. Its conversation lives in this process only (no AgentCore Memory)."""
    return Agent(
        name="GTM Account Research Agent",
        description="Chats about target accounts, researches them with a paid search, suggests outreach.",
        model=BedrockModel(model_id=model_id, region_name=region),
        system_prompt=CHAT,
        tools=[search_tool],                        # the only tool
        tool_executor=SequentialToolExecutor(),
        callback_handler=None,
    )


def chat_report(chat_agent: Agent, model_id: str, region: str) -> AccountReport:
    """The agent's structured report of the chat so far (its claim).

    Written by a copy of the agent with the same conversation but no tools, so
    asking for a report can't trigger another paid search, and the request
    doesn't become part of the chat.
    """
    reporter = Agent(
        model=BedrockModel(model_id=model_id, region_name=region),
        system_prompt=CHAT,
        messages=copy.deepcopy(chat_agent.messages),
        structured_output_model=AccountReport,
        callback_handler=None,
    )
    return reporter(REPORT_REQUEST).structured_output
