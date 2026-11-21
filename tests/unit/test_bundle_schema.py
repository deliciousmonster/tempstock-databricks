"""The bundle against the saved schema, and the grants, run_as, lifecycle and wheel it must carry."""

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from weather_signal.catalog.setup import grants
from weather_signal.config import Names

ROOT = Path(__file__).parents[2]
RUNNER, AGENT = "runner-app-id", "agent-app-id"


def _merge(a, b):
    for k, v in b.items():
        a[k] = _merge(a.get(k, {}), v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return a


@pytest.fixture(scope="module")
def bundle():
    doc = yaml.safe_load((ROOT / "databricks.yml").read_text())
    for f in sorted((ROOT / "resources").glob("*.yml")):
        _merge(doc, yaml.safe_load(f.read_text()))
    return doc


def _resolve(doc):
    """Put each complex variable's value where `${var.<name>}` stands alone, as the CLI does."""
    import copy

    values = {k: v["default"] for k, v in doc["variables"].items() if v.get("type") == "complex"}

    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, str) and o.startswith("${var.") and o[6:-1] in values:
            return copy.deepcopy(values[o[6:-1]])
        return o

    return {k: (v if k == "variables" else walk(v)) for k, v in doc.items()}


def _effective(bundle, target):
    """The target's view of the bundle, overrides merged, as the CLI resolves it."""
    import copy

    out = copy.deepcopy({k: v for k, v in bundle.items() if k != "targets"})
    t = copy.deepcopy(bundle["targets"][target])
    t.pop("default", None)
    for k, v in t.pop("variables", {}).items():
        out["variables"][k]["default"] = v
    return _resolve(_merge(out, t))


@pytest.mark.parametrize("target", ["dev", "scratch", "prod"])
def test_validates_against_the_saved_schema(bundle, target):
    # `databricks bundle schema` from CLI v1.26.0, saved so CI needs no CLI.
    text = (ROOT / "tests/fixtures/bundle-schema.json").read_text()
    # Python re lacks \\p{L} and \\p{N}; the classes below stand in for them.
    # A class of both comes first: substituted inside brackets, each would nest a set.
    text = text.replace("[\\\\p{L}\\\\p{N}]", "[^\\\\W_]")
    text = text.replace("\\\\p{L}", "[^\\\\W\\\\d_]").replace("\\\\p{N}", "\\\\d")
    validator = jsonschema.Draft202012Validator(json.loads(text))
    errors = list(validator.iter_errors(_effective(bundle, target)))
    assert not errors, [e.message[:200] for e in errors[:5]]
    assert not list(validator.iter_errors(_resolve({**bundle, "targets": {}})))


def test_prod_run_as_permissions_and_health(bundle):
    prod = bundle["targets"]["prod"]
    assert prod["mode"] == "production"
    assert "run_as" not in prod  # an App refuses it; each job and the pipeline set their own
    runner = {"service_principal_name": "${var.runner}"}
    for key in ("ingest", "score", "backtest", "ui_snapshot"):
        assert prod["resources"]["jobs"][key]["run_as"] == runner, key
    assert prod["resources"]["pipelines"]["weather_signal"]["run_as"] == runner
    assert set(prod["resources"]["jobs"]) == set(bundle["resources"]["jobs"])
    assert prod["permissions"]
    for job in bundle["resources"]["jobs"].values():
        assert job["health"]["rules"][0]["metric"] == "RUN_DURATION_SECONDS"
        assert job["max_concurrent_runs"] == 1 and job["performance_target"] == "STANDARD"
        assert all(t["timeout_seconds"] > 0 for t in job["tasks"])


@pytest.mark.parametrize(
    ("target", "status"), [("dev", "UNPAUSED"), ("scratch", "PAUSED"), ("prod", "PAUSED")]
)
def test_each_schedule_takes_the_target_pause_status_and_no_preset_overrides_it(
    bundle, target, status
):
    """dev stays live across a later deploy; bootstrap passes PAUSED until its live phase."""
    eff = _effective(bundle, target)
    assert eff["variables"]["schedule_pause"]["default"] == status
    assert "trigger_pause_status" not in eff.get("presets", {})
    scheduled = {k: j for k, j in eff["resources"]["jobs"].items() if "schedule" in j}
    assert set(scheduled) == {"ingest", "score", "train", "ui_snapshot"}
    assert all(j["schedule"]["pause_status"] == "${var.schedule_pause}" for j in scheduled.values())
    assert eff.get("mode") != "production" or target == "prod"


def test_prod_trains_as_the_model_owner_and_scores_as_the_runner(bundle):
    """A version and @champion need the model's owner (UC); the runner holds EXECUTE only."""
    jobs = _effective(bundle, "prod")["resources"]["jobs"]
    assert jobs["train"]["run_as"] == {"user_name": "${var.owner}"}
    assert (
        jobs["score"]["run_as"]
        == jobs["ingest"]["run_as"]
        == {"service_principal_name": "${var.runner}"}
    )
    assert all("run_as" not in _effective(bundle, t)["resources"]["jobs"]["train"]
               for t in ("dev", "scratch"))  # fmt: skip


def _runner_grants(bundle):
    out = {}
    r = _resolve(bundle)["resources"]
    for kind in ("schemas", "volumes", "registered_models"):
        for key, res in r[kind].items():
            for g in res.get("grants", []):
                if g["principal"] == "${var.runner}":
                    out[(kind, key)] = set(g["privileges"])
    return out


def test_job_principal_grants(bundle):
    g = _runner_grants(bundle)
    for s in ("raw", "daily", "signals"):
        assert {"CREATE_TABLE", "CREATE_MATERIALIZED_VIEW"} <= g[("schemas", s)]
    assert g[("volumes", "raw_files")] == {"READ_VOLUME", "WRITE_VOLUME"}
    assert g[("registered_models", "stock_signal")] == {"EXECUTE"}
    assert not any("MODIFY" in v for (k, s), v in g.items() if s == "signals")
    sql = grants(Names("workspace", ""), RUNNER, AGENT)
    assert not any("recommendations" in q and RUNNER in q for q in sql)
    assert f"GRANT MODIFY ON TABLE workspace.signals.signal TO `{RUNNER}`" in sql


def test_runner_may_rewrite_a_backtest_table_setup_created():
    sql = grants(Names("workspace", ""), RUNNER, AGENT)
    assert f"GRANT MODIFY ON TABLE workspace.signals.signal_backtest TO `{RUNNER}`" in sql
    assert not any("signal_backtest" in q and AGENT in q and "MODIFY" in q for q in sql)


def test_agent_principal_grants_only_its_tables():
    sql = [q for q in grants(Names("workspace", ""), RUNNER, AGENT) if AGENT in q]
    modify = [q for q in sql if "MODIFY" in q]
    assert sorted(modify) == sorted(
        [
            f"GRANT MODIFY ON TABLE workspace.signals.{t} TO `{AGENT}`"
            for t in ("recommendations", "agent_runs")
        ]
    )
    assert not any(s in q for q in sql for s in (".ops", ".raw", ".daily", "VOLUME"))


def test_one_principal_fallback_has_no_agent_grants():
    sql = grants(Names("workspace", ""), RUNNER, None)
    assert not any(AGENT in q for q in sql)
    assert f"GRANT MODIFY ON TABLE workspace.signals.recommendations TO `{RUNNER}`" in sql


def test_prod_prevent_destroy_on_schemas_and_volumes(bundle):
    prod = bundle["targets"]["prod"]["resources"]
    for kind, keys in (
        ("schemas", ("raw", "daily", "signals", "app", "ops")),
        ("volumes", ("raw_files", "ledger", "ui")),
    ):
        for k in keys:
            assert prod[kind][k]["lifecycle"]["prevent_destroy"] is True, (kind, k)


def test_wheel_artifact_and_environments(bundle):
    assert bundle["artifacts"]["weather_signal"]["type"] == "whl"
    for name, job in bundle["resources"]["jobs"].items():
        env = job["environments"][0]
        assert env["spec"]["environment_version"] == "4"
        assert "../dist/*.whl" in env["spec"]["dependencies"], name
        for t in job["tasks"]:
            if "python_wheel_task" in t:
                assert t["environment_key"] == env["environment_key"]
    assert (
        "../dist/*.whl"
        in bundle["resources"]["pipelines"]["weather_signal"]["environment"]["dependencies"]
    )


@pytest.mark.parametrize("target", ["dev", "scratch"])
def test_dev_pipeline_is_development_and_prod_is_not(bundle, target):
    # The CLI copies presets.pipelines_development onto each pipeline (read from validate's JSON).
    eff = _effective(bundle, target)
    assert eff["presets"]["pipelines_development"] is True
    assert "development" not in eff["resources"]["pipelines"]["weather_signal"]
    prod = _effective(bundle, "prod")
    assert not prod.get("presets", {}).get("pipelines_development", False)
    assert not prod["resources"]["pipelines"]["weather_signal"].get("development", False)


def test_dev_runs_the_backtest_as_the_runner_so_its_grants_are_exercised(bundle):
    dev = _effective(bundle, "dev")
    assert dev["resources"]["jobs"]["backtest"]["run_as"] == {
        "service_principal_name": "${var.runner}"
    }
    assert "run_as" not in dev["resources"]["jobs"]["score"]


@pytest.mark.parametrize("target", ["dev", "scratch", "prod"])
def test_the_runner_may_read_the_bundle_root_where_it_runs(bundle, target):
    # A job run as the runner installs the wheel from the bundle root; without this it cannot read it.
    perms = _effective(bundle, target)["permissions"]
    assert {"service_principal_name": "${var.runner}", "level": "CAN_VIEW"} in perms
    # The deploying owner is named, so validate warns of no grant the bundle leaves unstated.
    assert {"user_name": "${var.owner}", "level": "CAN_MANAGE"} in perms


def test_the_secret_scope_takes_its_grants_from_the_target():
    """The runner's CAN_VIEW becomes READ on the scope; an explicit READ is the duplicate validate
    warns of."""
    bundle = yaml.safe_load((ROOT / "resources/catalog.yml").read_text(encoding="utf-8"))
    assert "permissions" not in bundle["resources"]["secret_scopes"]["alphavantage"]


def test_no_volume_or_setting_names_a_seed_the_pipeline_never_reads(bundle):
    """The seed CSVs ship in the wheel; a `seed` volume or `weather_signal.seed` key is dead."""
    assert "seed" not in bundle["resources"]["volumes"]
    conf = bundle["resources"]["pipelines"]["weather_signal"]["configuration"]
    assert not any(k.endswith(".seed") for k in conf)


def test_every_job_and_pipeline_is_tagged_with_the_project(bundle):
    assert bundle["presets"]["tags"] == {"project": "weather-signal"}


def test_the_ingest_job_takes_catch_up_as_a_job_parameter_off_by_default(bundle):
    job = bundle["resources"]["jobs"]["ingest"]
    assert {"name": "catch_up", "default": "false"} in job["parameters"]
    params = job["tasks"][0]["python_wheel_task"]["parameters"]
    assert params[params.index("--catch-up") + 1] == "{{job.parameters.catch_up}}"


def test_the_bundle_declares_no_genie_space():
    """Genie refuses a space over views that do not exist, and the first deploy comes before them."""
    for f in [ROOT / "databricks.yml", *sorted((ROOT / "resources").glob("*.yml"))]:
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        assert "genie_spaces" not in (doc.get("resources") or {}), f
        for t in (doc.get("targets") or {}).values():
            assert "genie_spaces" not in ((t or {}).get("resources") or {}), f
