"""The operator ceiling: a session's cap is what was asked for, but never above it."""

from app.orchestrator import clamp_budget


def test_request_under_the_ceiling_is_kept():
    assert clamp_budget("0.015", "0.05") == ("0.015", False)


def test_request_over_the_ceiling_is_cut_to_the_ceiling():
    assert clamp_budget("10.00", "0.05") == ("0.05", True)


def test_no_ceiling_means_no_clamp():
    assert clamp_budget("10.00", None) == ("10.00", False)


def test_amounts_compare_as_numbers_not_strings():
    assert clamp_budget("0.1", "0.05") == ("0.05", True)   # "0.1" < "0.05" as strings
    assert clamp_budget("0.050", "0.05") == ("0.050", False)
