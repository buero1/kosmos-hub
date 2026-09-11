from types import SimpleNamespace

import pytest

from app.core.security import SecretCipher
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService


class _InvalidJsonResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @staticmethod
    def read() -> bytes:
        return b"<html>Unexpected WordPress output</html>"


def test_invalid_bridge_json_is_a_structured_remote_error(monkeypatch):
    cipher = SecretCipher("a" * 32)
    service = object.__new__(SiteMcpProxyService)
    service.cipher = cipher
    recorded_errors: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "app.services.site_mcp_proxy.request.urlopen",
        lambda *_args, **_kwargs: _InvalidJsonResponse(),
    )
    monkeypatch.setattr(
        service,
        "_record_error",
        lambda _site, action, detail, _request_id: recorded_errors.append((action, detail)),
    )

    with pytest.raises(SiteMcpProxyError) as error:
        service._send(
            SimpleNamespace(uuid="site-uuid"),
            SimpleNamespace(endpoint="https://site.example/wp-json/kosmos-bridge/v1/mcp", encrypted_credentials=cipher.encrypt("secret")),
            "execute-ability",
            {},
        )

    assert error.value.code == "REMOTE_INVALID_RESPONSE"
    assert error.value.status_code == 200
    assert error.value.details == {"response_length": 40}
    assert recorded_errors == [
        ("execute-ability", "Site returned an invalid response instead of the expected Bridge JSON. HTTP 200; response length: 40 bytes.")
    ]
