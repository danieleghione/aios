"""The control plane: the FastAPI application, its error handling, and the
routers each subject lives in. This module was 1200 lines of everything."""
import json
import sqlite3
import time
from contextlib import asynccontextmanager

import asyncio
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__, gateway, routes_accounts, routes_models, routes_system
from .core import encode, initialize
# Re-exported: tests and other modules reach these through aios.app.
from .gateway import (IMAGE_TIMEOUT, LOAD_TIMEOUT, SETTLE_POLL, _BUSY, _SLOTS, acquire_slot,  # noqa: F401
                      ensure_running, inference_authorised, settle)
from .routes_models import config_differs, group_variants, recommended_variant, released_at  # noqa: F401
from .routes_system import journal_message  # noqa: F401
from .runtime import model_kind, unusable  # noqa: F401
from .web import ACTIVE, CPU_HISTORY, ERRORS, LOG, REQUESTS, admin, operator, sample_cpu, superadmin, viewer  # noqa: F401

@asynccontextmanager
async def lifespan(app):
    initialize()
    sampler = asyncio.create_task(sample_cpu())
    yield
    sampler.cancel()

# The API reference describes every route: it is for signed-in users, not for
# whoever reaches the address.
app = FastAPI(title='AIOS Control Plane', version=__version__, lifespan=lifespan, docs_url=None, openapi_url=None, redoc_url=None)

@app.exception_handler(HTTPException)
async def http_error(request, exc):
    ERRORS.labels(str(exc.status_code)).inc()
    return JSONResponse({'error': {'code': exc.status_code, 'message': str(exc.detail)}}, status_code=exc.status_code, headers=exc.headers)

@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse({'error': {'code': 422, 'message': 'Invalid request', 'fields': [{'location': list(e['loc']), 'message': e['msg']} for e in exc.errors()]}}, status_code=422)

@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({'error': {'code': 400, 'message': str(exc)}}, status_code=400)

@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse({'error': {'code': 409, 'message': 'Conflicting resource or invalid lifecycle transition'}}, status_code=409)

@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    LOG.error(encode({'event': 'unhandled_error', 'type': type(exc).__name__}))
    ERRORS.labels('500').inc()
    return JSONResponse({'error': {'code': 500, 'message': 'Internal operation failed; consult appliance logs'}}, status_code=500)

@app.middleware('http')
async def request_log(request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    LOG.info(encode({'event': 'http', 'method': request.method, 'path': request.url.path, 'status': response.status_code, 'duration': time.monotonic() - start}))
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.get('/api/aios/openapi.json', include_in_schema=False)
def openapi_schema(user=Depends(viewer)):
    return app.openapi()


@app.get('/api/aios/docs', include_in_schema=False, response_class=HTMLResponse)
def offline_reference(user=Depends(viewer)):
    import html
    schema = app.openapi()
    sections = []
    for path, operations in schema['paths'].items():
        for method, operation in operations.items():
            sections.append('<details><summary><b>' + html.escape(method.upper()) + '</b> ' + html.escape(path) + '</summary><pre>' + html.escape(json.dumps(operation, indent=2)) + '</pre></details>')
    return '<!doctype html><html lang="en"><meta charset="utf-8"><title>AIOS API reference</title><style>body{max-width:1100px;margin:40px auto;font-family:system-ui;padding:20px;color:#18332f}details{padding:14px;border-bottom:1px solid #ddd}pre{overflow:auto;background:#f3f6f5;padding:16px}summary{cursor:pointer}a{color:#17796d}</style><h1>AIOS API reference</h1><p>Offline reference · <a href="/api/aios/openapi.json">OpenAPI JSON</a> · <a href="/admin/">Admin portal</a></p>' + ''.join(sections) + '<h2>Schemas</h2><pre>' + html.escape(json.dumps(schema.get('components', {}), indent=2)) + '</pre></html>'
for part in (routes_accounts, routes_models, routes_system, gateway):
    app.include_router(part.router)
