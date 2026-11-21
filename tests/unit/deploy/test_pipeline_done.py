"""The pipeline's done reads its table list from the pipeline source, never from a copy."""

from weather_signal.deploy import phases

from .conftest import REPO


def test_the_list_is_every_fq_table_in_the_pipeline_source():
    got = phases.pipeline_tables(REPO)
    assert ("raw", "raw_envelopes") in got and ("signals", "forecast_index") in got
    assert ("daily", "prices_daily") in got and len(got) >= 15


def test_one_missing_table_is_not_done(ctx, ws):
    ws.tables["signals"].remove("normals")
    assert phases.pipeline_done(ctx) is phases.State.NOT_DONE


def test_every_table_present_is_done(ctx):
    assert phases.pipeline_done(ctx) is phases.State.DONE


def test_a_table_added_to_the_source_is_required(ctx, tmp_path):
    src = tmp_path / phases.PIPELINE_SOURCE
    src.parent.mkdir(parents=True)
    body = (REPO / phases.PIPELINE_SOURCE).read_text(encoding="utf-8")
    src.write_text(body + '\n@dlt.table(name=fq("signals", "brand_new"))\n', encoding="utf-8")
    ctx.repo = tmp_path
    assert phases.pipeline_done(ctx) is phases.State.NOT_DONE
