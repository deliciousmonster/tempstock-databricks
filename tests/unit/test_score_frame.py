"""`score` refuses a frame with two rows for one key before any MERGE."""

from datetime import date

import pytest

from tests.conftest import utc
from weather_signal.signal import score


def _row(t):
    return {"as_of": utc(2026, 12, 2, 12, 30), "target_date": t}


def test_duplicate_key_fails():
    with pytest.raises(score.DuplicateKey):
        score.assert_unique(
            [_row(date(2026, 12, 3)), _row(date(2026, 12, 4)), _row(date(2026, 12, 3))]
        )


def test_unique_frame_passes():
    score.assert_unique([_row(date(2026, 12, 3)), _row(date(2026, 12, 4))])


def test_merge_is_insert_only():
    sql = score.merge_sql("g.signal", "src")
    assert "WHEN NOT MATCHED THEN INSERT" in sql and "UPDATE" not in sql and "DELETE" not in sql
