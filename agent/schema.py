"""The agent's report: what it *claims* it did. The verifier checks every field.

Strands enforces this shape on the model's final answer. Nothing here is
evidence; it is only the thing being checked.
"""

from pydantic import BaseModel, Field


class Signal(BaseModel):
    summary: str = Field(description="One sentence: a recent public signal about the company")
    source_url: str = Field(description="URL of the search result this signal came from")


class HardFact(BaseModel):
    label: str = Field(description="What the fact is, e.g. 'latest funding' or 'headcount'")
    value: str | None = Field(description="The value, or null if the search results did not contain it")
    source_url: str | None = Field(description="URL of the search result that states it, or null")


class Brief(BaseModel):
    company: str
    signals: list[Signal]
    hard_fact: HardFact
    outreach_angle: str = Field(description="One or two sentences: why reach out now, tied to the signals")


class AccountReport(BaseModel):
    briefs: list[Brief]
    companies_researched: list[str] = Field(description="Companies you actually researched with paid searches")
    paid_calls: int = Field(description="Number of searches you paid for")
    total_spent_usd: float = Field(description="Total USD you paid for searches")
    sources: list[str] = Field(description="Every URL cited in the briefs")
