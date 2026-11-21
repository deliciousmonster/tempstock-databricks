"""`put_if_absent` on the real volume: three racing writers, one `created` per key; 24 ledger slots."""

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from tests.integration.conftest import PROFILE
from weather_signal.ingest import av_ledger as av
from weather_signal.ingest.files import Files

KEYS, WRITERS, RUNS = 60, 3, 3


def _race(root: str) -> Counter:
    files = [Files.from_sdk(PROFILE) for _ in range(WRITERS)]
    out: Counter = Counter()
    lock = threading.Lock()

    def writer(w: int) -> None:
        for k in range(KEYS):
            barriers[k].wait(timeout=120)
            o = files[w].put_if_absent(f"{root}/{k:03d}.json.gz", f"writer {w}".encode())
            with lock:
                out[(k, o)] += 1

    barriers = [threading.Barrier(WRITERS) for _ in range(KEYS)]
    with ThreadPoolExecutor(WRITERS) as ex:
        list(ex.map(writer, range(WRITERS)))
    return out


@pytest.mark.parametrize("run", range(RUNS))
def test_one_created_per_key(n, run_id, run):
    # The ledger volume, not raw: Auto Loader must not ingest these.
    got = _race(f"{n.ledger}/_it/{run_id}/race{run}")
    for k in range(KEYS):
        assert got[(k, "created")] == 1, k
        assert got[(k, "exists")] == WRITERS - 1, k
    assert not [key for key in got if key[1] == "unknown"]


def test_24_slots_and_no_25th(n, run_id):
    ledger = av.Ledger(f"{n.ledger}/_it/{run_id}", Files.from_sdk(PROFILE))
    day = date(2026, 1, 1)
    assert [ledger.claim(day, h, {"h": h}) for h in range(24)] == ["created"] * 24
    assert [ledger.claim(day, h, {"h": h}) for h in range(24)] == ["exists"] * 24
    with pytest.raises(av.SlotRefused):
        ledger.claim(day, 24, {"h": 24})
