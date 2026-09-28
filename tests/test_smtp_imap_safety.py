"""Regression tests for IMAP reply-sync safety and follow-up gating."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import unibox as unibox_mod
from app.models import Inbox, SmtpAccount, SmtpSyncState
from app.routers.system_health import get_system_health
from app.smtp_utils import (
    derive_imap_sync_status,
    smtp_followup_hold_reason,
)


class _PagedFakeIMAP:
    def __init__(self, uids):
        self.uids = list(uids)
        self.fetch_queries = []

    def status(self, mailbox, query):
        return "OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 999)"]

    def uid(self, command, *args):
        if command == "search":
            low = int(str(args[-1]).split("UID", 1)[1].split(":", 1)[0])
            return "OK", [" ".join(str(uid) for uid in self.uids if uid >= low).encode()]
        if command == "fetch":
            uid = int(args[0])
            query = args[1]
            self.fetch_queries.append((uid, query))
            raw = f"From: lead{uid}@example.com\r\nMessage-ID: <{uid}@example.com>\r\n\r\nhello".encode()
            return "OK", [(f"{uid} (BODY[] {{{len(raw)}}})".encode(), raw)]
        raise AssertionError(f"unexpected IMAP command: {command}")

    def logout(self):
        return "OK", []


class _FailingPageIMAP(_PagedFakeIMAP):
    def uid(self, command, *args):
        if command == "fetch" and int(args[0]) == 2:
            return "NO", []
        return super().uid(command, *args)


def _account(*, imap_host="imap.example.com"):
    return SmtpAccount(
        inbox_id=1,
        smtp_host="smtp.example.com",
        smtp_username="sender@example.com",
        smtp_password="secret",
        imap_host=imap_host,
        imap_username="sender@example.com",
        imap_password="secret",
    )


def test_imap_fetch_peeks_without_seen_and_pages_oldest_first(monkeypatch):
    first_client = _PagedFakeIMAP(range(1, 204))
    monkeypatch.setattr("app.smtp_utils._imap_connect", lambda account, timeout: first_client)

    validity, first_page = unibox_mod._fetch_smtp_new_messages(
        _account(), uidvalidity=77, last_uid=0, cap=200
    )

    assert validity == 77
    assert [uid for uid, _ in first_page] == list(range(1, 201))
    assert first_client.fetch_queries
    assert {query for _, query in first_client.fetch_queries} == {"(BODY.PEEK[])"}

    second_client = _PagedFakeIMAP(range(1, 204))
    monkeypatch.setattr("app.smtp_utils._imap_connect", lambda account, timeout: second_client)
    _, second_page = unibox_mod._fetch_smtp_new_messages(
        _account(), uidvalidity=77, last_uid=200, cap=200
    )
    assert [uid for uid, _ in second_page] == [201, 202, 203]


def test_imap_fetch_aborts_partial_page_so_watermark_cannot_jump(monkeypatch):
    client = _FailingPageIMAP([1, 2, 3])
    monkeypatch.setattr("app.smtp_utils._imap_connect", lambda account, timeout: client)

    with pytest.raises(RuntimeError, match="UID 2"):
        unibox_mod._fetch_smtp_new_messages(
            _account(), uidvalidity=77, last_uid=0, cap=200
        )


def test_followup_gate_only_blocks_configured_unhealthy_imap():
    now = datetime(2026, 9, 27, 12, 0, 0)
    threshold = timedelta(minutes=15)
    configured = _account()

    assert "not completed" in smtp_followup_hold_reason(
        configured, None, now=now, stale_after=threshold
    )

    healthy = SmtpSyncState(last_success_at=now - timedelta(minutes=5), last_error="")
    assert smtp_followup_hold_reason(
        configured, healthy, now=now, stale_after=threshold
    ) is None
    assert derive_imap_sync_status(configured, healthy, now=now) == "healthy"

    stale = SmtpSyncState(last_success_at=now - timedelta(minutes=16), last_error="")
    assert "stale" in smtp_followup_hold_reason(
        configured, stale, now=now, stale_after=threshold
    )

    failed = SmtpSyncState(
        last_success_at=now - timedelta(minutes=1),
        last_error="Authentication failed — check username/password",
    )
    assert "failing" in smtp_followup_hold_reason(
        configured, failed, now=now, stale_after=threshold
    )
    assert derive_imap_sync_status(configured, failed, now=now) == "failing"

    send_only = _account(imap_host="")
    assert smtp_followup_hold_reason(send_only, None, now=now) is None
    assert derive_imap_sync_status(send_only, None, now=now) == "not_configured"


@pytest.mark.asyncio
async def test_sync_persists_failure_then_success_and_auto_resumes(session, monkeypatch):
    inbox = Inbox(email="sender@example.com", provider="smtp")
    session.add(inbox)
    await session.flush()
    account = _account()
    account.inbox_id = inbox.id
    session.add(account)
    await session.flush()

    async def no_event(*args, **kwargs):
        return None

    monkeypatch.setattr(unibox_mod, "maybe_fire_email_event", no_event)

    def fail_fetch(*args, **kwargs):
        raise RuntimeError("authentication failed")

    monkeypatch.setattr(unibox_mod, "_fetch_smtp_new_messages", fail_fetch)
    assert await unibox_mod._sync_inbox_smtp(session, inbox, "test-failure") == set()

    state = (
        await session.execute(
            select(SmtpSyncState).where(SmtpSyncState.inbox_id == inbox.id)
        )
    ).scalar_one()
    assert state.last_attempt_at is not None
    assert state.last_success_at is None
    assert state.last_error == "Authentication failed — check username/password"
    assert smtp_followup_hold_reason(account, state) is not None

    monkeypatch.setattr(
        unibox_mod,
        "_fetch_smtp_new_messages",
        lambda *args, **kwargs: (77, []),
    )
    assert await unibox_mod._sync_inbox_smtp(session, inbox, "test-recovery") == set()
    await session.refresh(state)
    assert state.uidvalidity == 77
    assert state.last_success_at is not None
    assert state.last_sync_at == state.last_success_at
    assert state.last_error == ""
    assert smtp_followup_hold_reason(account, state) is None


@pytest.mark.asyncio
async def test_parse_failure_does_not_advance_uid_watermark(session, monkeypatch):
    inbox = Inbox(email="sender@example.com", provider="smtp")
    session.add(inbox)
    await session.flush()
    account = _account()
    account.inbox_id = inbox.id
    session.add(account)
    await session.flush()

    monkeypatch.setattr(
        unibox_mod,
        "_fetch_smtp_new_messages",
        lambda *args, **kwargs: (77, [(10, b"not-an-email")]),
    )
    monkeypatch.setattr("app.smtp_utils.parse_imap_message", lambda raw: (_ for _ in ()).throw(ValueError("bad")))

    await unibox_mod._sync_inbox_smtp(session, inbox, "parse-failure")
    state = (
        await session.execute(
            select(SmtpSyncState).where(SmtpSyncState.inbox_id == inbox.id)
        )
    ).scalar_one()
    assert state.last_uid == 0
    assert state.last_success_at is None
    assert "UID 10" in state.last_error


@pytest.mark.asyncio
async def test_system_health_exposes_imap_sync_state(session):
    inbox = Inbox(email="sender@example.com", display_name="Sender", provider="smtp")
    session.add(inbox)
    await session.flush()
    account = _account()
    account.inbox_id = inbox.id
    session.add_all([
        account,
        SmtpSyncState(
            inbox_id=inbox.id,
            last_attempt_at=datetime.utcnow(),
            last_error="Connection timed out",
        ),
    ])
    await session.flush()

    payload = await get_system_health(session)
    item = payload["smtp"]["accounts"][0]
    assert item["imap_sync_status"] == "failing"
    assert item["imap_followups_on_hold"] is True
    assert item["last_imap_sync_attempt_at"]
    assert item["last_imap_sync_success_at"] is None
    assert item["last_imap_sync_error"] == "Connection timed out"
