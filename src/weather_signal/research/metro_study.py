"""The metro study in three stages. `extract` reads dev over Databricks Connect into training and
holdout files; `search` reads only the training files and freezes its picks; `holdout` tests them.

    python -m weather_signal.research.metro_study {extract,search,holdout} --out DIR
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from weather_signal.config import HISTORY_START, TICKERS
from weather_signal.research import metro_holdout as mh
from weather_signal.research.metro_data import (
    LAGS,
    TARGETS,
    Panel,
    anomalies,
    forward_targets,
    normals_before,
    panel,
)
from weather_signal.research.metro_search import (
    LassoCV,
    Search,
    SubsetScorer,
    null_search,
    null_share,
)

log = logging.getLogger(__name__)
# Fixed before any WMT data was read: the search starts at the last benchmark's first session.
TRAIN = (date(1998, 12, 22), date(2012, 12, 31))
HOLDOUT = (date(2013, 1, 1), date(2026, 11, 17))
NORMALS_CUTOFF = HISTORY_START
LASSO_TARGET = "xs_spy_1d"
OWNER = {"35620": "New York", "33100": "Miami", "32820": "Memphis (TN-MS-AR)"}
SEED_METROS = Path(__file__).resolve().parents[1] / "seed" / "metros.csv"


def extract(out: Path, profile: str = "weather-signal", prefix: str = "dev_") -> None:
    """Read-only: two SELECTs, each split at the holdout boundary into separate files."""
    from weather_signal.spark import connect_session

    spark = connect_session(profile)
    obs = spark.sql(
        f"SELECT cbsa_code, date, t_mean FROM workspace.{prefix}daily.weather_observed_daily"
    ).toPandas()
    px = spark.sql(
        f"SELECT ticker, trade_date, close FROM workspace.{prefix}daily.prices_daily "
        f"WHERE ticker IN ({', '.join(repr(t) for t in TICKERS)})"
    ).toPandas()
    obs["date"], px["trade_date"] = pd.to_datetime(obs["date"]), pd.to_datetime(px["trade_date"])
    cut = pd.Timestamp(HOLDOUT[0])
    obs[obs["date"] < cut].to_csv(out / "weather_train.csv.gz", index=False)
    obs[obs["date"] >= cut].to_csv(out / "weather_holdout.csv.gz", index=False)
    px[px["trade_date"] < cut].to_csv(out / "prices_train.csv.gz", index=False)
    px[px["trade_date"] >= cut].to_csv(out / "prices_holdout.csv.gz", index=False)


def _read(out: Path, name: str) -> pd.DataFrame:
    df = pd.read_csv(out / f"{name}.csv.gz", dtype={"cbsa_code": str})
    for col in ("date", "trade_date"):
        if col in df:
            df[col] = pd.to_datetime(df[col])
    return df


def _closes(px: pd.DataFrame) -> pd.DataFrame:
    return px.pivot(index="trade_date", columns="ticker", values="close").sort_index()


def _names() -> dict[str, str]:
    m = pd.read_csv(SEED_METROS, dtype={"cbsa_code": str})
    return dict(zip(m["cbsa_code"], m["point_place"].str.replace(" city", ""), strict=True))


def _feature(p: Panel, metros: list[str], kind: str, lag: int) -> np.ndarray:
    cols = [p.metros.index(c) for c in metros]
    return p.features[(kind, lag)][:, cols].mean(axis=1)


def _pick(p: Panel, family: str, metros: list[str], spec: tuple[str, int, str]) -> dict[str, Any]:
    kind, lag, target = spec
    x, y = _feature(p, metros, kind, lag), p.targets[:, TARGETS.index(target)]
    r = mh.corr(x, y)
    sign, thr = int(np.sign(r)), float(np.median(x))
    return {
        "family": family, "metros": metros, "kind": kind, "lag": lag, "target": target,
        "sign": sign, "threshold": thr, "train_corr": r,
        "train_hit": mh.hit_rate(mh.positions(x, sign, thr), y),
    }  # fmt: skip


def _top_screen(p: Panel, search: Search, real: Any, k: int) -> list[dict[str, Any]]:
    a = np.abs(real.screen)
    picks: list[dict[str, Any]] = []
    for flat in np.argsort(a, axis=None)[::-1]:
        f, m, t = np.unravel_index(flat, a.shape)
        if any(pk["metros"] == [p.metros[m]] for pk in picks):
            continue
        kind, lag = search.keys[f]
        picks.append(_pick(p, "screen", [p.metros[m]], (kind, lag, TARGETS[t])))
        if len(picks) == k:
            break
    return picks


def _top_combos(p: Panel, search: Search, real: Any, k: int) -> list[dict[str, Any]]:
    rows = []
    for s, v in real.combos.items():
        a = np.abs(v)
        best = a.max(axis=(0, 2))  # per subset, over lags and targets
        for i in np.argsort(best)[::-1][:k]:
            lag, t = np.unravel_index(a[:, i, :].argmax(), (len(LAGS), len(TARGETS)))
            rows.append((best[i], [p.metros[j] for j in search.idx[s][i]], int(lag), TARGETS[t]))
    rows.sort(key=lambda r: -r[0])
    return [_pick(p, "combo", ms, ("level", lag, t)) for _, ms, lag, t in rows[:k]]


def _owner(p: Panel) -> dict[str, Any]:
    idx = np.array([[p.metros.index(c) for c in OWNER]])
    best = max(
        ((abs(float(r)), lag, TARGETS[t]) for lag in LAGS
         for t, r in enumerate(SubsetScorer(p.features[("level", lag)], idx).corr(p.targets)[0])),
    )  # fmt: skip
    return _pick(p, "owner", list(OWNER), ("level", best[1], best[2]))


def _lasso_x(p: Panel) -> np.ndarray:
    return np.hstack([p.features[("level", lag)] for lag in LAGS])


def _lasso(p: Panel, offsets: np.ndarray) -> tuple[dict[str, Any], dict[str, Any]]:
    cv = LassoCV(_lasso_x(p))
    y = p.targets[:, TARGETS.index(LASSO_TARGET)]
    r2, alphas = cv.cv_r2(y)
    best = int(r2.argmax())
    coef = cv.fit(y, float(alphas[best]))
    null = np.array([cv.cv_r2(np.roll(y, int(o)))[0].max() for o in offsets])
    used = [
        {
            "metro": p.metros[j % len(p.metros)],
            "lag": int(j) // len(p.metros),
            "coef": float(coef[j]),
        }
        for j in np.flatnonzero(coef)
    ]
    pick = {
        "family": "lasso", "target": LASSO_TARGET, "alpha": float(alphas[best]),
        "coef": coef.tolist(), "mu": cv.mu.tolist(), "sd": cv.sd.tolist(), "used": used,
        "metros": p.metros, "sign": 1, "threshold": 0.0, "train_cv_r2": float(r2[best]),
    }  # fmt: skip
    summary = {
        "cv_r2": r2.tolist(), "alphas": alphas.tolist(), "best_cv_r2": float(r2[best]),
        "null_best_cv_r2": null.tolist(), "share_null_at_or_above": null_share(float(r2[best]), null),
        "n_used": len(used),
    }  # fmt: skip
    return pick, summary


def _per_metro(
    p: Panel, search: Search, real: Any, null: Any, names: dict[str, str]
) -> pd.DataFrame:
    a = np.abs(real.screen)
    rows = []
    for m, code in enumerate(p.metros):
        f, t = np.unravel_index(a[:, m, :].argmax(), (a.shape[0], a.shape[2]))
        kind, lag = search.keys[f]
        rows.append({
            "cbsa_code": code, "metro": names.get(code, code), "best_abs_corr": a[f, m, t],
            "corr": real.screen[f, m, t], "kind": kind, "lag": lag, "target": TARGETS[t],
            "corr_xs_spy_1d_level_lag0": real.screen[search.keys.index(("level", 0)), m, 2],
            "null_share": null_share(float(a[f, m, t]), null.per_metro[:, m]),
        })  # fmt: skip
    df = pd.DataFrame(rows)
    df["holm"] = mh.holm(df["null_share"].tolist())
    return df.sort_values("best_abs_corr", ascending=False)


def _top_combo_table(
    p: Panel, search: Search, real: Any, names: dict[str, str], k: int = 20
) -> pd.DataFrame:
    rows = []
    for s, v in real.combos.items():
        a = np.abs(v)
        for flat in np.argsort(a, axis=None)[::-1][: k * 4]:
            lag, i, t = np.unravel_index(flat, a.shape)
            ms = [names[p.metros[j]] for j in search.idx[s][i]]
            rows.append({"metros": " + ".join(ms), "size": s, "lag": int(lag),
                         "target": TARGETS[t], "corr": float(v[lag, i, t])})  # fmt: skip
    df = pd.DataFrame(rows)
    df = df.loc[df["corr"].abs().sort_values(ascending=False).index]
    return df.drop_duplicates("metros").head(k)


def run_search(out: Path, n_null: int = 200, seed: int = 0) -> str:
    obs, px = _read(out, "weather_train"), _read(out, "prices_train")
    normals = normals_before(obs, NORMALS_CUTOFF)
    p = panel(anomalies(obs, normals), forward_targets(_closes(px)), *TRAIN)
    names = _names()
    search = Search(p)
    real = search.run(p.targets)
    null = null_search(search, p.targets, n_null, seed)
    lasso_pick, lasso_summary = _lasso(p, null.offsets)
    _per_metro(p, search, real, null, names).to_csv(out / "per_metro.csv", index=False)
    _top_combo_table(p, search, real, names).to_csv(out / "top_combos.csv", index=False)
    rm = real.maxima()
    summary = {
        "rows": len(p.sessions), "first": str(p.sessions[0].date()), "last": str(p.sessions[-1].date()),
        "normal_years": [int(normals.n_years.min()), int(normals.n_years.max())],
        "normal_cutoff": str(normals.cutoff), "n_null": n_null, "offsets": null.offsets.tolist(),
        "real": rm, "null": {k: v.tolist() for k, v in null.maxima.items()},
        "share_null_at_or_above": {k: null_share(rm[k], null.maxima[k]) for k in rm},
        "lasso": lasso_summary,
    }  # fmt: skip
    (out / "search.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    picks = _top_screen(p, search, real, 5) + _top_combos(p, search, real, 5)
    picks += [lasso_pick, _owner(p)]
    for i, pk in enumerate(picks):
        pk["id"] = f"P{i + 1:02d}"
    return mh.freeze(picks, out / "picks.json")


def _holdout_feature(p: Panel, pk: dict[str, Any]) -> np.ndarray:
    if pk["family"] != "lasso":
        return _feature(p, pk["metros"], pk["kind"], pk["lag"])
    if pk["metros"] != p.metros:
        raise ValueError("holdout metros differ from the frozen LASSO's")
    z = (_lasso_x(p) - np.array(pk["mu"])) / np.array(pk["sd"])
    return np.asarray(z @ np.array(pk["coef"]))


def _test(p: Panel, pk: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    x, y = _holdout_feature(p, pk), p.targets[:, TARGETS.index(pk["target"])]
    row: dict[str, Any] = {
        "id": pk["id"], "family": pk["family"], "target": pk["target"],
        "metros": "LASSO" if pk["family"] == "lasso" else " + ".join(names[c] for c in pk["metros"]),
        "lag": pk.get("lag"), "kind": pk.get("kind"), "train": pk.get("train_corr", pk.get("train_cv_r2")),
        "always_buy": float((y > 0).mean()), "n": len(y),
    }  # fmt: skip
    if np.ptp(x) == 0:
        return row | {"note": "constant prediction: no metro selected"}
    pos = mh.positions(x, pk["sign"], pk["threshold"])
    band = mh.random_band(y, float((pos > 0).mean()))
    return row | {
        "hit": mh.hit_rate(pos, y), "buy_share": float((pos > 0).mean()), "random": band,
        "corr": mh.corr(x, y), "ci": mh.block_bootstrap_ci(x, y),
        "p_shift": mh.shift_pvalue(x, y, pk["sign"]),
    }  # fmt: skip


def run_holdout(out: Path) -> list[dict[str, Any]]:
    picks = mh.load_frozen(out / "picks.json")
    obs = pd.concat([_read(out, "weather_train"), _read(out, "weather_holdout")])
    normals = normals_before(obs, NORMALS_CUTOFF)
    px = pd.concat([_read(out, "prices_train"), _read(out, "prices_holdout")])
    closes = _closes(px).loc[pd.Timestamp(HOLDOUT[0]) :]
    p = panel(anomalies(obs, normals), forward_targets(closes), *HOLDOUT)
    names = _names()
    rows = [_test(p, pk, names) for pk in picks]
    tested = [r for r in rows if "p_shift" in r]
    for r, adj in zip(tested, mh.holm([r["p_shift"] for r in tested]), strict=True):
        r["p_holm"] = adj
    meta = {"rows": len(p.sessions), "first": str(p.sessions[0].date()),
            "last": str(p.sessions[-1].date()), "tested": len(tested)}  # fmt: skip
    (out / "holdout.json").write_text(
        json.dumps({"meta": meta, "picks": rows}, indent=1), encoding="utf-8"
    )
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="metro_study")
    ap.add_argument("stage", choices=["extract", "search", "holdout"])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--null", type=int, default=200)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    a.out.mkdir(parents=True, exist_ok=True)
    if a.stage == "extract":
        extract(a.out)
    elif a.stage == "search":
        log.info("picks frozen, sha256 %s", run_search(a.out, a.null))
    else:
        log.info("holdout tested %d picks", len(run_holdout(a.out)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
