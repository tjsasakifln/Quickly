"""Focused regressions for scheduler status, inbox deletion, and cadence."""
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select

from app.main import api_status
from app.models import Inbox, QueueSlot
from app.queue_logic import reserve_slots_for_lead
from app.routers import inbox as inbox_router
from app.schemas import InboxCreate, InboxUpdate
from tests.conftest import (
    make_campaign,
    make_campaign_inbox,
    make_campaign_lead,
    make_inbox,
    make_lead,
    make_queue_slot,
    make_sequence,
)


@pytest.mark.asyncio
async def test_status_reports_slot_scan_next_run(monkeypatch):
    import app.jobs as jobs_mod
    from app import time as time_provider

    class FakeSchedule:
        running = True

        def __init__(self):
            self.requested_ids = []

        def get_job(self, job_id):
            self.requested_ids.append(job_id)
            return SimpleNamespace(next_run_time=__import__("datetime").datetime(2027, 1, 1, 9, 0))

    schedule = FakeSchedule()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(schedule=schedule)))
    monkeypatch.setattr(jobs_mod, "last_send_job_run", datetime(2027, 1, 1, 8, 59))
    monkeypatch.setattr(jobs_mod, "last_send_job_sent_count", 7)
    monkeypatch.setattr(time_provider, "utcnow", lambda: datetime(2027, 1, 1, 8, 58))

    result = await api_status(request, user=object())

    assert schedule.requested_ids == ["slot_scan"]
    assert result["next_send_job_run"] == "2027-01-01T09:00:00"
    assert result["last_send_job_run"] == "2027-01-01T08:59:00Z"
    assert result["last_send_job_sent_count"] == 7
    assert result["server_time"] == "2027-01-01T08:58:00Z"


@pytest.mark.asyncio
async def test_delete_inbox_with_queued_slot_requires_explicit_reassignment(session):
    inbox = await make_inbox(session, email="queued-delete@test.com")
    campaign = await make_campaign(session)
    lead = await make_lead(session, email="queued-delete-lead@test.com")
    enrollment = await make_campaign_lead(session, campaign.id, lead.id)
    await make_queue_slot(session, enrollment.id, inbox.id, 0)

    with pytest.raises(HTTPException, match="queued sends") as exc:
        await inbox_router.delete_inbox(inbox.id, db=session)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_precise_cadence_is_persisted_and_can_be_cleared(session):
    inbox = await inbox_router.create_inbox(
        InboxCreate(
            email="cadence-api@test.com",
            provider="smtp",
            min_wait_seconds=20,
            max_wait_seconds=60,
        ),
        db=session,
    )
    assert (inbox.min_wait_seconds, inbox.max_wait_seconds) == (20, 60)

    await inbox_router.update_inbox(
        inbox.id,
        InboxUpdate(min_wait_seconds=None, max_wait_seconds=None),
        BackgroundTasks(),
        db=session,
    )
    refreshed = (await session.execute(select(Inbox).where(Inbox.id == inbox.id))).scalar_one()
    assert (refreshed.min_wait_seconds, refreshed.max_wait_seconds) == (None, None)


@pytest.mark.asyncio
async def test_precise_cadence_keeps_each_scheduled_gap_in_range(session):
    inbox = await make_inbox(session, email="cadence-queue@test.com", wait_minutes_between=5)
    inbox.min_wait_seconds = 20
    inbox.max_wait_seconds = 20  # deterministic exact gap for this regression test
    campaign = await make_campaign(
        session,
        sending_days=[0, 1, 2, 3, 4, 5, 6],
        sending_hours_start="09:00",
        sending_hours_end="17:00",
    )
    lead = await make_lead(session, email="cadence-queue-lead@test.com")
    enrollment = await make_campaign_lead(session, campaign.id, lead.id)
    sequences = [
        await make_sequence(session, campaign.id, position=index, wait_days_after_previous=0)
        for index in range(3)
    ]

    await reserve_slots_for_lead(
        session,
        enrollment.id,
        campaign,
        inboxes=[(inbox.id, inbox, inbox.wait_minutes_between)],
        sequences=sequences,
        lead_id=lead.id,
        start_date=date(2027, 1, 1),
        cache={},
    )
    slots = (await session.execute(
        select(QueueSlot)
        .where(QueueSlot.campaign_lead_id == enrollment.id)
        .order_by(QueueSlot.sequence_index)
    )).scalars().all()

    assert len(slots) == 3
    assert [
        int((later.scheduled_date - earlier.scheduled_date).total_seconds())
        for earlier, later in zip(slots, slots[1:])
    ] == [20, 20]
