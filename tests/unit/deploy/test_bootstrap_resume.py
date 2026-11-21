"""What proof-3's four resumes hit: a code-only change, a landed copy of the same request, and a
GitHub link without access to the repository."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from tests.unit.deploy.conftest import app_deployed
from weather_signal.deploy import phases, seed, wheel
from weather_signal.deploy.cli import Result
from weather_signal.ingest import envelope

ROOT = Path(__file__).parents[3]
VOL = "/Volumes/workspace/dev_raw/raw"
NO_ACCESS = (
    "Error: The link to your GitHub account does not have access. To fix this error:\n"
    "Next steps:\n  - Verify you have the required permissions for this operation\n"
)


def only(*names):
    return tuple(p for p in phases.PHASES if p.name in names)


def tree(root: Path, code: str = "X = 1\n") -> Path:
    (root / "src" / "weather_signal").mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text('[project]\nname = "weather-signal"\n', encoding="utf-8")
    (root / "src" / "weather_signal" / "mod.py").write_text(code, encoding="utf-8")
    return root


# --- the bundle phase and a code-only change


def test_the_tag_follows_the_wheels_sources_and_nothing_else(tmp_path):
    t = tree(tmp_path)
    first = wheel.tag(t)
    (t / "src" / "weather_signal" / "__pycache__").mkdir()
    (t / "src" / "weather_signal" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\0")
    (t / "README.md").write_text("docs", encoding="utf-8")
    assert wheel.tag(t) == first
    (t / "src" / "weather_signal" / "mod.py").write_text("X = 2\n", encoding="utf-8")
    assert wheel.tag(t) != first


@pytest.mark.parametrize(
    "rel",
    [
        "src/weather_signal_etl/transformations/pipeline.py",
        "app/ws_ui/main.py",
        "aibi/weather_signal.lvdash.json",
    ],
)
def test_the_tag_follows_every_tree_the_bundle_ships(tmp_path, rel):
    t = tree(tmp_path)
    (t / rel).parent.mkdir(parents=True, exist_ok=True)
    (t / rel).write_text("one\n", encoding="utf-8")
    first = wheel.tag(t)
    (t / rel).write_text("two\n", encoding="utf-8")
    assert wheel.tag(t) != first


def test_the_skip_rule_reads_paths_below_the_tree_not_above_it(tmp_path):
    t = tree(tmp_path / "x__pycache__" / "c.pyc")
    first = wheel.tag(t)
    (t / "src" / "weather_signal" / ".DS_Store").write_bytes(b"\0\1")
    assert wheel.tag(t) == first
    (t / "src" / "weather_signal" / "mod.py").write_text("X = 2\n", encoding="utf-8")
    assert wheel.tag(t) != first


def test_the_upload_folder_is_named_by_the_tag_bootstrap_passes():
    doc = yaml.safe_load((ROOT / "databricks.yml").read_text(encoding="utf-8"))
    assert doc["workspace"]["artifact_path"].endswith("/artifacts/${var.wheel_tag}")
    assert "wheel_tag" in doc["variables"]
    for name, target in doc["targets"].items():
        assert "artifact_path" not in (target.get("workspace") or {}), name


class Deployed:
    """A plan that, like the CLI's, sees no wheel bytes: only the library path the tag names."""

    def __init__(self, ws, tag):
        self.ws, self.tag, self.deploys = ws, tag, 0

    @staticmethod
    def passed(a):
        tags = [x.split("=", 1)[1] for x in a if x.startswith("wheel_tag=")]
        return tags[-1] if tags else None

    def __call__(self, argv, stdin=None):
        a, verb = list(argv), " ".join(argv[1:3])
        if verb == "bundle deploy":
            self.deploys += 1
            self.tag = self.passed(a) or self.tag
        if verb == "bundle plan":
            moved = self.passed(a) not in (None, self.tag)
            action = "update" if moved else "skip"
            self.ws.plan = {"plan": {"resources.jobs.ingest": {"action": action}}}
        return self.ws(argv, stdin)


def test_a_code_only_change_redeploys_through_the_bundle_phase(ctx, ws, tmp_path):
    ctx.repo = tree(tmp_path / "repo")
    ctx.run = run = Deployed(ws, wheel.tag(ctx.repo))
    (ctx.repo / "src" / "weather_signal" / "mod.py").write_text("X = 2\n", encoding="utf-8")
    ctx.wheel_tag = ""
    assert phases.drive(ctx, only("bundle")) == 0
    assert run.deploys == 1 and run.tag == wheel.tag(ctx.repo)
    assert ctx.lines[0] == "bundle: update resources.jobs.ingest" and ctx.lines[-1] == "bundle: ran"


def test_the_same_code_does_not_redeploy(ctx, ws, tmp_path):
    ctx.repo = tree(tmp_path / "repo")
    ctx.run = run = Deployed(ws, wheel.tag(ctx.repo))
    assert phases.drive(ctx, only("bundle")) == 0
    assert run.deploys == 0 and ctx.lines == ["bundle: done"]


def test_an_app_edit_redeploys_the_app_once(ctx, ws, tmp_path):
    ctx.repo = tree(tmp_path / "repo")
    (ctx.repo / "app").mkdir()
    (ctx.repo / "app" / "main.py").write_text("v = 1\n", encoding="utf-8")
    app_deployed(ctx)
    (ctx.repo / "app" / "main.py").write_text("v = 2\n", encoding="utf-8")
    assert phases.drive(ctx, only("app")) == 0
    assert [a[1:4] for a in ws.writes()] == [["bundle", "run", "ui_app"]]
    ctx.lines.clear()
    assert phases.drive(ctx, only("app")) == 0
    assert ctx.lines == ["app: done"] and len(ws.writes()) == 1


# --- the seed and a landed copy of the same request


def landed(params, body, *, slot="2024-01-10..2024-01-23", source="open-meteo-previous-runs"):
    """An envelope as the ingest job lands it, and its content-addressed path below the volume."""
    endpoint = "https://previous-runs-api.open-meteo.com/v1/forecast"
    key = envelope.landing_key(source, endpoint, params, slot)
    env = {
        "source": source,
        "request": {"endpoint": endpoint, "params": params},
        "slot": slot,
        "fetched_at": datetime(2026, 11, 20, tzinfo=UTC).isoformat(),
        "http_status": 200,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_text": body.decode(),
    }
    return f"{source}/2024/01/10/00/{key}.json.gz", envelope.encode(env)


PARAMS = {"latitude": "40.7", "start_date": "2024-01-10", "end_date": "2024-01-23"}


@pytest.fixture
def archived(ctx, tmp_path):
    """One archive file; `land(data)` puts `data` at its path in the raw volume."""
    path, data = landed(PARAMS, b'{"hourly":[1.0]}')
    root = tmp_path / "seed"
    (root / "raw" / path).parent.mkdir(parents=True)
    (root / "raw" / path).write_bytes(data)
    ctx.seed_from = root
    ctx.seed = [seed.Entry(path, len(data), hashlib.sha256(data).hexdigest())]

    def land(other: bytes) -> str:
        ctx.store.files[f"{VOL}/{path}"] = other
        ctx.list_volume = lambda r: {path: len(other)} if r == VOL else {}
        return path

    return land


def test_a_landed_copy_of_the_same_request_is_kept_and_said(ctx, archived):
    path = archived(landed(PARAMS, b'{"hourly":[1.25]}')[1])
    before = ctx.store.files[f"{VOL}/{path}"]
    assert phases.drive(ctx, only("seed")) == 0
    assert ctx.store.files[f"{VOL}/{path}"] == before and f"{VOL}/{path}" not in ctx.store.puts
    assert ctx.lines[0].startswith(f"seed: kept {VOL}/{path}: landed copy of the same request")
    assert ctx.lines[-1] == "seed: done"


@pytest.mark.parametrize(
    "other",
    [
        pytest.param(landed({**PARAMS, "latitude": "41.0"}, b"{}")[1], id="another request"),
        pytest.param(b"not an envelope", id="not an envelope"),
        pytest.param(envelope.encode({"source": "open-meteo-previous-runs"}), id="no request"),
    ],
)
def test_a_different_file_at_the_path_still_stops_and_is_left(ctx, archived, other):
    path = archived(other)
    assert phases.drive(ctx, only("seed")) == 1
    assert ctx.store.files[f"{VOL}/{path}"] == other and ctx.store.puts == []
    assert "is not a copy of the same request" in ctx.lines[-1]


def test_a_bit_flipped_landed_gzip_stops_with_the_remove_line(ctx, archived):
    flipped = bytearray(landed(PARAMS, b'{"hourly":[1.25]}' * 40)[1])
    for i in range(20, 60):
        flipped[i] ^= 0xFF
    path = archived(bytes(flipped))
    assert phases.drive(ctx, only("seed")) == 1
    assert f"databricks fs rm dbfs:{VOL}/{path}" in ctx.lines[-1] and ctx.store.puts == []


def test_a_landed_copy_with_a_broken_body_hash_still_stops(ctx, archived):
    env = envelope.decode(landed(PARAMS, b'{"hourly":[1.25]}')[1])
    archived(envelope.encode({**env, "body_text": '{"hourly":[9]}'}))
    assert phases.drive(ctx, only("seed")) == 1
    assert ctx.store.puts == []


def test_a_landed_copy_that_does_not_read_back_is_unknown(ctx, archived):
    path = archived(landed(PARAMS, b'{"hourly":[1.25]}')[1])
    ctx.store.unreadable.add(f"{VOL}/{path}")
    assert phases.drive(ctx, only("seed")) == 1
    assert "a probe did not answer" in ctx.lines[-1] and ctx.store.puts == []


# --- gitfolder and a GitHub link without access


def test_a_link_without_access_to_the_repository_skips_and_bootstrap_goes_on(ctx, ws):
    ws.git_folder = None
    ws.rc["repos create"] = Result(1, "", NO_ACCESS)
    assert phases.drive(ctx, only("gitfolder")) == 0
    assert ctx.lines[-2].startswith("gitfolder: skipped: repos create says the GitHub")
    assert ctx.lines[-1] == "gitfolder: ran"


def test_an_update_without_access_skips_too(ctx, ws):
    ws.git_folder["head_commit_id"] = "0ld"
    ws.rc["repos update"] = Result(1, "", NO_ACCESS)
    assert phases.drive(ctx, only("gitfolder")) == 0
    assert "gitfolder: skipped: repos update says" in ctx.lines[-2]


def test_any_other_create_failure_still_stops(ctx, ws):
    ws.git_folder = None
    ws.rc["repos create"] = Result(1, "", "Error: Internal error\n")
    assert phases.drive(ctx, only("gitfolder")) == 1
    assert ctx.lines[-1].startswith("gitfolder: stopped: repos create: exit 1")
