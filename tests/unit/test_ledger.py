"""The Open-Meteo ledger holds a send until every ceiling has room for it."""

from tests.conftest import utc
from weather_signal.ingest.om_budget import Ceilings, Ledger


def test_day_ceiling_defers_to_next_utc_midnight():
    led = Ledger([(utc(2026, 11, 18, h), 990.0) for h in range(2, 9)])
    now = utc(2026, 11, 18, 12)
    assert led.earliest_send(now, 100.0, Ceilings()) == utc(2026, 11, 19)
    assert led.earliest_send(now, 60.0, Ceilings()) == now
