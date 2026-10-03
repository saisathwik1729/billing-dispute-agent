from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db as db_module
from .agent.orchestrator import recover_interrupted_runs
from .api.routes import router
from .config import settings
from .logging_setup import app_log, request_id_var, setup_logging
from .services.cases import DomainError
from .services.samples import seed_if_empty


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)
    db_module.init_db()
    recovered = recover_interrupted_runs()
    if settings.seed_samples:
        session = db_module.SessionLocal()
        try:
            seed_if_empty(session)
        finally:
            session.close()
    app_log.info("startup", extra={"llm_provider": settings.llm_provider, "model": settings.resolved_model,
                                   "llm_configured": bool(settings.llm_api_key) or settings.llm_provider == "mock",
                                   "recovered_runs": recovered,
                                   "database": settings.database_url.split("://")[0]})
    yield


app = FastAPI(title="Billing Dispute Investigation Agent", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    token = request_id_var.set(rid)
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        app_log.exception("unhandled_error", extra={"path": request.url.path, "method": request.method})
        response = JSONResponse(status_code=500, content={"error": {
            "code": "internal_error", "message": "Something failed on the server. The error was logged.",
            "request_id": rid}})
    ms = int((time.monotonic() - started) * 1000)
    response.headers["x-request-id"] = rid
    if request.url.path.startswith("/api"):
        app_log.info("http_request", extra={"method": request.method, "path": request.url.path,
                                            "status": response.status_code, "duration_ms": ms})
    request_id_var.reset(token)
    return response


@app.exception_handler(DomainError)
async def domain_error(request: Request, exc: DomainError):
    if exc.status >= 500:
        app_log.error("domain_error", extra={"code": exc.code, "error": exc.message})
    return JSONResponse(status_code=exc.status, content={"error": {
        "code": exc.code, "message": exc.message, "details": exc.details, "request_id": request_id_var.get()}})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    details = [f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors()]
    return JSONResponse(status_code=422, content={"error": {
        "code": "validation_error", "message": "Some fields are missing or invalid", "details": details,
        "request_id": request_id_var.get()}})


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": {**detail, "request_id": request_id_var.get()}})


app.include_router(router)

_static = Path(settings.static_dir) if settings.static_dir else Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _static.is_dir():
    app.mount("/assets", StaticFiles(directory=_static / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "Unknown API route"}})
        candidate = _static / full_path
        if full_path and candidate.is_file() and _static in candidate.resolve().parents:
            return FileResponse(candidate)
        return FileResponse(_static / "index.html")
