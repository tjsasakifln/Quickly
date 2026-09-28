import csv
import io
from datetime import datetime, timedelta

import pytest
from fastapi import BackgroundTasks, UploadFile
from sqlalchemy import func, select

import app.jobs as jobs_mod
from app.models import CampaignLead, EmailLog, Lead, QueueSlot, SequenceVariant, SmtpAccount
from app.routers.campaigns import (
    DeliveryResolutionRequest,
    PreviewRequest,
    import_campaign_leads,
    preview_email,
    resolve_uncertain_delivery,
)
from app.sender import render_body
from tests.conftest import (
    make_campaign,
    make_campaign_lead,
    make_campaign_inbox,
    make_email_log,
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


def _csv_upload(rows: list[list[str]]) -> UploadFile:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(rows)
    return UploadFile(
        filename="leads.csv",
        file=io.BytesIO(output.getvalue().encode("utf-8")),
    )


@pytest.mark.asyncio
async def test_csv_validates_each_row_preserves_full_content_and_preview_matches_render(
    session, monkeypatch
):
    campaign = await make_campaign(session)
    inbox = await make_inbox(session, email="csv-template@test.com")
    await make_campaign_inbox(session, campaign.id, inbox.id)
    sequence = await make_sequence(
        session,
        campaign.id,
        position=0,
        subject="{{assunto_1}}",
        body="{{mensagem_1}}",
    )
    existing = await make_lead(session, email="valido@example.com", name="")
    existing.provider = "Other"
    await session.flush()

    subject = 'Proposta para São Paulo — "CONFENGE"'
    body = 'Olá, Tiago!\n\nPrimeiro parágrafo com acentuação.\nSegundo: "texto citado".'
    rows = [
        ["email", "name", "assunto_1", "mensagem_1"],
        ["valido@example.com", "José", subject, body],
        ["invalido@example.com", "Maria", "Assunto sem corpo", ""],
    ]

    async def _provider(_email):
        return "Other"

    monkeypatch.setattr("app.email_provider.detect_provider_for_email", _provider)
    confirmation = await import_campaign_leads(
        campaign.id,
        _csv_upload(rows),
        confirm_only=True,
        db=session,
    )

    assert confirmation["required_fields"] == ["assunto_1", "mensagem_1"]
    assert confirmation["total_valid"] == 1
    assert confirmation["total_flagged"] == 1
    assert confirmation["results"] == [
        {"row": 2, "email": "valido@example.com", "status": "valid"},
        {
            "row": 3,
            "email": "invalido@example.com",
            "missing_fields": ["mensagem_1"],
            "status": "missing_required_fields",
            "detail": "Missing content required by campaign templates",
        },
    ]

    result = await import_campaign_leads(campaign.id, _csv_upload(rows), db=session)
    assert result["added"] == 1
    assert result["errors"] == 1
    assert result["total_rows"] == 2
    assert [row["status"] for row in result["results"]] == [
        "added", "missing_required_fields",
    ]

    valid_lead = (
        await session.execute(select(Lead).where(Lead.email == "valido@example.com"))
    ).scalar_one()
    assert valid_lead.name == "José"
    assert valid_lead.custom_data["assunto_1"] == subject
    assert valid_lead.custom_data["mensagem_1"] == body
    invalid_count = await session.scalar(
        select(func.count(Lead.id)).where(Lead.email == "invalido@example.com")
    )
    assert invalid_count == 0

    enrollment_count = await session.scalar(
        select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id == campaign.id)
    )
    slot_count = await session.scalar(
        select(func.count(QueueSlot.id))
        .join(CampaignLead, CampaignLead.id == QueueSlot.campaign_lead_id)
        .where(CampaignLead.campaign_id == campaign.id)
    )
    assert enrollment_count == 1
    assert slot_count == 1

    preview = await preview_email(
        campaign.id,
        PreviewRequest(sequence_id=sequence.id, lead_id=valid_lead.id),
        db=session,
    )
    lead_data = {"name": "José", "email": valid_lead.email, **valid_lead.custom_data}
    assert preview["subject"] == render_body(sequence.subject, lead_data) == subject
    assert preview["body"] == render_body(sequence.body, lead_data) == body


@pytest.mark.asyncio
async def test_csv_requires_fields_used_by_enabled_standard_variants_only(session, monkeypatch):
    campaign = await make_campaign(session)
    sequence = await make_sequence(
        session,
        campaign.id,
        subject="{{base_subject}}",
        body="{{base_body}}",
    )
    session.add_all([
        SequenceVariant(
            sequence_id=sequence.id,
            label="enabled",
            subject="{{variant_subject}}",
            body="{{variant_body}}",
            enabled=True,
        ),
        SequenceVariant(
            sequence_id=sequence.id,
            label="disabled",
            subject="{{disabled_subject}}",
            body="{{disabled_body}}",
            enabled=False,
        ),
    ])
    # Personalized fallbacks are not CSV requirements; they have their own
    # per-lead override lifecycle.
    await make_sequence(
        session,
        campaign.id,
        position=1,
        sequence_type="personalized",
        fallback_subject="{{fallback_subject}}",
        fallback_body="{{fallback_body}}",
    )
    await session.flush()

    async def _provider(_email):
        return "Other"

    monkeypatch.setattr("app.email_provider.detect_provider_for_email", _provider)
    rows = [
        ["email", "base_subject", "base_body", "variant_subject", "variant_body"],
        ["ok@example.com", "A", "B", "C", "D"],
    ]
    confirmation = await import_campaign_leads(
        campaign.id,
        _csv_upload(rows),
        confirm_only=True,
        db=session,
    )
    assert confirmation["total_valid"] == 1
    assert confirmation["required_fields"] == [
        "base_body", "base_subject", "variant_body", "variant_subject",
    ]


@pytest.mark.asyncio
async def test_csv_reuses_case_insensitive_lead_with_multiple_prior_enrollments(session):
    prior_a = await make_campaign(session, name="Prior A")
    prior_b = await make_campaign(session, name="Prior B")
    target = await make_campaign(session, name="Target")
    await make_sequence(session, target.id, subject="Hello", body="Body")

    existing = await make_lead(session, email="Mixed.Case@Example.com")
    existing.provider = "Other"
    existing.custom_data = {"account": "original"}
    await make_campaign_lead(session, prior_a.id, existing.id)
    await make_campaign_lead(session, prior_b.id, existing.id)
    await session.flush()

    rows = [
        ["email", "name", "account"],
        ["mixed.case@example.com", "Existing", "must-not-overwrite-on-skip"],
    ]
    skipped = await import_campaign_leads(
        target.id,
        _csv_upload(rows),
        skip_duplicates=True,
        db=session,
    )
    assert skipped["already_enrolled"] == 1
    assert skipped["errors"] == 0
    await session.refresh(existing)
    assert existing.custom_data == {"account": "original"}

    added = await import_campaign_leads(
        target.id,
        _csv_upload(rows),
        skip_duplicates=False,
        db=session,
    )
    assert added["added"] == 1
    assert added["errors"] == 0
    assert await session.scalar(
        select(func.count(Lead.id)).where(func.lower(Lead.email) == "mixed.case@example.com")
    ) == 1


@pytest.mark.asyncio
async def test_marking_uncertain_final_delivery_sent_completes_enrollment(session):
    campaign = await make_campaign(session)
    await make_sequence(session, campaign.id, position=0)
    inbox = await make_inbox(session, email="uncertain-resolve-inbox@example.com")
    lead = await make_lead(session, email="uncertain-resolve@example.com")
    enrollment = await make_campaign_lead(session, campaign.id, lead.id)
    enrollment.sending_paused = True
    slot = await make_queue_slot(session, enrollment.id, inbox.id, 0)
    log = await make_email_log(session, lead.id, campaign.id, sequence_index=0)
    log.delivery_state = "uncertain"
    await session.flush()

    response = await resolve_uncertain_delivery(
        campaign.id,
        log.id,
        DeliveryResolutionRequest(action="mark_sent"),
        BackgroundTasks(),
        db=session,
    )

    assert response["delivery_state"] == "sent"
    assert enrollment.enrollment_status == "completed"
    assert enrollment.sending_paused is False
    assert await session.get(QueueSlot, slot.id) is None


@pytest.mark.asyncio
async def test_explicit_uncertain_retry_unpauses_without_claiming_delivery(session):
    campaign = await make_campaign(session)
    await make_sequence(session, campaign.id, position=0)
    lead = await make_lead(session, email="uncertain-retry@example.com")
    enrollment = await make_campaign_lead(session, campaign.id, lead.id)
    enrollment.sending_paused = True
    log = await make_email_log(session, lead.id, campaign.id, sequence_index=0)
    log.delivery_state = "uncertain"
    await session.flush()

    response = await resolve_uncertain_delivery(
        campaign.id,
        log.id,
        DeliveryResolutionRequest(action="retry"),
        BackgroundTasks(),
        db=session,
    )

    assert response["delivery_state"] == "failed"
    assert enrollment.enrollment_status == "active"
    assert enrollment.sending_paused is False


@pytest.mark.asyncio
async def test_send_job_blocks_legacy_row_with_unresolved_required_content(
    session, monkeypatch
):
    inbox = await make_inbox(
        session, email="safety@example.com", provider="smtp"
    )
    session.add(
        SmtpAccount(
            inbox_id=inbox.id,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="safety@example.com",
            smtp_password="secret",
            smtp_use_tls=True,
        )
    )
    campaign = await make_campaign(
        session,
        sending_days=[0, 1, 2, 3, 4, 5, 6],
        sending_hours_start="00:00",
        sending_hours_end="23:59",
    )
    await make_sequence(
        session,
        campaign.id,
        subject="{{assunto_1}}",
        body="{{mensagem_1}}",
    )
    lead = await make_lead(session, email="legacy@example.com", name="Legacy")
    lead.custom_data = {"assunto_1": "Subject exists"}
    enrollment = await make_campaign_lead(session, campaign.id, lead.id)
    await make_campaign_inbox(session, campaign.id, inbox.id)
    slot = await make_queue_slot(
        session,
        enrollment.id,
        inbox.id,
        scheduled_date=datetime.utcnow() - timedelta(minutes=1),
    )
    await session.flush()

    send_calls = []

    def _must_not_send(**kwargs):
        send_calls.append(kwargs)
        raise AssertionError("send_email must not be called with unresolved content")

    monkeypatch.setattr(jobs_mod, "AsyncSessionLocal", lambda: _SessionCtx(session))
    monkeypatch.setattr(jobs_mod, "send_email", _must_not_send)
    jobs_mod._inbox_auth_cooldown_until.clear()

    await jobs_mod.send_slot_job(slot.id)

    await session.refresh(enrollment)
    assert send_calls == []
    assert enrollment.sending_paused is True
    assert await session.scalar(select(func.count(QueueSlot.id))) == 0
    assert await session.scalar(select(func.count(EmailLog.id))) == 0
