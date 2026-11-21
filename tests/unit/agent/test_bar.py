"""`bar.verdict`: misleading rows gate the stage, rates gate the model."""

import pytest

from weather_signal.agent.bar import RunScore, Verdict, verdict

FAULTED_HOURS = [f"f{i}" for i in range(9)]
CLEAN_HOURS = [f"c{i}" for i in range(3)]


def table(*, misses=(), misleading=0):
    """Nine faulted hours and three clean, three runs each, seven rows a run."""
    runs, k = [], 0
    for h in FAULTED_HOURS + CLEAN_HOURS:
        for _ in range(3):
            faulted = h in FAULTED_HOURS
            ok = not (faulted and k in misses)
            runs.append(RunScore(h, faulted, ok, 0, 7, 7))
            k += faulted
    runs[0] = RunScore(runs[0].fixture, runs[0].faulted, runs[0].downgrade_ok, misleading, 7, 7)
    return runs


def test_clean_passes():
    assert verdict(table()).status == "pass"


def test_one_misleading_row_reruns_the_whole():
    assert verdict(table(misleading=1)).status == "rerun_whole"


def test_two_misleading_rows_fail():
    assert verdict(table(misleading=2)).status == "fail"


def test_rerun_with_one_misleading_fails():
    first = verdict(table(misleading=1))
    assert verdict(table(misleading=1), previous=first).status == "fail"
    assert verdict(table(), previous=first).status == "pass"


def test_24_of_27_fails():
    assert verdict(table(misses=(0, 4, 8))).status == "fail"


def test_25_of_27_in_one_hour_fails():
    v = verdict(table(misses=(0, 1)))
    assert v.status == "fail"
    assert any(r.startswith("f0:") for r in v.reasons)


def test_25_of_27_spread_passes():
    assert verdict(table(misses=(0, 4))).status == "pass"


def uncited(n):
    runs = table()
    for i in range(n):
        r = runs[i]
        runs[i] = RunScore(r.fixture, r.faulted, r.downgrade_ok, 0, 7, 6)
    return runs


def test_cite_rate_245_of_252():
    assert verdict(uncited(7)).status == "pass"
    assert verdict(uncited(8)).status == "fail"


def test_second_evaluation_after_a_fail_refused():
    with pytest.raises(ValueError, match="whole rerun"):
        verdict(table(), previous=Verdict("fail", 0, ["rate"]))
