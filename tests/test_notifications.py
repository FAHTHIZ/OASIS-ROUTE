"""Notifications API and the mail path (THREAT_MODEL T13: Gmail data over-collection)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from oasis_route.config import Environment, Settings
from oasis_route.context import AppContext
from oasis_route.db.models import AuditEvent, Credential, MailGrant, Notification
from oasis_route.notifications import mail
from oasis_route.notifications.service import notify
from tests.helpers import auth, bootstrap, create_user

PROVIDER_NOTICE = """From: OpenAI <noreply@tm.openai.com>
To: dev@example.com
Subject: Your free credits expire on 2026-12-31
Date: Tue, 17 Nov 2026 10:00:00 +0000
Message-ID: <abc123@openai.com>
Content-Type: text/plain

Hi Dev, your account balance of $18.00 in grant credits will expire on 2026-12-31.
Your secret phone number 555-0100 and project sk-proj-abcdefghijklmnopqrstuv123456 appear here.
"""

FORWARDED = """From: Dev <dev@example.com>
To: notices@oasis.local
Subject: Fwd: heads up
Content-Type: text/plain

see below

---------- Forwarded message ---------
From: Anthropic <no-reply@mail.anthropic.com>
Date: Mon, 16 Nov 2026 09:00:00 +0000
Subject: Your API key was disabled

We detected that an API key was exposed publicly and it has been disabled. Body secret: hunter2.
"""


async def _user(client: httpx.AsyncClient) -> tuple[str, str, str]:
    admin = await bootstrap(client)
    u = await create_user(client, admin["api_key"]["secret"], "dev@example.com")
    return admin["api_key"]["secret"], u["api_key"]["secret"], u["project"]["id"]


# ------------------------------------------------------------------------------- notifications API
async def test_notifications_scoped_read_and_delete(client: httpx.AsyncClient, ctx: AppContext) -> None:
    admin, key, pid = await _user(client)
    other = await create_user(client, admin, "other@example.com")
    async with ctx.db.transaction() as s:
        mine = await notify(s, kind="k", title="mine", project_id=pid)
        theirs = await notify(s, kind="k", title="theirs", project_id=other["project"]["id"])
        mine_id, theirs_id = mine.id, theirs.id
    r = await client.get("/api/v1/notifications", headers=auth(key))
    assert [n["title"] for n in r.json()] == ["mine"]
    assert (
        await client.post(f"/api/v1/notifications/{theirs_id}/read", headers=auth(key))
    ).status_code == 404
    assert (await client.delete(f"/api/v1/notifications/{theirs_id}", headers=auth(key))).status_code == 404
    r = await client.post(f"/api/v1/notifications/{mine_id}/read", headers=auth(key))
    assert r.status_code == 200 and r.json()["read_at"]
    assert (await client.get("/api/v1/notifications?unread=true", headers=auth(key))).json() == []
    assert (await client.delete(f"/api/v1/notifications/{mine_id}", headers=auth(key))).status_code == 204
    assert (await client.get("/api/v1/notifications", headers=auth(key))).json() == []


async def test_notification_facts_are_redacted(ctx: AppContext, client: httpx.AsyncClient) -> None:
    _, _, pid = await _user(client)
    async with ctx.db.transaction() as s:
        n = await notify(
            s, kind="k", title="t", project_id=pid, facts={"api_key": "sk-live-abcdef0123456789"}
        )
        assert "sk-live-abcdef0123456789" not in json.dumps(n.facts)


# ------------------------------------------------------------------------------- classifier
def test_classifier_requires_provider_domain_and_category() -> None:
    from oasis_route.db.models import Provider

    provs = [Provider(id="openai", name="OpenAI", adapter_key="x", official_domains=["openai.com"])]
    f = mail.classify("x <a@tm.openai.com>", "Credits expire soon", "on 2026-12-31 ($5.00)", provs)
    assert f is not None and f.category == "credit_expiry" and f.dates == ["2026-12-31"]
    assert f.amounts_usd == ["5.00"]
    # Look-alike and unrelated senders are discarded, whatever the content says.
    assert mail.classify("x <a@openai.com.evil.test>", "Credits expire", "", provs) is None
    assert mail.classify("x <a@notopenai.com>", "Credits expire", "", provs) is None
    assert mail.classify("x <a@openai.com>", "Lunch on Friday?", "", provs) is None
    sec = mail.classify("x <a@openai.com>", "Your API key was revoked", "", provs)
    assert sec is not None and sec.category == "key_security" and sec.severity == "critical"
    dep = mail.classify("x <a@openai.com>", "gpt-4-0613 will be deprecated", "", provs)
    assert dep is not None and dep.category == "model_deprecation" and dep.models == ["gpt-4-0613"]


async def test_forwarded_message_stores_only_facts(client: httpx.AsyncClient, ctx: AppContext) -> None:
    _, key, _ = await _user(client)
    r = await client.post("/api/v1/mail/forwarded", json={"message": PROVIDER_NOTICE}, headers=auth(key))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["stored"] is True
    assert out["facts"]["category"] == "credit_expiry" and out["facts"]["provider_id"] == "openai"
    assert out["facts"]["dates"] == ["2026-12-31"] and out["facts"]["amounts_usd"] == ["18.00"]
    async with ctx.db.session() as s:
        everything = json.dumps(
            [[n.title, n.body, n.facts] for n in (await s.execute(select(Notification))).scalars()]
            + [[a.action, a.details] for a in (await s.execute(select(AuditEvent))).scalars()],
            default=str,
        )
    # Neither the raw body, nor the subject, nor anything sensitive in the body was persisted.
    for leaked in ("555-0100", "sk-proj-", "Hi Dev", "Your free credits expire", "abc123"):
        assert leaked not in everything
    # De-duplicated on resubmission.
    r = await client.post("/api/v1/mail/forwarded", json={"message": PROVIDER_NOTICE}, headers=auth(key))
    assert r.json() == {"stored": False, "reason": "duplicate", "category": "credit_expiry"}


async def test_forwarded_inner_message_and_non_provider_discarded(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    _, key, _ = await _user(client)
    r = await client.post("/api/v1/mail/forwarded", json={"message": FORWARDED}, headers=auth(key))
    assert r.json()["stored"] is True and r.json()["facts"]["category"] == "key_security"
    assert r.json()["facts"]["provider_id"] == "anthropic"
    spam = "From: x@shop.test\nSubject: Your API key was revoked\n\nclick here"
    r = await client.post("/api/v1/mail/forwarded", json={"message": spam}, headers=auth(key))
    assert r.json() == {"stored": False, "reason": "not_a_recognised_provider_notice"}
    async with ctx.db.session() as s:
        assert "hunter2" not in json.dumps(
            [n.facts for n in (await s.execute(select(Notification))).scalars()]
        )
    big = "x" * (mail.MAX_FORWARDED_BYTES + 1)
    assert (
        await client.post("/api/v1/mail/forwarded", json={"message": big}, headers=auth(key))
    ).status_code == 422


async def test_mail_facts_have_retention_and_can_be_purged(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    _, key, _ = await _user(client)
    await client.post("/api/v1/mail/forwarded", json={"message": PROVIDER_NOTICE}, headers=auth(key))
    async with ctx.db.session() as s:
        n = (await s.execute(select(Notification))).scalar_one()
        assert n.delete_after is not None
        span = (n.delete_after - n.created_at).total_seconds()
        assert abs(span - ctx.settings.mail_facts_retention_days * 86400) < 60
    r = await client.delete("/api/v1/mail/data", headers=auth(key))
    assert r.json() == {"deleted": 1}


# ------------------------------------------------------------------------------- Gmail gate
def test_gmail_gate_states() -> None:
    import base64

    key = base64.b64encode(b"k" * 32).decode()
    assert mail.gmail_gate(Settings(gmail_enabled=False)) == (False, "gmail_disabled")
    assert mail.gmail_gate(Settings(gmail_enabled=True)) == (False, "gmail_not_configured")
    prod: dict[str, Any] = dict(
        environment=Environment.PRODUCTION,
        master_key=key,
        api_key_pepper="p",
        database_url="postgresql+asyncpg://u:p@db/x",
        gmail_enabled=True,
        gmail_client_id="c",
        gmail_client_secret="s",
        gmail_redirect_uri="https://x/cb",
    )
    with pytest.raises(ValidationError, match="Gmail"):
        Settings(**prod)  # production refuses to start without a compliance attestation
    ok = Settings(**prod, gmail_compliance_attestation="CASA-2026-0001")
    assert mail.gmail_gate(ok) == (True, "ok")


async def test_gmail_disabled_by_default(client: httpx.AsyncClient) -> None:
    _, key, _ = await _user(client)
    st = (await client.get("/api/v1/mail/status", headers=auth(key))).json()
    assert st["gmail"]["available"] is False and st["gmail"]["reason"] == "gmail_disabled"
    assert st["gmail"]["scopes"] == ["https://www.googleapis.com/auth/gmail.metadata"]
    r = await client.post(
        "/api/v1/mail/gmail/consent",
        json={"accept": True, "consent_version": mail.CONSENT_VERSION},
        headers=auth(key),
    )
    assert r.status_code == 403


class FakeGoogle:
    def __init__(self) -> None:
        self.granted_scope = mail.GMAIL_SCOPE
        self.revoked: list[str] = []
        self.requests: list[httpx.Request] = []
        self.refresh_ok = True
        self.messages = {
            "m1": ("OpenAI <noreply@tm.openai.com>", "You have reached 90% of your usage limit"),
            "m2": ("Friend <pal@example.com>", "Your API key was revoked"),
        }

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        url = str(req.url)
        if url.startswith(mail.TOKEN_URL):
            form = parse_qs(req.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(
                    200,
                    json={"access_token": "at", "refresh_token": "rt-secret", "scope": self.granted_scope},
                )
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at2"})
        if url.startswith(mail.REVOKE_URL):
            self.revoked.append(parse_qs(req.content.decode())["token"][0])
            return httpx.Response(200)
        if req.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": k} for k in self.messages]})
        mid = req.url.path.rsplit("/", 1)[1]
        frm, subj = self.messages[mid]
        return httpx.Response(
            200,
            json={
                "id": mid,
                "internalDate": "1794900000000",
                "snippet": "SNIPPET-MUST-NOT-BE-STORED",
                "payload": {"headers": [{"name": "From", "value": frm}, {"name": "Subject", "value": subj}]},
            },
        )


@pytest.fixture
def gmail(ctx: AppContext) -> FakeGoogle:
    fake = FakeGoogle()
    ctx.settings.gmail_enabled = True
    ctx.settings.gmail_client_id = "cid"
    ctx.settings.gmail_client_secret = SecretStr("csecret")
    ctx.settings.gmail_redirect_uri = "https://oasis.example/api/v1/mail/gmail/callback"
    ctx.google_transport = httpx.MockTransport(fake.handler)
    return fake


async def _consent(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    r = await client.post(
        "/api/v1/mail/gmail/consent",
        json={"accept": True, "consent_version": mail.CONSENT_VERSION},
        headers=auth(key),
    )
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()
    return data


async def test_gmail_consent_callback_sync_and_revoke(
    client: httpx.AsyncClient, ctx: AppContext, gmail: FakeGoogle
) -> None:
    _, key, _ = await _user(client)
    r = await client.post(
        "/api/v1/mail/gmail/consent", json={"accept": True, "consent_version": "old"}, headers=auth(key)
    )
    assert r.status_code == 422
    c = await _consent(client, key)
    q = parse_qs(urlparse(c["authorization_url"]).query)
    assert q["scope"] == [mail.GMAIL_SCOPE] and q["access_type"] == ["offline"]
    # Tampered state is refused.
    bad = q["state"][0][:-2] + "00"
    assert (
        await client.get("/api/v1/mail/gmail/callback", params={"code": "c", "state": bad})
    ).status_code == 403
    r = await client.get("/api/v1/mail/gmail/callback", params={"code": "c", "state": q["state"][0]})
    assert r.status_code == 200, r.text
    async with ctx.db.session() as s:
        g = await s.get(MailGrant, c["grant_id"])
        assert g is not None and g.status == "active" and g.credential_id
        cred = await s.get(Credential, g.credential_id)
        assert cred is not None and b"rt-secret" not in cred.ciphertext
    # Replay of the same state fails: the grant is no longer pending.
    assert (
        await client.get("/api/v1/mail/gmail/callback", params={"code": "c", "state": q["state"][0]})
    ).status_code == 404

    r = await client.post(f"/api/v1/mail/gmail/grants/{c['grant_id']}/sync", headers=auth(key))
    assert r.status_code == 200 and r.json() == {"status": "ok", "scanned": 2, "recorded": 1}
    # Only metadata was requested, and only From/Subject/Date headers.
    fetches = [x for x in gmail.requests if "/messages/" in x.url.path]
    assert fetches and all(x.url.params["format"] == "metadata" for x in fetches)
    assert all(set(x.url.params.get_list("metadataHeaders")) == {"From", "Subject", "Date"} for x in fetches)
    async with ctx.db.session() as s:
        notes = list((await s.execute(select(Notification))).scalars())
        assert [n.kind for n in notes] == ["mail.quota_warning"] and notes[0].facts["percent"] == 90
        dump = json.dumps([[n.title, n.body, n.facts] for n in notes])
        assert "SNIPPET" not in dump and "usage limit" not in dump
    # Second sync records nothing new.
    r = await client.post(f"/api/v1/mail/gmail/grants/{c['grant_id']}/sync", headers=auth(key))
    assert r.json()["recorded"] == 0

    r = await client.delete(f"/api/v1/mail/gmail/grants/{c['grant_id']}?purge=true", headers=auth(key))
    assert r.json() == {"status": "revoked", "purged_notifications": 1}
    assert gmail.revoked == ["rt-secret"]
    async with ctx.db.session() as s:
        g = await s.get(MailGrant, c["grant_id"])
        assert g is not None and g.status == "revoked" and g.credential_id is None and g.revoked_at
        assert list((await s.execute(select(Credential))).scalars()) == []
        actions = {a.action for a in (await s.execute(select(AuditEvent))).scalars()}
        assert {
            "mail.consent_started",
            "mail.grant_activated",
            "mail.synced",
            "mail.grant_revoked",
        } <= actions


async def test_gmail_overbroad_scope_is_refused_and_revoked(
    client: httpx.AsyncClient, ctx: AppContext, gmail: FakeGoogle
) -> None:
    _, key, _ = await _user(client)
    gmail.granted_scope = f"{mail.GMAIL_SCOPE} https://www.googleapis.com/auth/gmail.readonly"
    c = await _consent(client, key)
    state = parse_qs(urlparse(c["authorization_url"]).query)["state"][0]
    r = await client.get("/api/v1/mail/gmail/callback", params={"code": "c", "state": state})
    assert r.status_code == 403
    assert gmail.revoked == ["rt-secret"]
    async with ctx.db.session() as s:
        g = await s.get(MailGrant, c["grant_id"])
        assert g is not None and g.status == "rejected" and g.credential_id is None


async def test_gmail_revoked_upstream_marks_grant_and_notifies(
    client: httpx.AsyncClient, ctx: AppContext, gmail: FakeGoogle
) -> None:
    _, key, _ = await _user(client)
    c = await _consent(client, key)
    state = parse_qs(urlparse(c["authorization_url"]).query)["state"][0]
    await client.get("/api/v1/mail/gmail/callback", params={"code": "c", "state": state})
    gmail.refresh_ok = False
    r = await client.post(f"/api/v1/mail/gmail/grants/{c['grant_id']}/sync", headers=auth(key))
    assert r.json() == {"status": "error", "recorded": 0}
    async with ctx.db.session() as s:
        g = await s.get(MailGrant, c["grant_id"])
        assert g is not None and g.status == "error"
        assert (
            await s.execute(select(Notification).where(Notification.kind == "mail.grant_error"))
        ).scalar_one()


async def test_other_users_cannot_touch_a_grant(
    client: httpx.AsyncClient, ctx: AppContext, gmail: FakeGoogle
) -> None:
    admin, key, _ = await _user(client)
    c = await _consent(client, key)
    other = (await create_user(client, admin, "o@example.com"))["api_key"]["secret"]
    assert (
        await client.delete(f"/api/v1/mail/gmail/grants/{c['grant_id']}", headers=auth(other))
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/mail/gmail/grants/{c['grant_id']}/sync", headers=auth(other))
    ).status_code == 404
