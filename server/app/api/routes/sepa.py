"""Elementor webhook: bearer capability in the POST body, no Hub session."""

import json
import logging
import threading
import time
from collections import OrderedDict
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError

from app.core.security import get_secret_cipher
from app.db.session import SessionLocal
from app.services.hub_sepa import HubSepaService, SepaError, extract_fields


WEBHOOK_PATH = "/api/v1/integrations/sepa/submissions"
router = APIRouter(tags=["sepa-integration"])
MAX_BODY = 32768
_rates = OrderedDict()
_rate_lock = threading.Lock()
logger = logging.getLogger(__name__)


def _allow_request(address):
    now = time.monotonic()
    with _rate_lock:
        for key in ("*", address):
            start, count = _rates.get(key, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            if count >= (1000 if key == "*" else 120):
                return False
            _rates[key] = (start, count + 1)
            _rates.move_to_end(key)
        while len(_rates) > 2048:
            _rates.popitem(last=False)
    return True


def decode_fields(body, content_type):
    try:
        if content_type == "application/x-www-form-urlencoded":
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=256)
        elif content_type == "application/json":
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            payload = json.loads(body, object_pairs_hook=unique_object)
            if not isinstance(payload, dict):
                raise ValueError()
            if "fields" in payload:
                if not isinstance(payload["fields"], dict):
                    raise ValueError()
                pairs = [(key, entry.get("value")) for key, entry in payload["fields"].items() if isinstance(entry, dict)]
            else:
                pairs = list(payload.items())
        else:
            raise SepaError("Bitte Elementor-Webhook mit Advanced Data aktivieren.", 415)
        return extract_fields(pairs)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, SepaError):
            raise
        raise SepaError("Die Formulardaten konnten nicht gelesen werden.") from None


def _receive(fields):
    with SessionLocal() as db:
        result = HubSepaService(db=db, cipher=get_secret_cipher()).receive(fields)
        db.commit()
        return result


@router.post("/api/v1/integrations/sepa/submissions")
async def submit_sepa(request: Request):
    headers = {"Cache-Control": "no-store"}
    if not _allow_request(request.client.host if request.client else "unknown"):
        return JSONResponse({"success": False, "message": "Zu viele Anfragen. Bitte spaeter erneut versuchen."},
                            status_code=429, headers={**headers, "Retry-After": "60"})
    try:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise SepaError("Die Formulardaten sind zu gross.", 413)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        fields = decode_fields(bytes(body), content_type)
        result = await run_in_threadpool(_receive, fields)
        return JSONResponse(result, headers=headers)
    except SepaError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=exc.status_code, headers=headers)
    except SQLAlchemyError:
        # Do not emit exception parameters: submissions contain bank data.
        logger.error("SEPA submission transaction failed; no success acknowledged")
        return JSONResponse({"success": False, "message": "Speichern derzeit nicht moeglich. Bitte erneut versuchen."},
                            status_code=503, headers=headers)
