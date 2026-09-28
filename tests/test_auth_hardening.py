"""Production hardening tests for public authentication endpoints."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.main import app
from app.models import User
from app.rate_limit import limiter
from app.routers.auth import (
    _FIRST_ADMIN_ADVISORY_LOCK_ID,
    _first_admin_creation_guard,
    RegisterRequest,
)


@pytest.fixture(autouse=True)
def _isolated_rate_limit_storage():
    limiter.reset()
    yield
    limiter.reset()


def _transport() -> httpx.ASGITransport:
    # Lifespan is intentionally omitted: the engine fixture already creates
    # the schema and points app.database.AsyncSessionLocal at that engine.
    return httpx.ASGITransport(app=app)


@pytest.mark.asyncio
async def test_auth_endpoints_have_independent_explicit_rate_limits(engine):
    async with httpx.AsyncClient(transport=_transport(), base_url="http://test") as client:
        login_results = [
            await client.post(
                "/api/auth/login",
                json={"username": "missing", "password": "invalid"},
            )
            for _ in range(11)
        ]
        assert [response.status_code for response in login_results[:10]] == [401] * 10
        assert login_results[10].status_code == 429

        # Exhausting login must not consume setup-status or refresh buckets.
        assert (await client.get("/api/auth/setup-status")).status_code == 200
        assert (await client.post("/api/auth/refresh")).status_code == 401

        refresh_results = [
            await client.post("/api/auth/refresh") for _ in range(30)
        ]
        assert [response.status_code for response in refresh_results[:29]] == [401] * 29
        assert refresh_results[29].status_code == 429

        setup_results = [
            await client.get("/api/auth/setup-status") for _ in range(30)
        ]
        assert [response.status_code for response in setup_results[:29]] == [200] * 29
        assert setup_results[29].status_code == 429


@pytest.mark.asyncio
async def test_first_admin_registration_has_explicit_rate_limit(engine):
    payload = {
        "username": "initial-admin",
        "email": "initial-admin@example.com",
        "password": "StrongPass123",
    }
    async with httpx.AsyncClient(transport=_transport(), base_url="http://test") as client:
        first = await client.post("/api/auth/register", json=payload)
        assert first.status_code == 201

        closed = [
            await client.post("/api/auth/register", json=payload) for _ in range(4)
        ]
        assert [response.status_code for response in closed] == [403] * 4
        assert (await client.post("/api/auth/register", json=payload)).status_code == 429


@pytest.mark.asyncio
async def test_first_admin_registration_and_login_accept_dotted_username(engine):
    payload = {
        "username": "Tiago.Sasaki",
        "email": "tiago.sasaki@example.com",
        "password": "StrongPass123",
    }
    async with httpx.AsyncClient(transport=_transport(), base_url="http://test") as client:
        registered = await client.post("/api/auth/register", json=payload)
        assert registered.status_code == 201
        assert registered.json()["username"] == "tiago.sasaki"

        logged_in = await client.post(
            "/api/auth/login",
            json={"username": "Tiago.Sasaki", "password": payload["password"]},
        )
        assert logged_in.status_code == 200


@pytest.mark.parametrize(
    "username",
    [
        ".admin",
        "admin.",
        "admin..root",
        "tiago/sasaki",
        "tiago@sasaki",
        "tiago'sasaki",
        "tiago\nsasaki",
    ],
)
def test_registration_rejects_unsafe_or_ambiguous_username(username):
    with pytest.raises(ValidationError):
        RegisterRequest(
            username=username,
            email="admin@example.com",
            password="StrongPass123",
        )


@pytest.mark.asyncio
async def test_concurrent_first_admin_registration_creates_exactly_one_admin(engine):
    payloads = (
        {
            "username": "admin-one",
            "email": "admin-one@example.com",
            "password": "StrongPass123",
        },
        {
            "username": "admin-two",
            "email": "admin-two@example.com",
            "password": "StrongPass123",
        },
    )

    async with httpx.AsyncClient(transport=_transport(), base_url="http://test") as client:
        responses = await asyncio.gather(
            *(client.post("/api/auth/register", json=payload) for payload in payloads)
        )

    assert sorted(response.status_code for response in responses) == [201, 403]

    from app import database as app_database

    async with app_database.AsyncSessionLocal() as db:
        user_count = await db.scalar(select(func.count(User.id)))
        admin_count = await db.scalar(
            select(func.count(User.id)).where(User.role == "admin")
        )
    assert user_count == 1
    assert admin_count == 1


@pytest.mark.asyncio
async def test_postgres_first_admin_guard_uses_transaction_advisory_lock():
    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _FakePostgresSession:
        def __init__(self):
            self.calls = []

        def get_bind(self):
            return _Bind()

        async def execute(self, statement, params):
            self.calls.append((str(statement), params))

    db = _FakePostgresSession()
    async with _first_admin_creation_guard(db):
        pass

    assert db.calls == [
        (
            "SELECT pg_advisory_xact_lock(:lock_id)",
            {"lock_id": _FIRST_ADMIN_ADVISORY_LOCK_ID},
        )
    ]
