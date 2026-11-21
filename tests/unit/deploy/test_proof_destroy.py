"""The proof's destroy against dev's prevent_destroy holds, and a destroy that deletes nothing."""

from pathlib import Path

import pytest
import yaml

from tests.unit.deploy.proof_world import _destroys
from weather_signal.deploy import proof
from weather_signal.deploy.cli import Result

RUN = ["run", "--yes", "--profile", "p", "--seed-from", "x", "--out"]
ROOT = Path(__file__).parents[3]
HELD = "Error: resources.schemas.raw has lifecycle.prevent_destroy set\n"


def _holds(p, ws, lifted_by="dev_holds=false"):
    """`bundle destroy` refuses at plan time, deleting nothing, unless the argv lifts the holds."""
    inner = p.ctx.run

    def run(argv, stdin=None):
        a = list(argv)
        if a[1:3] == ["bundle", "destroy"] and (lifted_by is None or lifted_by not in a):
            ws.calls.append(a)
            return Result(1, "", HELD)
        return inner(a, stdin)

    p.ctx.run = run


def _said(p):
    lines = []
    p.ctx.say = lines.append
    return lines


def test_the_proof_destroy_gets_past_devs_holds(p, ws):
    _holds(p, ws)
    proof.main([*RUN, str(p.out)], make=lambda a: p)
    assert ws.state is None, "dev was destroyed"
    assert ws.streamed, "the bootstrap ran"
    argv = _destroys(ws)[0]
    assert argv[argv.index("-t") + 1] == "dev"


def test_devs_holds_are_a_variable_and_prods_stay_literal():
    doc = yaml.safe_load((ROOT / "databricks.yml").read_text(encoding="utf-8"))
    assert doc["variables"]["dev_holds"]["default"] is True
    dev = doc["targets"]["dev"]["resources"]
    for kind, key in (("schemas", "raw"), ("volumes", "raw_files"), ("volumes", "ledger")):
        assert dev[kind][key]["lifecycle"]["prevent_destroy"] == "${var.dev_holds}"
    for target in ("prod", "scratch"):
        assert "dev_holds" not in yaml.safe_dump(doc["targets"][target])
    prod = doc["targets"]["prod"]["resources"]
    assert all(v["lifecycle"]["prevent_destroy"] is True for v in prod["schemas"].values())
    assert all(v["lifecycle"]["prevent_destroy"] is True for v in prod["volumes"].values())


def test_a_destroy_that_deletes_nothing_closes_the_run_and_frees_a_fresh_one(p, ws, tmp_path):
    said = _said(p)
    _holds(p, ws, lifted_by=None)
    assert proof.main([*RUN, str(p.out)], make=lambda a: p) == 1
    assert ws.state is not None
    assert not proof.record.pointer(p).exists()
    line = next(x for x in said if "nothing was deleted" in x)
    assert f"--ledger-from {p.out / 'archive/ledger'}" in line and "--resume" not in line
    with pytest.raises(proof.Stop, match="closed when its destroy failed"):
        proof.run(p, resume=True)
    assert len(_destroys(ws)) == 1, "the resume destroyed nothing"
    p.out = tmp_path / "out2"
    assert proof.main([*RUN, str(p.out)], make=lambda a: p) == 1
    assert len(_destroys(ws)) == 2, "a fresh run was not refused"


def test_a_failed_destroy_that_lost_a_resource_stays_resumable(p, ws):
    inner = p.ctx.run

    def run(argv, stdin=None):
        a = list(argv)
        if a[1:3] == ["bundle", "destroy"]:
            ws.calls.append(a)
            ws.state["state"].pop("resources.jobs.score")
            return Result(1, "", "Error: app delete timed out\n")
        return inner(a, stdin)

    p.ctx.run = run
    said = _said(p)
    assert proof.main([*RUN, str(p.out)], make=lambda a: p) == 1
    assert proof.record.pending(p) == p.out
    assert not any("nothing was deleted" in x for x in said)
    assert any("--resume" in x for x in said if "To finish:" in x)
