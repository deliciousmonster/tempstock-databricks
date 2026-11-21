"""The study's stages on synthetic files: the search runs with no holdout file present, and the
holdout runs only the picks as frozen."""

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from weather_signal.research import metro_study as m

CODES = ["35620", "33100", "32820", "41860", "14460"]


@pytest.fixture
def study(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "TRAIN", (date(2001, 1, 1), date(2001, 12, 31)))
    monkeypatch.setattr(m, "HOLDOUT", (date(2002, 1, 1), date(2002, 12, 31)))
    monkeypatch.setattr(m, "NORMALS_CUTOFF", date(2001, 1, 1))
    rng = np.random.default_rng(5)
    days = pd.date_range("2000-01-01", "2002-12-31", freq="D")
    obs = pd.DataFrame(
        [{"cbsa_code": c, "date": d, "t_mean": rng.normal(15, 5)} for d in days for c in CODES]
    )
    sessions = pd.bdate_range("2001-01-01", "2002-12-31")
    px = pd.concat(
        pd.DataFrame({"ticker": t, "trade_date": sessions,
                      "close": 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(sessions))))})
        for t in ("WMT", "SPY", "XLP")
    )  # fmt: skip
    cut = pd.Timestamp("2002-01-01")
    obs[obs["date"] < cut].to_csv(tmp_path / "weather_train.csv.gz", index=False)
    px[px["trade_date"] < cut].to_csv(tmp_path / "prices_train.csv.gz", index=False)
    return tmp_path, obs[obs["date"] >= cut], px[px["trade_date"] >= cut]


def test_search_needs_no_holdout_file_and_holdout_tests_every_frozen_pick(study):
    out, obs_h, px_h = study
    digest = m.run_search(out, n_null=3)
    assert (out / "picks.sha256").read_text().startswith(digest)
    assert not (out / "weather_holdout.csv.gz").exists()
    obs_h.to_csv(out / "weather_holdout.csv.gz", index=False)
    px_h.to_csv(out / "prices_holdout.csv.gz", index=False)
    rows = m.run_holdout(out)
    assert [r["id"] for r in rows] == [f"P{i:02d}" for i in range(1, len(rows) + 1)]
    meta = json.loads((out / "holdout.json").read_text())["meta"]
    assert meta["first"] >= "2002-01-01"
    tested = [r for r in rows if "p_holm" in r]
    assert tested and all(r["p_holm"] >= r["p_shift"] for r in tested)


def test_holdout_refuses_picks_edited_after_freezing(study):
    out, obs_h, px_h = study
    m.run_search(out, n_null=3)
    obs_h.to_csv(out / "weather_holdout.csv.gz", index=False)
    px_h.to_csv(out / "prices_holdout.csv.gz", index=False)
    picks = out / "picks.json"
    picks.write_text(picks.read_text().replace('"lag": 0', '"lag": 1', 1) + " ")
    with pytest.raises(ValueError, match="changed after it was frozen"):
        m.run_holdout(out)


def test_targets_and_windows_follow_the_stock_switch():
    from weather_signal.research import metro_data as md

    assert md.TARGETS == (
        "stock_1d",
        "stock_5d",
        "xs_spy_1d",
        "xs_spy_5d",
        "xs_xlp_1d",
        "xs_xlp_5d",
    )
    assert m.TRAIN == (date(1998, 12, 22), date(2012, 12, 31))
    assert m.HOLDOUT[0] == date(2013, 1, 1) and m.NORMALS_CUTOFF == date(1991, 1, 1)
