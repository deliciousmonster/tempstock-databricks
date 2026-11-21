"""A live row has the snapshot's types and timestamp shape, so it can be drilled into."""

from __future__ import annotations

import re


def test_live_rows_are_typed_and_timestamps_match_the_routes(client):
    _, main, _ = client
    from ws_ui.source import typed

    cols = [
        ("as_of", "TIMESTAMP"),
        ("target_date", "DATE"),
        ("horizon_days", "INT"),
        ("score", "DOUBLE"),
        ("reasons", "ARRAY"),
        ("open_incident", "BOOLEAN"),
        ("note", "STRING"),
    ]
    row = [
        "2014-04-02T07:30:00.000Z",
        "2014-04-02",
        "0",
        "0.0",
        '["stale_forecast"]',
        "false",
        None,
    ]
    [r] = typed(cols, [row])
    assert r == {
        "as_of": "2014-04-02T07:30:00Z",
        "target_date": "2014-04-02",
        "horizon_days": 0,
        "score": 0.0,
        "reasons": ["stale_forecast"],
        "open_incident": False,
        "note": None,
    }
    assert re.fullmatch(main.AS_OF, r["as_of"])
