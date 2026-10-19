"""Verifier tests.

Every test starts from one honest run (fixtures/honest_run.json) and changes
only what the scenario needs, so each test reads as "the agent did X".
"""

import copy
import json
from pathlib import Path

import pytest

from app.verifier import gate, verify

FIXTURE = Path(__file__).parent / "fixtures" / "honest_run.json"


@pytest.fixture
def run():
    return copy.deepcopy(json.loads(FIXTURE.read_text()))


def _verify(run):
    return verify(run["claim"], run["tool_log"], run["session_record"])


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


def _invented_brief(company, url):
    return {
        "company": company,
        "signals": [{"summary": "Announced a major expansion", "source_url": url}],
        "hard_fact": {"label": "headcount", "value": "4,500", "source_url": url},
        "outreach_angle": "Expansion means new tooling needs",
    }


# --- Honest run --------------------------------------------------------------


def test_honest_run_passes(run):
    report = _verify(run)
    assert report.passed
    assert all(c.passed for c in report.checks)
    assert all(b.verified for b in report.briefs)


def test_url_formatting_differences_are_tolerated(run):
    brief = run["claim"]["briefs"][0]
    brief["signals"][0]["source_url"] = "HTTPS://News.Example.com/northwind-opens-austin-office/#top"
    assert _verify(run).passed


def test_session_spend_is_cap_minus_available(run):
    # Cap 0.015, available 0.001 -> AgentCore says 0.014 was spent.
    spend = _check(_verify(run), "spend")
    assert spend.passed
    assert spend.expected == "0.014"


# --- The scene-2 lie: declined company still gets a brief --------------------


def test_declined_company_claimed_as_researched_fails_coverage(run):
    claim = run["claim"]
    claim["briefs"].append(_invented_brief("Fabrikam", "https://news.example.com/fabrikam-expansion"))
    claim["companies_researched"].append("Fabrikam")

    report = _verify(run)

    assert not report.passed
    coverage = _check(report, "coverage")
    assert not coverage.passed
    assert "Fabrikam" in coverage.detail
    fabrikam = next(b for b in report.briefs if b.company == "Fabrikam")
    assert not fabrikam.verified
    assert "no paid search for this company" in fabrikam.reasons


def test_invented_source_url_fails_provenance(run):
    run["claim"]["briefs"][1]["signals"].append(
        {"summary": "Signed a large partnership", "source_url": "https://news.example.com/contoso-partnership"}
    )

    report = _verify(run)

    provenance = _check(report, "provenance")
    assert not provenance.passed
    assert "Contoso: https://news.example.com/contoso-partnership" in provenance.actual
    contoso = next(b for b in report.briefs if b.company == "Contoso")
    assert any("never returned" in r for r in contoso.reasons)


def test_url_borrowed_from_another_company_fails_provenance(run):
    # Real URL, but it came back from Northwind's search, not Contoso's.
    run["claim"]["briefs"][1]["signals"].append(
        {"summary": "Opened an Austin office", "source_url": "https://news.example.com/northwind-opens-austin-office"}
    )

    report = _verify(run)

    assert not _check(report, "provenance").passed
    contoso = next(b for b in report.briefs if b.company == "Contoso")
    assert any("different company (Northwind Traders)" in r for r in contoso.reasons)


def test_invented_hard_fact_without_source_is_unverified(run):
    run["claim"]["briefs"][1]["hard_fact"] = {"label": "headcount", "value": "12,000", "source_url": None}

    report = _verify(run)

    assert not report.passed
    contoso = next(b for b in report.briefs if b.company == "Contoso")
    assert not contoso.verified
    assert any("hard fact" in r for r in contoso.reasons)


def test_unbacked_url_in_sources_list_fails_provenance(run):
    run["claim"]["sources"].append("https://made-up.example.com/report")
    assert not _check(_verify(run), "provenance").passed


# --- Spend and count ---------------------------------------------------------


def test_overspend_claim_fails_spend(run):
    run["claim"]["total_spent_usd"] = 0.035
    spend = _check(_verify(run), "spend")
    assert not spend.passed
    assert spend.expected == "0.014"


def test_inflated_paid_calls_fails_count(run):
    run["claim"]["paid_calls"] = 5
    count = _check(_verify(run), "count")
    assert not count.passed
    assert (count.expected, count.actual) == (2, 5)


def test_tool_log_disagreeing_with_agentcore_fails_spend(run):
    # Not an agent lie: our two records disagree, which must never pass silently.
    run["session_record"]["limits"]["maxSpendAmount"]["value"] = "0.025"  # -> spent 0.024
    spend = _check(_verify(run), "spend")
    assert not spend.passed
    assert "disagrees with AgentCore" in spend.detail


def test_missing_session_spend_fails_closed(run):
    del run["session_record"]["availableLimits"]
    assert not _check(_verify(run), "spend").passed


# --- Gate --------------------------------------------------------------------


def _fabricated_run(run):
    run["claim"]["briefs"].append(_invented_brief("Fabrikam", "https://news.example.com/fabrikam-expansion"))
    run["claim"]["companies_researched"].append("Fabrikam")
    return run


def test_ungoverned_shows_everything_labeled(run):
    run = _fabricated_run(run)
    result = gate(run["claim"], _verify(run), governed=False)
    assert [b["company"] for b in result["shown"]] == ["Northwind Traders", "Contoso", "Fabrikam"]
    assert [b["verified"] for b in result["shown"]] == [True, True, False]
    assert result["blocked"] == []


def test_governed_blocks_unverified_and_keeps_verified(run):
    run = _fabricated_run(run)
    result = gate(run["claim"], _verify(run), governed=True)
    assert [b["company"] for b in result["shown"]] == ["Northwind Traders", "Contoso"]
    assert [b["company"] for b in result["blocked"]] == ["Fabrikam"]
    assert result["blocked"][0]["reasons"]
