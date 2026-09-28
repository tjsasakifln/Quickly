"""Auth-failure handling in the per-slot send job (issue #1).

A broken SMTP/IMAP credential used to leave the queue slot in place and retry
forever, each attempt holding a DB transaction open across a blocking SMTP
call.  These tests pin the fixed behaviour: the inbox is paused, the
in-memory circuit breaker skips the remaining due slots, and the event carries
enough context for a correctly-labelled notification.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

import app.jobs as jobs_mod
from app.models import CampaignLead, EmailLog, QueueSlot, Sequence, SmtpAccount, SmtpSyncState
from app.sender import SendFailure, SendResult
from tests.conftest import (
    make_campaign,
    make_campaign_inbox,
    make_campaign_lead,
    make_inbox,
    make_lead,
    make_queue_slot,
    make_sequence,
)


class _SessionCtx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return None


async def _make_smtp_inbox(session, email: str = "relay@example.com"):
    inbox = await make_inbox(session, email=email, provider="smtp")
    session.add(
        SmtpAccount(
            inbox_id=inbox.id,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="user",
            smtp_password="pass",
            smtp_use_tls=True,
        )
    )
    await session.flush()
    return inbox


async def _make_due_slot(session, inbox, campaign=None):
    if campaign is None:
        campaign = await make_campaign(
            session,
            sending_days=[0, 1, 2, 3, 4, 5, 6],
            sending_hours_start="00:00",
            sending_hours_end="23:59",
        )
    await make_sequence(session, campaign.id)
    lead = await make_lead(session)
    cl = await make_campaign_lead(session, campaign.id, lead.id)
    await make_campaign_inbox(session, campaign.id, inbox.id)
    slot = await make_queue_slot(
        session, cl.id, inbox.id, scheduled_date=datetime.utcnow() - timedelta(minutes=1)
    )
    await session.flush()
    return slot


@pytest.mark.asyncio
async def test_smtp_auth_failure_pauses_inbox_and_labels_event(session, monkeypatch):
    inbox = await _make_smtp_inbox(session)
    slot = await _make_due_slot(session, inbox)

    events = []

    async def fake_webhook(db, event, data):
        events.append((event, data))

    monkeypatch.setattr("app.jobs.fire_webhook_event", fake_webhook)
    monkeypatch.setattr(
        "app.jobs.send_email",
        lambda **kwargs: SendFailure(
            error_type="auth_failed", message="SMTP authentication failed: 535"
        ),
    )
    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: _SessionCtx(session))
    jobs_mod._inbox_auth_cooldown_until.clear()

    await jobs_mod.send_slot_job(slot.id)

    # The inbox is paused so the next scan does not retry the broken credential.
    await session.refresh(inbox)
    assert inbox.paused is True

    # Pre-created log removed; slot retained for when credentials are fixed.
    assert (
        await session.execute(select(func.count(EmailLog.id)).where(EmailLog.inbox_id == inbox.id))
    ).scalar() == 0
    assert (
        await session.execute(select(func.count(QueueSlot.id)).where(QueueSlot.inbox_id == inbox.id))
    ).scalar() == 1

    ev = next((e for e in events if e[0] == "token_expired"), None)
    assert ev is not None, "auth failure must fire a token_expired event"
    assert ev[1]["provider"] == "smtp"
    assert ev[1]["error_type"] == "auth_failed"
    assert ev[1]["inbox_email"] == inbox.email


@pytest.mark.asyncio
async def test_auth_failure_circuit_breaker_skips_remaining_slots(session, monkeypatch):
    inbox = await _make_smtp_inbox(session, email="relay2@example.com")
    slot1 = await _make_due_slot(session, inbox)
    slot2 = await _make_due_slot(session, inbox)

    calls: list[str | None] = []

    def fake_send(**kwargs):
        calls.append(kwargs.get("to_email"))
        return SendFailure(error_type="auth_failed", message="535")

    async def fake_webhook(db, event, data):
        return None

    monkeypatch.setattr("app.jobs.fire_webhook_event", fake_webhook)
    monkeypatch.setattr("app.jobs.send_email", fake_send)
    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: _SessionCtx(session))
    jobs_mod._inbox_auth_cooldown_until.clear()

    await jobs_mod.send_slot_job(slot1.id)
    assert len(calls) == 1

    # Unpause to prove the in-memory cooldown (not just inbox.paused) is what
    # stops the second slot already dispatched in the same scan tick.
    inbox.paused = False
    await session.flush()
    await jobs_mod.send_slot_job(slot2.id)

    assert len(calls) == 1, "second slot must be skipped while the inbox is in auth cooldown"


@pytest.mark.asyncio
async def test_sender_display_name_renders_lead_variables(session, monkeypatch):
    """The From: name must support the same {{variables}} as subject/body."""
    inbox = await _make_smtp_inbox(session, email="brand@example.com")
    inbox.display_name = "{{name}} at Acme"
    await session.flush()
    slot = await _make_due_slot(session, inbox)

    captured: dict = {}

    def fake_send(**kwargs):
        captured.update(kwargs)
        return SendResult(message_id="<x>", thread_id="t")

    async def fake_webhook(db, event, data):
        return None

    monkeypatch.setattr("app.jobs.fire_webhook_event", fake_webhook)
    monkeypatch.setattr("app.jobs.send_email", fake_send)
    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: _SessionCtx(session))
    jobs_mod._inbox_auth_cooldown_until.clear()

    await jobs_mod.send_slot_job(slot.id)

    # make_lead() default name is "Test Lead"
    assert captured["from_name"] == "Test Lead at Acme"
    # one-click unsubscribe is on by default
    assert captured["list_unsubscribe_one_click"] is True
    assert captured["list_unsubscribe_url"]


@pytest.mark.asyncio
async def test_send_email_raising_marks_delivery_uncertain_and_blocks_retry(session, monkeypatch):
    """A transport exception may happen after SMTP accepted the message.

    Preserve the attempt as uncertain and remove its slot instead of retrying
    blindly and risking a duplicate.
    """
    inbox = await _make_smtp_inbox(session, email="boom@example.com")
    slot = await _make_due_slot(session, inbox)
    cl = await session.get(CampaignLead, slot.campaign_lead_id)
    await make_sequence(session, cl.campaign_id, position=1, subject="Future")
    await make_queue_slot(
        session,
        cl.id,
        inbox.id,
        sequence_index=1,
        scheduled_date=datetime.utcnow() + timedelta(hours=1),
    )

    def exploding_send(**kwargs):
        raise RuntimeError("transport exploded")

    async def fake_webhook(db, event, data):
        return None

    monkeypatch.setattr("app.jobs.fire_webhook_event", fake_webhook)
    monkeypatch.setattr("app.jobs.send_email", exploding_send)
    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: _SessionCtx(session))
    jobs_mod._inbox_auth_cooldown_until.clear()

    before = (
        await session.execute(select(func.count(EmailLog.id)).where(EmailLog.inbox_id == inbox.id))
    ).scalar()
    assert before == 0

    await jobs_mod.send_slot_job(slot.id)

    attempt = (
        await session.execute(select(EmailLog).where(EmailLog.inbox_id == inbox.id))
    ).scalar_one()
    assert attempt.delivery_state == "uncertain"
    assert "transport exploded" in attempt.delivery_error

    # The slot is removed so a later scan cannot retry automatically.
    slots = (
        await session.execute(select(func.count(QueueSlot.id)).where(QueueSlot.inbox_id == inbox.id))
    ).scalar()
    assert slots == 0
    await session.refresh(cl)
    assert cl.sending_paused is True


@pytest.mark.asyncio
async def test_stale_sending_attempt_becomes_uncertain_without_resend(session, monkeypatch):
    """Restart recovery must not resend a step with an unfinished attempt."""
    inbox = await _make_smtp_inbox(session, email="restart@example.com")
    slot = await _make_due_slot(session, inbox)
    cl = await session.get(CampaignLead, slot.campaign_lead_id)
    session.add(
        EmailLog(
            lead_id=cl.lead_id,
            campaign_id=cl.campaign_id,
            inbox_id=inbox.id,
            sequence_index=slot.sequence_index,
            subject="Potentially accepted",
            message_id="",
            delivery_state="sending",
        )
    )
    await session.commit()

    calls = []
    monkeypatch.setattr("app.jobs.send_email", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: _SessionCtx(session))

    await jobs_mod.send_slot_job(slot.id)

    assert calls == []
    attempt = (
        await session.execute(select(EmailLog).where(EmailLog.inbox_id == inbox.id))
    ).scalar_one()
    assert attempt.delivery_state == "uncertain"
    assert "automatic retry was blocked" in attempt.delivery_error
    assert await session.get(QueueSlot, slot.id) is None


@pytest.mark.asyncio
async def test_unresolved_custom_field_pauses_only_enrollment(session, monkeypatch):
    inbox = await _make_smtp_inbox(session, email="content@example.com")
    slot = await _make_due_slot(session, inbox)
    cl = await session.get(CampaignLead, slot.campaign_lead_id)
    sequence = (
        await session.execute(
            select(Sequence).where(
                Sequence.campaign_id == cl.campaign_id,
                Sequence.position == slot.sequence_index,
            )
        )
    ).scalar_one()
    sequence.subject = "{{assunto_1}}"
    sequence.body = "{{mensagem_1}}"
    await session.commit()

    calls = []
    monkeypatch.setattr("app.jobs.send_email", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: _SessionCtx(session))

    await jobs_mod.send_slot_job(slot.id)

    await session.refresh(cl)
    assert calls == []
    assert cl.sending_paused is True
    assert await session.get(QueueSlot, slot.id) is None
    assert (
        await session.execute(select(func.count(EmailLog.id)).where(EmailLog.inbox_id == inbox.id))
    ).scalar() == 0


@pytest.mark.asyncio
async def test_imap_health_holds_followup_then_auto_resumes(session, monkeypatch):
    inbox = await _make_smtp_inbox(session, email="reply-sync@example.com")
    smtp_account = (
        await session.execute(select(SmtpAccount).where(SmtpAccount.inbox_id == inbox.id))
    ).scalar_one()
    smtp_account.imap_host = "imap.example.com"

    campaign = await make_campaign(
        session,
        sending_days=[0, 1, 2, 3, 4, 5, 6],
        sending_hours_start="00:00",
        sending_hours_end="23:59",
    )
    await make_sequence(session, campaign.id, position=0)
    await make_sequence(session, campaign.id, position=1, subject="Follow-up")
    lead = await make_lead(session, email="followup-lead@example.com")
    cl = await make_campaign_lead(session, campaign.id, lead.id)
    await make_campaign_inbox(session, campaign.id, inbox.id)
    slot = await make_queue_slot(
        session,
        cl.id,
        inbox.id,
        sequence_index=1,
        scheduled_date=datetime.utcnow() - timedelta(minutes=1),
    )
    await session.commit()

    calls = []
    monkeypatch.setattr(
        "app.jobs.send_email",
        lambda **kwargs: calls.append(kwargs) or SendResult(message_id="<safe>", thread_id="thread"),
    )
    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: _SessionCtx(session))

    await jobs_mod.send_slot_job(slot.id)
    assert calls == []
    assert await session.get(QueueSlot, slot.id) is not None

    session.add(
        SmtpSyncState(
            inbox_id=inbox.id,
            last_attempt_at=datetime.utcnow(),
            last_success_at=datetime.utcnow(),
            last_sync_at=datetime.utcnow(),
            last_error="",
        )
    )
    await session.commit()

    await jobs_mod.send_slot_job(slot.id)
    assert len(calls) == 1
    assert await session.get(QueueSlot, slot.id) is None
    sent = (
        await session.execute(select(EmailLog).where(EmailLog.inbox_id == inbox.id))
    ).scalar_one()
    assert sent.delivery_state == "sent"


@pytest.mark.asyncio
async def test_precise_cadence_runtime_does_not_apply_legacy_minute_wait(session, monkeypatch):
    inbox = await _make_smtp_inbox(session, email="cadence-runtime@example.com")
    inbox.wait_minutes_between = 5
    inbox.min_wait_seconds = 20
    inbox.max_wait_seconds = 60
    slot = await _make_due_slot(session, inbox)
    cl = await session.get(CampaignLead, slot.campaign_lead_id)
    session.add(
        EmailLog(
            lead_id=cl.lead_id,
            campaign_id=cl.campaign_id,
            inbox_id=inbox.id,
            sequence_index=99,
            subject="Earlier send",
            message_id="<earlier>",
            delivery_state="sent",
            sent_at=datetime.utcnow() - timedelta(seconds=30),
        )
    )
    await session.commit()

    calls = []
    monkeypatch.setattr(
        "app.jobs.send_email",
        lambda **kwargs: calls.append(kwargs) or SendResult(message_id="<next>", thread_id="next"),
    )
    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: _SessionCtx(session))

    await jobs_mod.send_slot_job(slot.id)

    assert len(calls) == 1
    assert await session.get(QueueSlot, slot.id) is None


@pytest.mark.asyncio
async def test_unpause_inbox_clears_auth_cooldown(session):
    """A manual unpause must drop the 15-minute in-memory send cooldown."""
    from fastapi import BackgroundTasks

    from app.routers import inbox as inbox_router

    inbox = await _make_smtp_inbox(session, email="resume-me@example.com")
    inbox.paused = True
    await session.flush()

    jobs_mod._mark_inbox_auth_failure(inbox.id, jobs_mod.time_provider.now())
    assert jobs_mod._inbox_auth_cooldown_active(inbox.id, jobs_mod.time_provider.now()) is True

    await inbox_router.unpause_inbox(inbox.id, BackgroundTasks(), db=session)

    assert jobs_mod._inbox_auth_cooldown_active(inbox.id, jobs_mod.time_provider.now()) is False


@pytest.mark.asyncio
async def test_successful_smtp_test_clears_auth_cooldown(session, monkeypatch):
    """A passing SMTP connection test must drop the in-memory send cooldown."""
    from app.routers import smtp as smtp_router

    inbox = await _make_smtp_inbox(session, email="test-ok@example.com")
    acct = (
        await session.execute(select(SmtpAccount).where(SmtpAccount.inbox_id == inbox.id))
    ).scalar_one()

    class _R:
        def __init__(self, ok, error="", detail=""):
            self.ok = ok
            self.error = error
            self.detail = detail

    monkeypatch.setattr(
        smtp_router,
        "test_account_connections",
        lambda account: (_R(True, "", "SMTP ok"), _R(True, "", "skipped")),
    )
    jobs_mod._mark_inbox_auth_failure(inbox.id, jobs_mod.time_provider.now())
    assert jobs_mod._inbox_auth_cooldown_active(inbox.id, jobs_mod.time_provider.now()) is True

    result = await smtp_router.test_smtp_account(inbox.id, db=session, _user=object())

    assert result["ok"] is True
    assert acct.last_test_ok is True
    assert jobs_mod._inbox_auth_cooldown_active(inbox.id, jobs_mod.time_provider.now()) is False


@pytest.mark.asyncio
async def test_failed_smtp_test_keeps_auth_cooldown(session, monkeypatch):
    """A failing connection test must NOT clear the cooldown."""
    from app.routers import smtp as smtp_router

    inbox = await _make_smtp_inbox(session, email="test-bad@example.com")

    class _R:
        def __init__(self, ok, error="", detail=""):
            self.ok = ok
            self.error = error
            self.detail = detail

    monkeypatch.setattr(
        smtp_router,
        "test_account_connections",
        lambda account: (_R(False, "auth failed"), _R(True, "", "skipped")),
    )
    jobs_mod._mark_inbox_auth_failure(inbox.id, jobs_mod.time_provider.now())

    await smtp_router.test_smtp_account(inbox.id, db=session, _user=object())

    assert jobs_mod._inbox_auth_cooldown_active(inbox.id, jobs_mod.time_provider.now()) is True


@pytest.mark.asyncio
async def test_one_click_unsubscribe_flag_comes_from_campaign(session, monkeypatch):
    """campaign.add_one_click_unsubscribe=False keeps List-Unsubscribe but drops -Post."""
    inbox = await _make_smtp_inbox(session, email="brand2@example.com")
    campaign = await make_campaign(
        session,
        sending_days=[0, 1, 2, 3, 4, 5, 6],
        sending_hours_start="00:00",
        sending_hours_end="23:59",
    )
    campaign.add_one_click_unsubscribe = False
    await session.flush()
    slot = await _make_due_slot(session, inbox, campaign=campaign)

    captured: dict = {}

    def fake_send(**kwargs):
        captured.update(kwargs)
        return SendResult(message_id="<x>", thread_id="t")

    async def fake_webhook(db, event, data):
        return None

    monkeypatch.setattr("app.jobs.fire_webhook_event", fake_webhook)
    monkeypatch.setattr("app.jobs.send_email", fake_send)
    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: _SessionCtx(session))
    jobs_mod._inbox_auth_cooldown_until.clear()

    await jobs_mod.send_slot_job(slot.id)

    assert captured["list_unsubscribe_one_click"] is False
    assert captured["list_unsubscribe_url"]
