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


@pytest.mark.parametrize("case", ["error", "oversized"])
def test_strict_bridge_transport_bounds_reads_and_redacts_remote_errors(monkeypatch, case):
    from io import BytesIO
    from urllib.error import HTTPError
    cipher = SecretCipher("a" * 32)
    service = object.__new__(SiteMcpProxyService)
    service.cipher = cipher
    logged = []
    service._record_error = lambda *args: logged.append(args[2])
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, limit):
            assert limit == 4194305
            return b"x" * limit
    def open_request(*args, **kwargs):
        if case == "error":
            raise HTTPError("https://site.example", 409, "Conflict", {}, BytesIO(
                b'{"code":"conflict","message":"PRIVATE PROFILE VALUE","data":{"value":"PRIVATE PROFILE VALUE"}}'))
        return Response()
    def opener(*handlers):
        assert any(handler.__class__.__name__ == "NoBridgeRedirect" for handler in handlers)
        return SimpleNamespace(open=open_request)
    monkeypatch.setattr("app.services.site_mcp_proxy.request.build_opener", opener)
    with pytest.raises(SiteMcpProxyError) as exc:
        service._send(SimpleNamespace(uuid="site-uuid"),
            SimpleNamespace(endpoint="https://site.example/mcp", encrypted_credentials=cipher.encrypt("secret")),
            "execute-ability", {}, strict_transport=True)
    assert exc.value.code == ("CONFLICT" if case == "error" else "REMOTE_INVALID_RESPONSE")
    assert "PRIVATE" not in str(exc.value) + str(logged) + str(exc.value.details)
