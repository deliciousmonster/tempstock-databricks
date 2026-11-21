"""Keep the Alpha Vantage key out of envelopes, logs and exception messages."""

from __future__ import annotations

from collections.abc import Mapping

CREDENTIAL_PARAMS = frozenset({"apikey", "api_key", "token"})


def strip_credentials(params: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in params.items() if k.lower() not in CREDENTIAL_PARAMS}


def redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[redacted]")
    return text
