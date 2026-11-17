"""HTTP layer: routing, request parsing and the error contract."""

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from app.auth import BearerAuthMiddleware
from app.config import load_settings
from app.errors import ApiError, error_response
from app.ledger import Ledger
from app.validation import parse_operation, validate_idempotency_key, validate_user_id

logger = logging.getLogger("wallet")


def _json(payload: dict, status: int = 200) -> Response:
    return Response(json.dumps(payload).encode(), status_code=status, media_type="application/json")


def create_app() -> FastAPI:
    settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # A fixed-size pool: all connections are opened up front, so no request
        # ever pays for connection setup.
        pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=settings.db_pool_size,
            max_size=settings.db_pool_size,
            command_timeout=30,
        )
        app.state.pool = pool
        app.state.ledger = Ledger(pool)
        try:
            yield
        finally:
            # Graceful shutdown: uvicorn has already stopped accepting requests
            # and let in-flight ones finish before it runs this.
            await pool.close()

    # The interactive docs are disabled: the only endpoints are the brief's.
    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False
    )
    app.add_middleware(BearerAuthMiddleware, token=settings.api_token)

    @app.post("/credit")
    async def credit(request: Request) -> Response:
        user_id, amount = parse_operation(await request.body())
        key = validate_idempotency_key(request.headers.get("idempotency-key"))
        balance = await request.app.state.ledger.credit(user_id, amount, key)
        return _json({"user_id": user_id, "balance": balance})

    @app.post("/debit")
    async def debit(request: Request) -> Response:
        user_id, amount = parse_operation(await request.body())
        key = validate_idempotency_key(request.headers.get("idempotency-key"))
        balance = await request.app.state.ledger.debit(user_id, amount, key)
        return _json({"user_id": user_id, "balance": balance})

    @app.get("/balance/{user_id}")
    async def get_balance(user_id: str, request: Request) -> Response:
        validate_user_id(user_id)
        balance = await request.app.state.ledger.balance(user_id)
        return _json({"user_id": user_id, "balance": balance})

    @app.get("/health")
    async def health(request: Request) -> Response:
        await request.app.state.pool.fetchval("SELECT 1")
        return _json({"status": "ok"})

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> Response:
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        # Unknown paths and wrong methods, in the same error shape as the rest.
        codes = {404: "not_found", 405: "method_not_allowed"}
        return error_response(exc.status_code, codes.get(exc.status_code, "http_error"), str(exc.detail))

    @app.exception_handler(asyncpg.PostgresConnectionError)
    @app.exception_handler(asyncpg.CannotConnectNowError)
    @app.exception_handler(asyncpg.InterfaceError)
    @app.exception_handler(OSError)
    @app.exception_handler(TimeoutError)
    async def handle_database_down(request: Request, exc: Exception) -> Response:
        logger.error("database unavailable: %s", type(exc).__name__)
        return error_response(503, "service_unavailable", "database is unavailable")

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> Response:
        # Details go to the server log only, never into the response.
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return error_response(500, "internal_error", "internal server error")

    return app
