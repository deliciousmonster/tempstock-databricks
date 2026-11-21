"""Each `known_at` bound catches a row forged 48 h early; the fill signature catches a mistyped `P`."""

from datetime import date, timedelta

from pyspark.sql import functions as F

from tests.conftest import fixture_json
from tests.spark.conftest import ts
from tests.spark.helpers import envelopes
from weather_signal.pipeline import transforms


def test_price_bound_selects_a_forged_row(spark):
    close = ts(2026, 11, 16, 21)
    df = spark.createDataFrame(
        [
            ("WMT", date(2026, 11, 16), close, close + timedelta(minutes=20)),
            ("WMT", date(2026, 11, 16), close, close - timedelta(hours=48)),
            ("WMT", date(2026, 11, 16), close, close + timedelta(minutes=5)),
        ],
        "ticker string, trade_date date, close_utc timestamp, known_at timestamp",
    )
    bad = sorted(r["known_at"] for r in df.where(~F.expr(transforms.PRICE_KNOWN_AT_OK)).collect())
    assert bad == [close - timedelta(hours=48), close + timedelta(minutes=5)]


def test_era5_bound_selects_a_forged_row(spark):
    d = date(2026, 11, 1)
    df = spark.createDataFrame(
        [(d, ts(2026, 11, 9)), (d, ts(2026, 11, 9) - timedelta(hours=48 * 3))],
        "date date, known_at timestamp",
    )
    bad = df.where(~transforms.era5_bound_ok()).collect()
    assert len(bad) == 1


def _runs(spark, metros, p, items):
    env = envelopes(spark, items)
    landed = transforms.landed_forecast(transforms.forecast_points(env, metros), p)
    return transforms.fill_runs(landed)


def _max_run(spark, metros, p):
    items = [("e1", ts(2026, 11, 17, 18, 35), fixture_json("prevruns-1835.json"))]
    return _runs(spark, metros, p, items).agg(F.max("max_run")).first()[0]


def test_fill_signature_passes_at_p24_and_fails_with_p_mistyped(spark, metros):
    # Measured on 18:35: max_run 2 at P 19 to 30, 4 at 12, 6 at 10, 10 at 6. The plan's
    # "12 h gives >= 6" does not reproduce; a mistype down to 11 h passes the check.
    assert _max_run(spark, metros, 24) < 6
    assert _max_run(spark, metros, 30) < 6
    assert _max_run(spark, metros, 12) == 4
    assert _max_run(spark, metros, 10) >= 6


def _synthetic(lead0, lead3):
    n = len(lead0)
    hours = [f"2026-11-10T{h:02d}:00" for h in range(n)]
    h = {"time": hours, "temperature_2m": lead0}
    for k in range(1, 8):
        h[f"temperature_2m_previous_day{k}"] = lead3 if k == 3 else [None] * n
    return {"hourly": h}


def test_single_chance_match_is_a_run_of_one(spark, metros):
    body = _synthetic([1.0, 2.0, 3.0, 4.0], [9.0, 2.0, 9.0, 9.0])
    out = _runs(spark, metros, 30, [("e", ts(2026, 11, 17), body)]).collect()
    assert [r["max_run"] for r in out] == [1]


def test_runs_counted_within_one_envelope(spark, metros):
    a = _synthetic([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], [1.0, 2.0, 3.0, 9.0, 9.0, 9.0])
    b = _synthetic([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], [9.0, 9.0, 9.0, 4.0, 5.0, 6.0])
    out = _runs(
        spark, metros, 30, [("a", ts(2026, 11, 17), a), ("b", ts(2026, 11, 17, 1), b)]
    ).collect()
    assert sorted(r["max_run"] for r in out) == [3, 3]


def _coincidence(run_hours, filled=(0,), short=()):
    """Two days, every lead off lead 0 except lead 7 at the `filled` positions (rank - 1) for
    `run_hours` hours; the `short` positions carry 2022-04-30 only."""
    hours = [f"2022-04-{d}T{h:02d}:00" for d in (29, 30) for h in range(24)]
    lead0 = [10.0 + (i % 5) / 10 for i in range(48)]
    locs = []
    for m in range(50):
        h = {"time": hours, "temperature_2m": lead0}
        h |= {f"temperature_2m_previous_day{n}": [v + n for v in lead0] for n in range(1, 8)}
        if m in filled:
            h["temperature_2m_previous_day7"] = [
                v if 5 <= i < 5 + run_hours else v + 7 for i, v in enumerate(lead0)
            ]
        if m in short:
            h = {k: v[24:] for k, v in h.items()}
        locs.append({"hourly": h})
    return locs


def _landed(spark, metros, run_hours, filled=(0,), short=()):
    body = _coincidence(run_hours, filled, short)
    env = envelopes(spark, [("b", ts(2026, 11, 18, 1), body)], "2022-04-20..2022-05-03")
    return transforms.landed_forecast(transforms.forecast_points(env, metros), 30)


def test_a_six_hour_match_is_quarantined_hour_by_hour_with_a_reason(spark, metros):
    q = transforms.fill_quarantine(_landed(spark, metros, 6)).collect()
    assert len(q) == 6 and {r["lead_days"] for r in q} == {7}
    assert {r["reason"] for r in q} == {"lead_equals_lead0_6h"} and {r["run"] for r in q} == {6}
    assert transforms.fill_quarantine(_landed(spark, metros, 5)).count() == 0


def test_quarantined_hours_leave_only_their_day_out_of_the_daily_forecast(spark, metros):
    landed = _landed(spark, metros, 6)
    kept = transforms.drop_quarantined(landed, transforms.fill_quarantine(landed))
    daily = transforms.forecast_daily(kept)
    rank1 = metros.where("rank = 1").first()["cbsa_code"]
    left = daily.where(f"cbsa_code = '{rank1}' AND lead_days = 7").collect()
    assert [r["target_date"] for r in left] == [date(2022, 4, 30)]
    assert daily.count() == 50 * 7 * 2 - 1


def _cell(spark, metros, filled, short=(), lead=7):
    """The 2022-04-29 index cell at `lead`, or None when the index has no row for it."""
    landed = _landed(spark, metros, 6, filled, short)
    q = transforms.fill_quarantine(landed)
    kept = transforms.drop_quarantined(landed, q)
    norms = spark.createDataFrame([(119, 9.0), (120, 9.0)], "day_of_year int, normal_c double")
    cells = transforms.quarantined_days(landed, q)
    out = transforms.forecast_index(transforms.forecast_daily(kept), metros, norms, cells)
    rows = out.where(f"target_date = DATE'2022-04-29' AND lead_days = {lead}").collect()
    return rows[0] if rows else None


LIGHTEST, NEW_YORK = 49, 0  # body positions: rank 50 (weight 0.0062) and rank 1 (0.1066)
RANK_9, RANK_10 = 8, 9  # weights 0.0336 and 0.0277, either side of the 0.03 the floor admits
FOUR_LIGHTEST = (46, 47, 48, 49)  # 0.0252 together


def test_the_floor_holds_a_cell_without_rank_9_and_keeps_it_without_rank_10(spark, metros):
    assert _cell(spark, metros, (RANK_9,))["quarantined"] is True
    assert _cell(spark, metros, (RANK_10,))["quarantined"] is False


def test_the_four_lightest_metros_quarantined_together_keep_the_cell(spark, metros):
    cell = _cell(spark, metros, FOUR_LIGHTEST)
    assert (cell["n_metros"], cell["quarantined"]) == (46, False)


def test_a_light_metros_coincidence_keeps_the_cell_over_the_other_metros(spark, metros):
    cell = _cell(spark, metros, (LIGHTEST,))
    assert (cell["n_metros"], cell["n_quarantined"], cell["quarantined"]) == (49, 1, False)
    assert cell["anomaly_c"] is not None


def test_a_heavy_metros_coincidence_leaves_the_cell_quarantined_without_a_value(spark, metros):
    cell = _cell(spark, metros, (NEW_YORK,))
    assert (cell["n_quarantined"], cell["quarantined"]) == (1, True)
    assert cell["anomaly_c"] is None and cell["known_at"] is not None


def test_a_lead_filled_at_every_metro_leaves_a_quarantined_cell_without_a_value(spark, metros):
    cell = _cell(spark, metros, tuple(range(50)))
    assert cell["quarantined"] is True and cell["n_quarantined"] == 50
    assert cell["anomaly_c"] is None and cell["known_at"] is not None
    assert _cell(spark, metros, tuple(range(50)), lead=6)["quarantined"] is False


def test_a_cell_short_for_other_reasons_is_not_labelled_quarantined(spark, metros):
    assert _cell(spark, metros, (LIGHTEST,), short=(1, 2)) is None


def test_the_fill_summary_counts_metros_per_envelope_and_lead(spark, metros):
    one = transforms.fill_summary(transforms.fill_quarantine(_landed(spark, metros, 6)))
    every = tuple(range(50))
    many = transforms.fill_summary(transforms.fill_quarantine(_landed(spark, metros, 6, every)))
    assert [(r["lead_days"], r["n_metros"], r["hours"]) for r in one.collect()] == [(7, 1, 6)]
    assert [(r["n_metros"], r["hours"]) for r in many.collect()] == [(50, 300)]
    assert many.first()["fetched_at"] == ts(2026, 11, 18, 1)


def _filled_1835():
    body = fixture_json("prevruns-1835.json")
    body["hourly"]["temperature_2m_previous_day3"] = list(body["hourly"]["temperature_2m"])
    return body


def _daily_lead3(spark, metros, items):
    """The pipeline's path: first sightings, quarantine on every envelope, daily without it."""
    landed = transforms.landed_forecast(
        transforms.forecast_points(envelopes(spark, items), metros), 30
    )
    q = transforms.fill_quarantine(landed)
    hourly = transforms.first_sightings(landed)
    daily = transforms.forecast_daily(transforms.drop_quarantined(hourly, q))
    return sorted(r["target_date"] for r in daily.where("lead_days = 3").collect()), q.count()


def test_a_filled_envelope_after_clean_ones_leaves_their_first_sightings_in_the_daily(
    spark, metros
):
    a = ("a", ts(2026, 11, 17, 18, 7), fixture_json("prevruns-1807.json"))
    b = ("b", ts(2026, 11, 17, 18, 35), fixture_json("prevruns-1835.json"))
    e = ("e", ts(2026, 11, 17, 19, 5), _filled_1835())
    clean, _ = _daily_lead3(spark, metros, [a, b])
    with_fill, n_q = _daily_lead3(spark, metros, [a, b, e])
    b_alone, _ = _daily_lead3(spark, metros, [b])
    e_alone, _ = _daily_lead3(spark, metros, [("e", b[1], e[2])])
    assert n_q > 0 and with_fill == clean and len(clean) == 3
    assert len(b_alone) > 0 and e_alone == []
