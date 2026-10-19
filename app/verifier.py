"""Verifier: check what the agent *claims* against what the system *recorded*.

This is the reusable lesson of the demo. The agent writes a report ("I paid for
5 searches, here are 5 briefs with sources"). Nothing in that report counts as
evidence, because the same model that did the work also wrote the report.

Evidence comes only from two records the model cannot write to:

- the **tool log**: one entry per search call, written by application code
  inside the search tool (query, payment outcome, amount, returned URLs);
- the **AgentCore session record**: what AgentCore Payments says was spent in
  the budget-capped payment session.

The verifier is a pure function: no network calls, no framework imports. That
keeps it easy to read, easy to test, and portable to any agent stack.

Input shapes
------------
claim (the agent's structured output)::

    {
      "briefs": [{
        "company": "Acme",
        "signals": [{"summary": "...", "source_url": "https://..."}],
        "hard_fact": {"label": "headcount", "value": "1200" | None,
                      "source_url": "https://..." | None},
        "outreach_angle": "..."
      }],
      "companies_researched": ["Acme"],
      "paid_calls": 1,
      "total_spent_usd": 0.007,
      "sources": ["https://..."]
    }

tool_log (one entry per search call)::

    {"call_id": "c1", "company": "Acme", "query": "...", "http_status": 200,
     "payment_outcome": "settled" | "declined_cap" | "declined_policy" | "error",
     "amount_usd": "0.007", "result_urls": ["https://..."]}

session_record (from AgentCore ``get_payment_session``)::

    {"limits": {"maxSpendAmount": {"value": "0.015", "currency": "USD"}},
     "availableLimits": {"availableSpendAmount": {"value": "0.001", "currency": "USD"}},
     ...}

AgentCore reports the cap and what is still available, not a spent total, so
spent = cap - available.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# Amounts are compared as Decimal; this absorbs rounding in how the agent
# writes a number (0.014 vs 0.0140000001), not real differences in spend.
SPEND_TOLERANCE_USD = Decimal("0.0001")

SETTLED = "settled"


@dataclass
class Check:
    name: str
    passed: bool
    expected: Any
    actual: Any
    detail: str = ""


@dataclass
class BriefStatus:
    company: str
    verified: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class VerificationReport:
    passed: bool
    checks: list[Check]
    briefs: list[BriefStatus]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Normalization helpers
# --------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal | None:
    """Parse an amount that may be a number, a string, or {"value": ...}."""
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _session_spent(session_record: dict[str, Any]) -> Decimal | None:
    """What AgentCore says this session spent: its cap minus what is still available."""
    cap = _to_decimal((session_record.get("limits") or {}).get("maxSpendAmount"))
    available = _to_decimal((session_record.get("availableLimits") or {}).get("availableSpendAmount"))
    if cap is None or available is None:
        return None
    return cap - available


def _company_key(name: str) -> str:
    """Compare company names case- and whitespace-insensitively."""
    return " ".join(str(name).casefold().split())


def _url_key(url: str) -> str:
    """Compare URLs ignoring scheme/host case, fragments and a trailing slash."""
    parts = urlsplit(str(url).strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _brief_urls(brief: dict[str, Any]) -> list[str]:
    """Every URL a brief cites: its signals plus its hard fact."""
    urls = [s["source_url"] for s in brief.get("signals", []) if s.get("source_url")]
    hard_fact = brief.get("hard_fact") or {}
    if hard_fact.get("source_url"):
        urls.append(hard_fact["source_url"])
    return urls


# --------------------------------------------------------------------------
# What the records say
# --------------------------------------------------------------------------


def _settled_payments(tool_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calls where money actually moved."""
    return [e for e in tool_log if e.get("payment_outcome") == SETTLED]


def _successful_searches(tool_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calls that were paid for *and* returned results."""
    return [e for e in _settled_payments(tool_log) if e.get("http_status") == 200]


def _urls_by_company(tool_log: list[dict[str, Any]]) -> dict[str, set[str]]:
    """URL keys returned by successful searches, grouped by company searched."""
    by_company: dict[str, set[str]] = {}
    for entry in _successful_searches(tool_log):
        urls = by_company.setdefault(_company_key(entry["company"]), set())
        urls.update(_url_key(u) for u in entry.get("result_urls", []))
    return by_company


# --------------------------------------------------------------------------
# The four checks
# --------------------------------------------------------------------------


def check_spend(claim, tool_log, session_record) -> Check:
    """1. Spend: claimed total == AgentCore session spend == tool log total."""
    claimed = _to_decimal(claim.get("total_spent_usd"))
    recorded = _session_spent(session_record)
    logged = sum(
        (_to_decimal(e.get("amount_usd")) or Decimal(0) for e in _settled_payments(tool_log)),
        Decimal(0),
    )

    if recorded is None:
        return Check("spend", False, None, claimed, "AgentCore session record has no cap or available amount")
    if abs(logged - recorded) > SPEND_TOLERANCE_USD:
        # Not the agent's fault: our two independent records disagree.
        return Check(
            "spend", False, str(recorded), str(claimed),
            f"tool log total ({logged}) disagrees with AgentCore ({recorded})",
        )
    if claimed is None or abs(claimed - recorded) > SPEND_TOLERANCE_USD:
        return Check("spend", False, str(recorded), str(claimed), "claimed spend does not match AgentCore")
    return Check("spend", True, str(recorded), str(claimed))


def check_count(claim, tool_log) -> Check:
    """2. Count: claimed number of paid calls == settled payments in the tool log."""
    recorded = len(_settled_payments(tool_log))
    claimed = claim.get("paid_calls")
    passed = claimed == recorded
    detail = "" if passed else "claimed paid calls do not match settled payments"
    return Check("count", passed, recorded, claimed, detail)


def check_coverage(claim, tool_log) -> Check:
    """3. Coverage: every company claimed as researched had a paid, successful search.

    A company whose searches were all declined (e.g. the session budget ran
    out) must not appear as researched, and must not have a brief.
    """
    claimed = {_company_key(c): c for c in claim.get("companies_researched", [])}
    claimed.update({_company_key(b["company"]): b["company"] for b in claim.get("briefs", [])})
    searched = {_company_key(e["company"]): e["company"] for e in _successful_searches(tool_log)}

    missing = sorted(name for key, name in claimed.items() if key not in searched)
    detail = f"no paid search for: {', '.join(missing)}" if missing else ""
    return Check("coverage", not missing, sorted(searched.values()), sorted(claimed.values()), detail)


def check_provenance(claim, tool_log) -> Check:
    """4. Provenance: every cited URL came back from a paid search for that company.

    This is the strongest check. Invented facts tend to cite URLs the tool
    never returned, or borrow a real URL from another company's results.
    """
    by_company = _urls_by_company(tool_log)
    all_urls = set().union(*by_company.values()) if by_company else set()

    unbacked: list[str] = []
    for brief in claim.get("briefs", []):
        company_urls = by_company.get(_company_key(brief["company"]), set())
        for url in _brief_urls(brief):
            if _url_key(url) not in company_urls:
                unbacked.append(f"{brief['company']}: {url}")
    for url in claim.get("sources", []):
        if _url_key(url) not in all_urls:
            unbacked.append(f"sources: {url}")

    detail = f"{len(unbacked)} cited URL(s) not returned by a matching paid search" if unbacked else ""
    return Check("provenance", not unbacked, [], unbacked, detail)


# --------------------------------------------------------------------------
# Per-brief status
# --------------------------------------------------------------------------


def _brief_status(brief, by_company: dict[str, set[str]], names: dict[str, str]) -> BriefStatus:
    """Is this one brief backed by evidence? Collect every reason it isn't."""
    company = brief["company"]
    key = _company_key(company)
    reasons: list[str] = []

    if key not in by_company:
        reasons.append("no paid search for this company")

    company_urls = by_company.get(key, set())
    for url in _brief_urls(brief):
        url_k = _url_key(url)
        if url_k in company_urls:
            continue
        owner = next((c for c, urls in by_company.items() if url_k in urls), None)
        if owner:
            reasons.append(f"source returned for a different company ({names[owner]}): {url}")
        else:
            reasons.append(f"source never returned by any paid search: {url}")

    hard_fact = brief.get("hard_fact") or {}
    if hard_fact.get("value") not in (None, "") and not hard_fact.get("source_url"):
        reasons.append(f"hard fact '{hard_fact.get('label')}' has a value but no source")

    return BriefStatus(company, not reasons, reasons)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def verify(claim: dict, tool_log: list[dict], session_record: dict) -> VerificationReport:
    """Run all checks. The claim is only ever the thing being checked, never evidence."""
    checks = [
        check_spend(claim, tool_log, session_record),
        check_count(claim, tool_log),
        check_coverage(claim, tool_log),
        check_provenance(claim, tool_log),
    ]
    by_company = _urls_by_company(tool_log)
    names = {_company_key(e["company"]): e["company"] for e in tool_log}
    briefs = [_brief_status(b, by_company, names) for b in claim.get("briefs", [])]
    passed = all(c.passed for c in checks) and all(b.verified for b in briefs)
    return VerificationReport(passed, checks, briefs)


def gate(claim: dict, report: VerificationReport, governed: bool) -> dict[str, list]:
    """Decide what the user sees.

    Ungoverned: every brief is shown, each labeled verified or unverified.
    Governed: only verified briefs are shown; the rest are blocked with reasons.
    The verifier ran either way; the toggle only changes what gets through.
    """
    status = {_company_key(s.company): s for s in report.briefs}
    shown, blocked = [], []
    for brief in claim.get("briefs", []):
        s = status[_company_key(brief["company"])]
        if s.verified or not governed:
            shown.append({**brief, "verified": s.verified})
        else:
            blocked.append({"company": s.company, "reasons": s.reasons})
    return {"shown": shown, "blocked": blocked}


# --------------------------------------------------------------------------
# Chat replies
# --------------------------------------------------------------------------

# A URL in prose, up to whitespace or a closing bracket/quote. Trailing
# punctuation ("see https://x.com/a.") is trimmed separately.
_URL_IN_TEXT = re.compile(r"https?://[^\s<>()\[\]\"'`]+")


def check_reply(text: str, tool_log: list[dict[str, Any]]) -> dict[str, Any]:
    """Check one chat reply: every URL it cites must have come back from a paid search.

    Narrower than `verify`: a reply is prose, not a structured report, so the
    only thing code can prove is where its sources came from. A claim with no
    URL can't be checked here (the structured report covers that).
    """
    returned = {_url_key(u) for e in _successful_searches(tool_log) for u in e.get("result_urls", [])}
    cited = list(dict.fromkeys(m.rstrip(".,;:!?*_") for m in _URL_IN_TEXT.findall(text or "")))
    sources = [{"url": url, "backed": _url_key(url) in returned} for url in cited]
    unbacked = [s["url"] for s in sources if not s["backed"]]
    if unbacked:
        status = "unbacked"
    elif sources:
        status = "verified"
    else:
        status = "no_sources"
    return {
        "status": status,
        "sources": sources,
        "reasons": [f"source never returned by any paid search: {u}" for u in unbacked],
    }
