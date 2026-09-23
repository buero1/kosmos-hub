import asyncio
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from app import main


def request(path):
    return Request({"type": "http", "method": "GET", "scheme": "https",
        "server": ("hub.test", 443), "path": path, "query_string": b"",
        "headers": [], "session": {}})


def middleware():
    return next(item.kwargs["dispatch"] for item in main.create_app().user_middleware
                if "dispatch" in item.kwargs)


async def success(_request):
    return PlainTextResponse("ok")


@pytest.mark.parametrize("path,helper", [
    ("/updates", "_authenticated_hub_user"),
    ("/mcp", "_authenticated_mcp_actor"),
    ("/mcp", "_authenticated_hub_user"),
    ("/api/v1/desktop/events", "_authenticated_desktop_user"),
    ("/api/v1/integrations/callapp", "_authenticated_integration"),
])
def test_authentication_does_not_wait_on_event_loop(monkeypatch, path, helper):
    called = []
    monkeypatch.setattr(main, "_authenticated_mcp_actor", lambda req: None)
    monkeypatch.setattr(main, "_authenticated_hub_user", lambda req: None)

    def authenticate(req):
        called.append(threading.get_ident())
        return None

    monkeypatch.setattr(main, helper, authenticate)
    response = asyncio.run(middleware()(request(path), success))
    assert response.status_code == 401
    assert called and all(ident != threading.get_ident() for ident in called)


@pytest.mark.parametrize("module_allowed,record_allowed,expected", [
    (False, True, 403), (True, False, 404), (True, True, 200),
])
def test_access_checks_still_enforced_off_loop(monkeypatch, module_allowed, record_allowed, expected):
    called = []
    user = SimpleNamespace(id=1, username="test", display_name="Test", role="admin")
    monkeypatch.setattr(main, "_authenticated_hub_user", lambda req: user)
    monkeypatch.setattr(main, "SessionLocal", lambda: nullcontext(None))
    monkeypatch.setattr(main, "_should_record_http_activity", lambda *args, **kwargs: False)

    def allowed(*args, **kwargs):
        called.append(threading.get_ident())
        return module_allowed

    def record(**kwargs):
        called.append(threading.get_ident())
        return record_allowed

    monkeypatch.setattr(main, "HubAccessControlService", lambda **kwargs: SimpleNamespace(
        can=allowed, can_access_record=record))
    response = asyncio.run(middleware()(request("/customers/1"), success))
    assert response.status_code == expected
    assert called and all(ident != threading.get_ident() for ident in called)


def test_exhausted_database_pool_can_be_released_by_another_request(monkeypatch):
    engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=1,
        max_overflow=0, pool_timeout=0.5, connect_args={"check_same_thread": False})
    held = engine.connect()

    def authenticate(req):
        with engine.connect() as connection:
            assert connection.scalar(text("select 1")) == 1
        return None

    monkeypatch.setattr(main, "_authenticated_hub_user", authenticate)

    async def scenario():
        async def finish_previous_request():
            await asyncio.sleep(0.05)
            held.close()

        waiting = asyncio.create_task(middleware()(request("/updates"), success))
        cleanup = asyncio.create_task(finish_previous_request())
        try:
            assert (await waiting).status_code == 401
        finally:
            await cleanup

    try:
        asyncio.run(scenario())
    finally:
        held.close()
        engine.dispose()
