"""The metros seed: 50 rows, weights summing to 1, rank 50 Hartford, every point located."""

import pandas as pd
import pytest

from tests.conftest import SEED


@pytest.fixture(scope="module")
def m():
    return pd.read_csv(SEED / "metros.csv", dtype={"cbsa_code": str})


def test_fifty_rows_weights_sum_to_one(m):
    assert len(m) == 50 and m["cbsa_code"].is_unique
    assert m["weight"].sum() == pytest.approx(1.0, abs=1e-4)


def test_rank_50_is_hartford(m):
    assert m.loc[m["rank"] == 50, "cbsa_name"].iloc[0].startswith("Hartford")


def test_every_metro_has_a_point_including_nashville(m):
    assert m["latitude"].notna().all() and m["longitude"].notna().all()
    nash = m[m["cbsa_name"].str.startswith("Nashville")].iloc[0]
    assert nash["point_place"].startswith("Nashville-Davidson")
