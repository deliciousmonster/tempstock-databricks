"""A gzipped envelope read the pipeline's way passes the hash check and parses to VARIANT."""

from pyspark.sql import functions as F
from pyspark.sql import types as T

from tests.conftest import fixture_bytes, utc
from weather_signal.ingest import envelope
from weather_signal.ingest.fetch import RawResponse
from weather_signal.pipeline import transforms


def _land(tmp_path, body_bytes, tamper=False):
    resp = RawResponse(200, {"X-RateLimit-Cost": "50"}, body_bytes, utc(2026, 11, 17, 18, 7))
    env = envelope.build("open-meteo-previous-runs", "e", {"a": "1"}, "s", resp, host="h")
    if tamper:
        env["body_text"] = env["body_text"].replace("1", "2", 1)
    p = tmp_path / f"{'t' if tamper else 'k'}.json.gz"
    p.write_bytes(envelope.encode(env))
    return env


def test_envelope_hash_check_and_variant(spark, tmp_path):
    good = _land(tmp_path, fixture_bytes("prevruns-1807.json"))
    _land(tmp_path, fixture_bytes("prevruns-1807.json"), tamper=True)
    df = spark.read.schema(transforms.ENVELOPE_SCHEMA).json(str(tmp_path / "*.json.gz"))
    out = transforms.raw(df, F.col("_metadata.file_path")).orderBy("path").collect()
    assert [r["sha_ok"] for r in out] == [True, False]
    k = out[0]
    assert k["body_text"] == good["body_text"]
    schema = transforms.raw(df, F.col("_metadata.file_path")).schema
    assert isinstance(schema["body"].dataType, T.VariantType)
    assert isinstance(schema["body_text"].dataType, T.StringType)
    lat = (
        transforms.raw(df, F.col("_metadata.file_path"))
        .where("sha_ok")
        .select(F.expr("variant_get(body, '$.latitude', 'double')").alias("lat"))
        .first()["lat"]
    )
    assert abs(lat - 40.71) < 0.01
