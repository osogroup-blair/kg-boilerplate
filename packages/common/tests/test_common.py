import base64
import time
import uuid

import pytest
from kg_common.errors import DependencyUnavailable, NotFound, PlatformError
from kg_common.ids import is_uuid, uuid7, uuid7_timestamp_ms
from kg_common.settings import DEV_JWT_SECRET, DEV_PEPPER, Settings
from kg_common.tokens import TokenCounter, count_tokens, split_by_tokens, truncate_to_tokens

pytestmark = pytest.mark.unit


def test_uuid7_format_and_version():
    u = uuid7()
    assert u.version == 7 and u.variant == uuid.RFC_4122
    assert is_uuid(str(u))
    assert abs(uuid7_timestamp_ms(u) - time.time() * 1000) < 5000


def test_uuid7_is_monotonic():
    ids = [uuid7() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_is_uuid_rejects_garbage():
    assert not is_uuid("not-a-uuid") and not is_uuid(None) and not is_uuid("0" * 36)
    assert is_uuid("0199c0de-0000-7000-8000-000000000000")


def test_settings_dev_defaults_ok():
    s = Settings(_env_file=None, env="dev")
    assert s.router_base_url.endswith("/v1") and s.router_origin == s.router_base_url[:-3]


def test_settings_production_refuses_dev_defaults():
    with pytest.raises(ValueError, match="development default"):
        Settings(_env_file=None, env="production")
    with pytest.raises(ValueError, match="at least 32 bytes"):
        Settings(_env_file=None, env="production", auth_key_pepper="short", auth_jwt_secret="x" * 40, telemetry_hmac_secret="y" * 40)
    p1 = {"auth_key_pepper": "p" * 40, "auth_jwt_secret": "j" * 40, "telemetry_hmac_secret": "t" * 40}
    with pytest.raises(
        ValueError, match="CONTENT_HMAC_SECRET.*PAYLOAD_KEYS"
    ):  # P3: the ledger's encryption inputs are production secrets too
        Settings(_env_file=None, env="production", **p1)
    ok = Settings(
        _env_file=None,
        env="production",
        **p1,
        content_hmac_secret="c" * 40,
        payload_keys="prod1:" + base64.b64encode(b"k" * 32).decode(),
        payload_active_kid="prod1",
    )
    assert ok.is_production and DEV_PEPPER not in repr(ok) and DEV_JWT_SECRET not in repr(ok) and "prod1:" not in repr(ok)


def test_secrets_are_not_in_repr():
    assert "kg_app_dev" not in repr(Settings(_env_file=None))


def test_errors_carry_code_and_status():
    assert (NotFound().code, NotFound().http_status) == ("not_found", 404)
    e = DependencyUnavailable("x")
    assert e.retryable and e.http_status == 503
    assert PlatformError("m", code="custom", http_status=418).code == "custom"


def test_token_helpers():
    assert count_tokens("hello world") >= 2
    assert count_tokens(truncate_to_tokens("word " * 200, 10)) <= 10
    parts = split_by_tokens("word " * 200, 50)
    assert len(parts) >= 4 and "".join(parts).replace(" ", "") == ("word" * 200)


def test_token_counter_margin_and_id():
    c = TokenCounter()
    assert c.tokenizer_id == "tiktoken:max(o200k_base,cl100k_base)+margin0.1"
    assert c.count("some text to count") >= c.raw("some text to count")
