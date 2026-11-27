"""OpenAI chat transport for a LiteLLM sidecar. Configured channels are fixed destinations."""
import asyncio
import json
import os
import re
import sqlite3
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .shadow_metering import ShadowMeter


def identifier(value):
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:\-]{1,200}', value):
        return value
    return None


class Completion:
    """Read provider usage from the final streamed packet or the JSON response."""
    def __init__(self, streaming):
        self.streaming = streaming
        self.serialized = bytearray()
        self.oversized = False
        self.usage = {}
        self.response_id = self.model = None
        self.done = False

    def observe_json(self, data):
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            return
        if not isinstance(value, dict):
            return
        self.response_id = identifier(value.get('id')) or self.response_id
        self.model = identifier(value.get('model')) or self.model
        if isinstance(value.get('usage'), dict):
            self.usage = value['usage']

    def observe(self, data):
        if not self.streaming:
            if not self.oversized:
                if len(self.serialized) + len(data) > 4 * 1024 * 1024:
                    self.serialized.clear()
                    self.oversized = True
                else:
                    self.serialized.extend(data)
            return
        lines = data.split(b'\n')
        for index, line in enumerate(lines):
            if not self.oversized:
                if len(self.serialized) + len(line) > 1024 * 1024:
                    self.serialized.clear()
                    self.oversized = True
                else:
                    self.serialized.extend(line)
            if index == len(lines) - 1:
                break
            if not self.oversized and self.serialized.startswith(b'data:'):
                payload = self.serialized[5:].strip()
                if payload == b'[DONE]':
                    self.done = True
                else:
                    self.observe_json(payload)
            self.serialized.clear()
            self.oversized = False


def create_relay(channels, meter, *, transport=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    in_flight = set()

    @app.get('/ready')
    async def ready():
        return {'ready': True}

    @app.post('/{channel}/v1/chat/completions')
    async def forward(channel: str, request: Request):
        config = channels.get(channel)
        if config is None:
            raise HTTPException(404, 'Unknown model channel')
        try:
            call_id = str(UUID(request.headers.get('x-starbridge-call-id', '')))
        except ValueError:
            raise HTTPException(403, 'Model correlation required') from None
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 512 * 1024:
                raise HTTPException(413, 'Model request exceeds relay limit')
        try:
            requested = json.loads(body)
            model = identifier(requested.get('model'))
        except (ValueError, AttributeError):
            model = None
        if not model:
            raise HTTPException(400, 'Invalid model request')
        # This id represents what is actually sent from this transport, including
        # an SDK retry with the same call id. The proxy cannot supply this id.
        max_tokens = config.get('max_tokens')
        if max_tokens is not None or config.get('disable_thinking'):
            # Candidate supplier limits are enforced before contacting the vendor.
            if max_tokens is not None:
                requested.pop('max_completion_tokens', None)
                requested['max_tokens'] = max_tokens
            if config.get('disable_thinking'):
                requested['thinking'] = {'type': 'disabled'}
                requested['reasoning_effort'] = 'none'
            body = json.dumps(requested).encode()
        try:
            attempt_id = await asyncio.to_thread(
                meter.start, call_id, config.get('provider', channel), model,
                batch=config.get('batch'), limit=config.get('limit'),
            )
        except (ValueError, OSError, sqlite3.Error):
            raise HTTPException(503, 'Shadow admission unavailable') from None
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=10), follow_redirects=False, trust_env=False, transport=transport,
        )
        upstream = None
        provider_headers = {'Content-Type': 'application/json'}
        if config.get('key_env'):
            provider_headers['Authorization'] = 'Bearer ' + os.environ[config['key_env']]
        try:
            upstream = await client.send(client.build_request(
                'POST', config['url'], headers=provider_headers, content=bytes(body),
            ), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            await asyncio.to_thread(meter.finish, attempt_id, 'unconfirmed', None, None, None, None, {}, config.get('price'))
            raise HTTPException(502, 'Upstream model response unconfirmed') from None
        mime = upstream.headers.get('content-type', 'application/json')
        completion = Completion(mime.split(';', 1)[0].strip() == 'text/event-stream')
        status = upstream.status_code
        request_id = identifier(upstream.headers.get('x-request-id')) or identifier(upstream.headers.get('request-id'))
        passthrough = {key: upstream.headers[key] for key in ('x-request-id', 'request-id', 'retry-after')
                       if key in upstream.headers}

        async def finish(exhausted):
            if not completion.streaming and not completion.oversized:
                completion.observe_json(completion.serialized)
            state = 'failed' if status >= 400 else (
                'complete' if exhausted and (not completion.streaming or completion.done) else 'unconfirmed'
            )
            await asyncio.to_thread(meter.finish, attempt_id, state, status, request_id, completion.response_id,
                                    completion.model, completion.usage, config.get('price'),
                                    defer_seconds=config.get('defer_seconds', 0))

        if not completion.streaming:
            exhausted = False
            try:
                async for chunk in upstream.aiter_bytes():
                    completion.observe(chunk)
                    if completion.oversized:
                        raise HTTPException(502, 'Model response exceeds relay limit')
                exhausted = True
                return Response(bytes(completion.serialized), status_code=status, media_type=mime, headers=passthrough)
            finally:
                try:
                    await finish(exhausted)
                finally:
                    await upstream.aclose()
                    await client.aclose()

        queue = asyncio.Queue(maxsize=32)
        detached = asyncio.Event()

        async def offer(data):
            while not detached.is_set():
                try:
                    await asyncio.wait_for(queue.put(data), 0.25)
                    break
                except TimeoutError:
                    continue

        async def drain():
            exhausted = False
            try:
                async for chunk in upstream.aiter_bytes():
                    completion.observe(chunk)
                    await offer(chunk)
                exhausted = True
            finally:
                try:
                    await finish(exhausted)
                finally:
                    await upstream.aclose()
                    await client.aclose()
                    await offer(None)

        task = asyncio.create_task(drain())
        in_flight.add(task)

        def release(ended):
            in_flight.discard(ended)
            if not ended.cancelled():
                ended.exception()

        task.add_done_callback(release)

        async def relay_chunks():
            try:
                while (chunk := await queue.get()) is not None:
                    yield chunk
                await task
            finally:
                detached.set()

        return StreamingResponse(relay_chunks(), status_code=status, media_type=mime, headers=passthrough)

    return app


def app():
    return create_relay(json.loads(os.environ['STARBRIDGE_SHADOW_CHANNELS']),
                        ShadowMeter(os.environ['STARBRIDGE_SHADOW_DB']))
