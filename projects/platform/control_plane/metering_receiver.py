"""Internal OTLP/HTTP JSON receiver for dedicated, allowlisted model usage logs.

Run as a separate background service with uvicorn --factory. It commits usage
before acknowledging the collector. The existing worker publishes CloudEvents.
"""
import hmac
import json
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .cost_console import import_receipt
from .database import SessionLocal
from .settings import get_settings

SCOPE = 'starbridge.model-usage'
MAX_BATCH_BYTES = 1024 * 1024
MAX_BATCH_RECORDS = 256


class MeterReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attempt_id: UUID
    call_id: UUID
    owner_id: str = Field(min_length=1, max_length=255)
    channel: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=200)
    ordinal: int = Field(default=1, ge=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    status: str = Field(min_length=1, max_length=32)
    provider_request_id: str | None = Field(default=None, max_length=255)
    response_id: str | None = Field(default=None, max_length=255)
    usage_format: Literal['litellm', 'openai_chat', 'anthropic_messages', 'deepseek_chat']
    observation_scope: Literal['upstream_attempt', 'litellm_spend_record'] = 'upstream_attempt'
    usage: dict

    def event(self):
        return {**self.model_dump(mode='json'), 'event_id': str(self.attempt_id), 'schema': 2}


def metering_records(source):
    records = []
    for resource in source['resourceLogs']:
        for scope in resource.get('scopeLogs', []):
            if scope.get('scope', {}).get('name') != SCOPE:
                raise ValueError('unexpected_metering_scope')
            for record in scope.get('logRecords', []):
                if len(records) >= MAX_BATCH_RECORDS:
                    raise ValueError('metering_batch_too_large')
                data = json.loads(record['body']['stringValue'])
                records.append(MeterReceipt.model_validate(data).event())
    if not records:
        raise ValueError('empty_metering_batch')
    return records


def persist(events):
    with SessionLocal.begin() as db:
        for event in events:
            import_receipt(db, event)


def create_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.post('/v1/logs')
    async def ingest(req: Request, x_meter_token: str = Header(default='')):
        token = get_settings().cost_ingest_token
        if len(token) < 32 or not hmac.compare_digest(x_meter_token.encode(), token.encode()):
            raise HTTPException(403)
        if req.headers.get('content-type', '').split(';', 1)[0] != 'application/json':
            raise HTTPException(415)
        body = bytearray()
        async for chunk in req.stream():
            if len(body) + len(chunk) > MAX_BATCH_BYTES:
                raise HTTPException(413)
            body.extend(chunk)
        try:
            records = metering_records(json.loads(body))
            await run_in_threadpool(persist, records)
        except (ValueError, AttributeError, TypeError, KeyError, OverflowError):
            raise HTTPException(400, 'invalid_metering_log') from None
        except SQLAlchemyError:
            # Concurrent identical first imports can race. Retry the whole transaction.
            raise HTTPException(503, 'metering_receipt_not_committed') from None
        return {}

    return app
