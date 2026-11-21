"""aibi/weather_signal.genie.json, which bootstrap's `genie` phase renders, reads only the six views."""

from __future__ import annotations

import json
from pathlib import Path

from weather_signal.serving import views
from weather_signal.signal import checks

ROOT = Path(__file__).resolve().parents[3]


def test_the_space_names_only_ui_views():
    space = json.loads((ROOT / "aibi" / "weather_signal.genie.json").read_text(encoding="utf-8"))
    tables = [t["identifier"] for t in space["data_sources"]["tables"]]
    assert tables == sorted(tables)
    assert [t.rsplit(".", 1)[1] for t in tables] == sorted(views.VIEWS)
    assert all(t.startswith("${catalog}.${app_schema}.") for t in tables)


def test_the_next_seven_days_include_today():
    """Genie answered the sample with `target_date > CURRENT_DATE`, dropping horizon 0."""
    space = json.loads((ROOT / "aibi" / "weather_signal.genie.json").read_text(encoding="utf-8"))
    text = "".join(space["instructions"]["text_instructions"][0]["content"])
    assert "today (horizon 0)" in text and "target_date > CURRENT_DATE" in text
    samples = {q for s in space["config"]["sample_questions"] for q in s["question"]}
    examples = {
        e["question"][0]: "".join(e["sql"]) for e in space["instructions"]["example_question_sqls"]
    }
    for q in samples & examples.keys():
        assert "CURRENT_DATE" not in examples[q]
    assert "What did the agent call for each of the next seven days?" in examples


def test_the_space_says_the_fill_shape_row_is_not_rows_checked():
    """Genie reads v_ui_sources raw, fill_shape row included."""
    space = json.loads((ROOT / "aibi" / "weather_signal.genie.json").read_text(encoding="utf-8"))
    text = "".join(space["instructions"]["text_instructions"][0]["content"])
    assert "kind = 'fill_shape'" in text and "not rows checked" in text
    assert f"last {checks.FILL_ALERT_WINDOW.days} days" in text
