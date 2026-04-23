"""Unit tests for the credential manager.

Covers:
  - Canonical env var naming.
  - Legacy env var alias fallback.
  - None when everything is unset.
  - list_missing catalog behaviour.
  - Redaction by key name AND by value match.
  - Graceful degradation when `keyring` is unavailable.
"""

from __future__ import annotations

import pytest

from tools import credentials as creds


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip every CALLISTO_* + legacy alias env var so tests start clean."""
    for key in list(__import__("os").environ.keys()):
        if key.startswith("CALLISTO_"):
            monkeypatch.delenv(key, raising=False)
    for legacy in (
        "ODDS_API_IO_KEY", "ODDSAPI_IO_KEY",
        "ODDS_API_KEY", "THE_ODDS_API_KEY",
        "BRAVE_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "ACTION_NETWORK_KEY",
    ):
        monkeypatch.delenv(legacy, raising=False)
    yield


def test_env_var_name_format():
    assert creds.env_var_name("fanatics", "session_cookie") == "CALLISTO_FANATICS_SESSION_COOKIE"
    assert creds.env_var_name("draftkings", "API_KEY") == "CALLISTO_DRAFTKINGS_API_KEY"


def test_env_var_name_rejects_empty():
    with pytest.raises(ValueError):
        creds.env_var_name("", "password")
    with pytest.raises(ValueError):
        creds.env_var_name("fanatics", "")


def test_get_credential_missing_returns_none():
    assert creds.get_credential("fanatics", creds.FIELD_SESSION_COOKIE) is None
    assert creds.get_credential("draftkings", creds.FIELD_PASSWORD) is None


def test_get_credential_canonical_env_var(monkeypatch):
    monkeypatch.setenv("CALLISTO_FANATICS_SESSION_COOKIE", "sessabc123xyz")
    assert creds.get_credential("fanatics", creds.FIELD_SESSION_COOKIE) == "sessabc123xyz"


def test_get_credential_accepts_mixed_case_inputs(monkeypatch):
    monkeypatch.setenv("CALLISTO_DRAFTKINGS_USERNAME", "marco@example.com")
    assert creds.get_credential("DraftKings", "username") == "marco@example.com"
    assert creds.get_credential("draftkings", "USERNAME") == "marco@example.com"


def test_legacy_alias_fallback(monkeypatch):
    # Canonical unset, legacy set — legacy wins.
    monkeypatch.setenv("ODDS_API_IO_KEY", "legacy-token-123")
    assert creds.get_credential("odds_api_io", "api_key") == "legacy-token-123"


def test_canonical_beats_legacy(monkeypatch):
    monkeypatch.setenv("CALLISTO_ODDS_API_IO_API_KEY", "canonical-456")
    monkeypatch.setenv("ODDS_API_IO_KEY", "legacy-wrong")
    assert creds.get_credential("odds_api_io", "api_key") == "canonical-456"


def test_has_credential(monkeypatch):
    assert creds.has_credential("fanatics", "session_cookie") is False
    monkeypatch.setenv("CALLISTO_FANATICS_SESSION_COOKIE", "abc")
    assert creds.has_credential("fanatics", "session_cookie") is True


def test_list_missing_full_catalog():
    missing = creds.list_missing()
    assert "CALLISTO_FANATICS_SESSION_COOKIE" in missing
    assert "CALLISTO_DRAFTKINGS_USERNAME" in missing
    assert "CALLISTO_ODDS_API_IO_API_KEY" in missing


def test_list_missing_filters_set(monkeypatch):
    monkeypatch.setenv("CALLISTO_FANATICS_SESSION_COOKIE", "xxx")
    missing = creds.list_missing(["fanatics"])
    assert "CALLISTO_FANATICS_SESSION_COOKIE" not in missing
    # But other fanatics fields still missing:
    assert "CALLISTO_FANATICS_USERNAME" in missing


def test_list_missing_respects_override():
    missing = creds.list_missing(
        required_fields={"fanatics": (creds.FIELD_API_KEY,)}
    )
    assert missing == ["CALLISTO_FANATICS_API_KEY"]


# --- Redaction ----------------------------------------------------------

def test_redact_sensitive_key_names():
    d = {
        "user": "marco",
        "password": "hunter2",
        "session_cookie": "abcdef",
        "auth_token": "xyz",
        "auth_status": "ok",  # safe substring
        "normal_field": 5,
    }
    out = creds.redact_in_logs(d)
    assert out["user"] == "marco"
    assert out["password"] == "***"
    assert out["session_cookie"] == "***"
    assert out["auth_token"] == "***"
    assert out["auth_status"] == "ok"  # NOT redacted
    assert out["normal_field"] == 5


def test_redact_nested_structures(monkeypatch):
    monkeypatch.setenv("CALLISTO_FANATICS_SESSION_COOKIE", "supersecretvaluelong")
    payload = {
        "ok": True,
        "steps": [
            {"msg": "fetched url with token=supersecretvaluelong", "count": 3},
            {"password": "raw", "safe": 1},
        ],
    }
    out = creds.redact_in_logs(payload)
    assert "supersecretvaluelong" not in out["steps"][0]["msg"]
    assert "***" in out["steps"][0]["msg"]
    assert out["steps"][1]["password"] == "***"
    assert out["steps"][1]["safe"] == 1


def test_redact_string_replaces_live_creds(monkeypatch):
    monkeypatch.setenv("CALLISTO_DRAFTKINGS_SESSION_COOKIE", "dkcookieABCDEF12345")
    s = "Calling DK with Cookie=dkcookieABCDEF12345; Expires=..."
    redacted = creds.redact_in_logs(s)
    assert "dkcookieABCDEF12345" not in redacted
    assert "***" in redacted


def test_redact_ignores_short_values(monkeypatch):
    # Values shorter than 6 chars shouldn't trigger a global replace
    monkeypatch.setenv("CALLISTO_DRAFTKINGS_USERNAME", "abc")
    s = "the quick brown abc fox jumps"
    out = creds.redact_in_logs(s)
    # The literal "abc" in the sentence must survive
    assert out == s


def test_redact_preserves_non_str_scalars():
    assert creds.redact_in_logs(42) == 42
    assert creds.redact_in_logs(None) is None
    assert creds.redact_in_logs(True) is True


def test_redact_does_not_mutate_input():
    d = {"password": "abc"}
    out = creds.redact_in_logs(d)
    assert d == {"password": "abc"}  # Original untouched
    assert out == {"password": "***"}


# --- Keyring graceful fallback ------------------------------------------

def test_keyring_disabled_via_env(monkeypatch):
    """CALLISTO_DISABLE_KEYRING=1 short-circuits the keychain path and we
    still work if env vars are the source."""
    monkeypatch.setenv("CALLISTO_DISABLE_KEYRING", "1")
    monkeypatch.setenv("CALLISTO_PINNACLE_API_KEY", "pk-token-abc")
    # get_credential doesn't re-read _keyring_disabled on the fly (set at
    # import time) — that's deliberate: the disable flag is for deploys.
    # We still exercise the env-var path which is independent.
    assert creds.get_credential("pinnacle", "api_key") == "pk-token-abc"


def test_missing_keyring_does_not_raise(monkeypatch):
    """Simulate an env without the `keyring` package. get_credential must
    still work and simply return None for absent creds."""
    monkeypatch.setattr(creds, "_keyring", None, raising=False)
    # No env var, no keyring — expect None, not an exception.
    assert creds.get_credential("fanatics", "password") is None


def test_keyring_lookup_uses_canonical_key(monkeypatch):
    """When keyring returns a value, it's plumbed straight through."""
    class FakeKeyring:
        @staticmethod
        def get_password(service, username):
            assert service == "callisto"
            assert username == "CALLISTO_FANATICS_API_KEY"
            return "fake-keychain-secret"

    monkeypatch.setattr(creds, "_keyring", FakeKeyring(), raising=False)
    assert creds.get_credential("fanatics", "api_key") == "fake-keychain-secret"


def test_keyring_errors_are_swallowed(monkeypatch):
    class ExplodingKeyring:
        @staticmethod
        def get_password(service, username):
            raise RuntimeError("keychain locked")

    monkeypatch.setattr(creds, "_keyring", ExplodingKeyring(), raising=False)
    assert creds.get_credential("fanatics", "api_key") is None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
