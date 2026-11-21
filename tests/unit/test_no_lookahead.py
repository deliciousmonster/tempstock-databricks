"""`visible` drops rows known after `as_of`; a forged row raises `LookaheadError`."""

import pandas as pd
import pytest

from tests.conftest import utc
from weather_signal import asof


def _df():
    return pd.DataFrame(
        {
            "k": ["a", "a", "b"],
            "known_at": pd.to_datetime(
                [utc(2026, 11, 18, 1), utc(2026, 11, 18, 3), utc(2026, 11, 18, 2)]
            ),
            "x": [1, 2, 3],
        }
    )


def test_visible_excludes_future_rows():
    v = asof.visible(_df(), utc(2026, 11, 18, 2), ["k"])
    assert sorted(v["x"]) == [1, 3]
    assert asof.visible(_df(), utc(2026, 11, 18, 0), ["k"]).empty


def test_equal_known_at_is_visible():
    assert len(asof.visible(_df(), utc(2026, 11, 18, 1), ["k"])) == 1


def test_forged_row_raises():
    with pytest.raises(asof.LookaheadError):
        asof.assert_no_lookahead([{"known_at": utc(2026, 11, 18, 3)}], utc(2026, 11, 18, 2))
    asof.assert_no_lookahead([{"known_at": utc(2026, 11, 18, 2)}], utc(2026, 11, 18, 2))
