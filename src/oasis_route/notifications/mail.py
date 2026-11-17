"""Provider-notice classification for user-forwarded mail and the optional, gated Gmail integration.

Data minimisation is the design constraint (SECURITY_AND_COMPLIANCE.md section 6, THREAT_MODEL T13):

* Only messages whose sender belongs to a known provider's official domain are processed at all.
* Classification reads the sender, subject, date and (forwarded path only) the body text in memory; the
  raw message, body, and subject are never persisted. Only a small set of extracted facts is stored, as
  an in-app notification with a retention deadline.
* Gmail uses ``gmail.metadata`` (headers only, no bodies) and is disabled unless explicitly enabled; in
  production it additionally requires a recorded Google verification / security assessment attestation.
* Nothing here sends mail, creates provider accounts, or accepts terms.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import message_from_string, policy
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.config import Environment, Settings
from oasis_route.context import AppContext
from oasis_route.db.models import Credential, MailGrant, Notification, Provider, User, utcnow
from oasis_route.domain.errors import Conflict, Forbidden, NotFound, ValidationFailed
from oasis_route.identity import audit
from oasis_route.security.redaction import redact

log = logging.getLogger(__name__)
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
SCOPES = (GMAIL_SCOPE,)
CONSENT_VERSION = "2026-11-01"
CONSENT_TEXT = (
    "Oasis Route will read only the sender, subject and date headers of messages in your Gmail inbox "
    "(Gmail metadata scope; message bodies are not accessible). Messages from known AI provider domains are "
    "classified into notices such as quota warnings, credit expiry, key security alerts, terms or pricing "
    "changes, and model deprecations. Only the notice type, provider, and extracted dates/amounts/model "
    "names are stored, for a limited retention period. Headers, subjects and bodies are never stored. "
    "Oasis Route never sends mail, creates accounts, or accepts terms for you. You can revoke access and "
    "delete all derived data at any time."
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint URL, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_FORWARDED_BYTES = 256_000
STATE_TTL_S = 600
SYNC_PAGE = 50

# Ordered: the first matching category wins (security first; it is the most actionable).
_CATEGORIES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "key_security",
        "critical",
        re.compile(
            r"\b(api[ -]?key|secret|credential|token)s?\b.{0,40}\b(revoked|disabled|leaked|exposed|"
            r"compromised|rotated|deactivated)\b|\bsecurity (alert|notice)\b",
            re.I | re.S,
        ),
    ),
    (
        "quota_exhausted",
        "warning",
        re.compile(
            r"\b(quota|usage limit|rate limit|spending limit|credits?)\b.{0,30}\b(exceeded|exhausted|reached|"
            r"depleted|used up|run out)\b|\bout of credits\b|\bno (remaining )?credits\b",
            re.I | re.S,
        ),
    ),
    (
        "credit_expiry",
        "warning",
        re.compile(r"\b(credits?|trial|free tier|grant)\b.{0,40}\b(expir\w*|ends?|ending)\b", re.I | re.S),
    ),
    (
        "quota_warning",
        "info",
        re.compile(
            r"\b(approaching|nearing|close to)\b.{0,30}\b(quota|limit)\b|\b\d{1,3}\s?% of (your )?"
            r"(?:(?:usage|rate|spending|monthly|daily) )?(quota|limit|credits|budget)\b|"
            r"\busage (alert|warning)\b",
            re.I | re.S,
        ),
    ),
    (
        "model_deprecation",
        "info",
        re.compile(r"\b(deprecat\w*|retir\w*|sunset\w*|discontinu\w*)\b", re.I),
    ),
    (
        "terms_change",
        "info",
        re.compile(
            r"\b(terms of (service|use)|usage policy|privacy policy|pricing)\b.{0,30}\b(update|change)\w*\b|"
            r"\bupdates? to (our|the) (terms|policies|pricing)\b",
            re.I | re.S,
        ),
    ),
    (
        "billing",
        "info",
        re.compile(r"\b(invoice|receipt|payment (failed|declined|received)|billing)\b", re.I),
    ),
)
_DATE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\b|"
    r"\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2},? \d{4})\b",
    re.I,
)
_AMOUNT = re.compile(r"(?:US)?\$\s?(\d{1,7}(?:,\d{3})*(?:\.\d{1,2})?)")
_PERCENT = re.compile(r"\b(\d{1,3})\s?%")
_MODEL = re.compile(r"\b([a-z][a-z0-9]*(?:[-.][a-z0-9]+){1,6})\b", re.I)
_MODEL_HINT = re.compile(
    r"(gpt|claude|gemini|llama|mistral|mixtral|qwen|deepseek|command|grok|phi|gemma)", re.I
)
_FWD_MARK = re.compile(r"^-{2,}\s*(forwarded message|original message)\s*-{2,}\s*$", re.I | re.M)


@dataclass
class MailFacts:
    provider_id: str
    category: str
    severity: str
    sender_domain: str
    received_at: datetime | None = None
    dates: list[str] = field(default_factory=list)
    amounts_usd: list[str] = field(default_factory=list)
    percent: int | None = None
    models: list[str] = field(default_factory=list)

    def as_facts(self, source: str, message_ref: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": source,
            "provider_id": self.provider_id,
            "category": self.category,
            "sender_domain": self.sender_domain,
            "message_ref": message_ref,
        }
        if self.received_at:
            out["received_at"] = self.received_at.isoformat()
        if self.dates:
            out["dates"] = self.dates[:5]
        if self.amounts_usd:
            out["amounts_usd"] = self.amounts_usd[:5]
        if self.percent is not None:
            out["percent"] = self.percent
        if self.models:
            out["models"] = self.models[:10]
        return out


# ------------------------------------------------------------------------------------------- gating
def gmail_gate(settings: Settings) -> tuple[bool, str]:
    """Is the Gmail OAuth path allowed to run at all?"""
    if not settings.gmail_enabled:
        return False, "gmail_disabled"
    if settings.environment is Environment.PRODUCTION and not settings.gmail_compliance_attestation:
        return False, "compliance_attestation_missing"  # also enforced by Settings validation
    if not (settings.gmail_client_id and settings.gmail_client_secret and settings.gmail_redirect_uri):
        return False, "gmail_not_configured"
    return True, "ok"


def _require_gmail(settings: Settings) -> None:
    ok, reason = gmail_gate(settings)
    if not ok:
        raise Forbidden("Gmail integration is not enabled on this instance", details={"reason": reason})


# ------------------------------------------------------------------------------------------- classifier
def _domain(addr: str) -> str:
    _, email_addr = parseaddr(addr or "")
    return email_addr.rpartition("@")[2].strip().lower().rstrip(".")


def match_provider(sender_domain: str, providers: list[Provider]) -> Provider | None:
    if not sender_domain:
        return None
    for p in providers:
        for d in p.official_domains or []:
            d = str(d).lower().lstrip(".")
            if sender_domain == d or sender_domain.endswith("." + d):
                return p
    return None


def classify(
    sender: str, subject: str, text: str, providers: list[Provider], *, date: datetime | None = None
) -> MailFacts | None:
    """Classify one provider notice. Returns None (discard) unless from a provider domain and matched."""
    domain = _domain(sender)
    prov = match_provider(domain, providers)
    if prov is None:
        return None
    hay = f"{subject}\n{text}"[:50_000]
    hit = next(((c, sev) for c, sev, rx in _CATEGORIES if rx.search(hay)), None)
    if hit is None:
        return None
    category, severity = hit
    dates = [m.group(1) or m.group(2) for m in _DATE.finditer(hay)]
    amounts = [m.group(1).replace(",", "") for m in _AMOUNT.finditer(hay)]
    pct = next((int(m.group(1)) for m in _PERCENT.finditer(hay) if 0 < int(m.group(1)) <= 100), None)
    models = sorted({m.group(1).lower() for m in _MODEL.finditer(hay) if _MODEL_HINT.search(m.group(1))})
    return MailFacts(
        provider_id=prov.id,
        category=category,
        severity=severity,
        sender_domain=domain,
        received_at=date,
        dates=list(dict.fromkeys(dates)),
        amounts_usd=list(dict.fromkeys(amounts)),
        percent=pct if category in ("quota_warning", "quota_exhausted") else None,
        models=models if category == "model_deprecation" else [],
    )


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def parse_forwarded(raw: str) -> tuple[str, str, str, datetime | None, str]:
    """Return (sender, subject, body_text, date, message_id) of the *original* provider message.

    Accepts either a full RFC 822 message ("show original") or a pasted forward. When the outer message is a
    forward (the user is the sender), the inner ``From:`` / ``Subject:`` / ``Date:`` block is used instead.
    """
    msg = message_from_string(raw, policy=policy.default)
    sender, subject = str(msg.get("from", "") or ""), str(msg.get("subject", "") or "")
    date, mid = _parse_date(msg.get("date")), str(msg.get("message-id", "") or "")
    body = ""
    if msg.is_multipart():
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            body = str(part.get_content())
    elif msg.keys():
        try:
            body = str(msg.get_content())
        except (KeyError, LookupError):  # pragma: no cover - unusual charsets
            body = str(msg.get_payload() or "")
    else:
        body = raw
    m = _FWD_MARK.search(body)
    if m:
        inner = body[m.end() :]
        headers: dict[str, str] = {}
        lines = inner.lstrip("\n").splitlines()
        i = 0
        for i, line in enumerate(lines):  # noqa: B007 - i is used after the loop
            hm = re.match(r"^\s*(from|subject|date|to|sent|cc)\s*:\s*(.*)$", line, re.I)
            if not hm:
                if line.strip() == "" and headers:
                    break
                continue
            headers[hm.group(1).lower()] = hm.group(2).strip()
        if headers.get("from"):
            sender = headers["from"]
            subject = headers.get("subject", subject)
            date = _parse_date(headers.get("date") or headers.get("sent")) or date
            body = "\n".join(lines[i:])
    body = re.sub(r"<[^>]+>", " ", body)  # html part: tags are irrelevant to classification
    return sender, subject, body, date, mid


def message_ref(user_id: str, sender: str, subject: str, date: datetime | None, mid: str) -> str:
    """Stable, non-reversible reference used only for de-duplication."""
    basis = mid or f"{_domain(sender)}|{subject}|{date.isoformat() if date else ''}"
    return hashlib.sha256(f"{user_id}|{basis}".encode()).hexdigest()[:24]


async def _providers(session: AsyncSession) -> list[Provider]:
    return list((await session.execute(select(Provider))).scalars().all())


async def _already_recorded(session: AsyncSession, user_id: str, ref: str) -> bool:
    rows = (
        await session.execute(
            select(Notification.facts).where(
                Notification.user_id == user_id, Notification.kind.like("mail.%")
            )
        )
    ).scalars()
    return any((f or {}).get("message_ref") == ref for f in rows)


async def _record(
    session: AsyncSession, settings: Settings, user_id: str, facts: MailFacts, source: str, ref: str
) -> Notification:
    titles = {
        "key_security": "Security notice about a provider credential",
        "quota_exhausted": "Provider quota or credits exhausted",
        "credit_expiry": "Provider credits or trial expiring",
        "quota_warning": "Provider usage approaching a limit",
        "model_deprecation": "Provider model deprecation notice",
        "terms_change": "Provider terms, policy, or pricing change",
        "billing": "Provider billing notice",
    }
    n = Notification(
        project_id=None,
        user_id=user_id,
        kind=f"mail.{facts.category}",
        severity=facts.severity,
        title=f"{titles[facts.category]} ({facts.provider_id})",
        body="Detected from a provider message. Review the original in your mailbox; it was not stored.",
        channel="mail",
        facts=redact(facts.as_facts(source, ref)),
        delete_after=utcnow() + timedelta(days=settings.mail_facts_retention_days),
    )
    session.add(n)
    await session.flush()
    return n


async def ingest_forwarded(ctx: AppContext, session: AsyncSession, user: User, raw: str) -> dict[str, Any]:
    """Classify a user-forwarded message. The raw text lives only for the duration of this call."""
    if not ctx.settings.mail_forwarding_enabled:
        raise Forbidden("forwarded-mail ingestion is disabled on this instance")
    if len(raw.encode("utf-8", "ignore")) > MAX_FORWARDED_BYTES:
        raise ValidationFailed("message too large", details={"max_bytes": MAX_FORWARDED_BYTES})
    sender, subject, body, date, mid = parse_forwarded(raw)
    facts = classify(sender, subject, body, await _providers(session), date=date)
    del raw, body  # explicit: nothing below may reference the message content
    if facts is None:
        return {"stored": False, "reason": "not_a_recognised_provider_notice"}
    ref = message_ref(user.id, sender, subject, date, mid)
    if await _already_recorded(session, user.id, ref):
        return {"stored": False, "reason": "duplicate", "category": facts.category}
    n = await _record(session, ctx.settings, user.id, facts, "forwarded", ref)
    await audit.record(
        session,
        "mail.forwarded_classified",
        actor_user_id=user.id,
        target_type="notification",
        target_id=n.id,
        details={"category": facts.category, "provider": facts.provider_id},
    )
    return {"stored": True, "notification_id": n.id, "facts": n.facts}


async def purge_mail_data(session: AsyncSession, user_id: str) -> int:
    res = await session.execute(
        delete(Notification).where(Notification.user_id == user_id, Notification.kind.like("mail.%"))
    )
    await audit.record(
        session, "mail.data_purged", actor_user_id=user_id, target_type="user", target_id=user_id
    )
    return int(getattr(res, "rowcount", 0) or 0)


# ------------------------------------------------------------------------------------------- OAuth state
def _sign_state(settings: Settings, grant_id: str, user_id: str, now: float | None = None) -> str:
    payload = json.dumps({"g": grant_id, "u": user_id, "e": int((now or time.time()) + STATE_TTL_S)}).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    mac = hmac.new(settings.pepper_bytes(), b"mail-state:" + body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def _verify_state(settings: Settings, state: str, now: float | None = None) -> tuple[str, str]:
    body, _, mac = state.partition(".")
    want = hmac.new(settings.pepper_bytes(), b"mail-state:" + body.encode(), hashlib.sha256).hexdigest()
    if not body or not hmac.compare_digest(mac, want):
        raise Forbidden("invalid OAuth state")
    data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if int(data["e"]) < (now or time.time()):
        raise Forbidden("OAuth state expired; start again")
    return str(data["g"]), str(data["u"])


def _aad(grant_id: str) -> str:
    return f"mail_grant:{grant_id}"


# ------------------------------------------------------------------------------------------- Gmail
async def start_consent(
    ctx: AppContext, session: AsyncSession, user: User, consent_version: str
) -> dict[str, Any]:
    _require_gmail(ctx.settings)
    if consent_version != CONSENT_VERSION:
        raise ValidationFailed(
            "consent text changed; review and accept the current version",
            details={"consent_version": CONSENT_VERSION},
        )
    active = (
        await session.execute(
            select(MailGrant).where(MailGrant.user_id == user.id, MailGrant.status == "active")
        )
    ).scalar_one_or_none()
    if active is not None:
        raise Conflict("a Gmail grant is already active; revoke it first")
    g = MailGrant(user_id=user.id, scopes=list(SCOPES), status="pending", consent_version=CONSENT_VERSION)
    session.add(g)
    await session.flush()
    s = ctx.settings
    params = {
        "client_id": s.gmail_client_id,
        "redirect_uri": s.gmail_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": _sign_state(s, g.id, user.id),
    }
    await audit.record(
        session,
        "mail.consent_started",
        actor_user_id=user.id,
        target_type="mail_grant",
        target_id=g.id,
        details={"scopes": list(SCOPES), "consent_version": CONSENT_VERSION},
    )
    return {"grant_id": g.id, "authorization_url": f"{AUTH_URL}?{urlencode(params)}", "scopes": list(SCOPES)}


async def complete_oauth(ctx: AppContext, code: str, state: str) -> MailGrant:
    """OAuth redirect target. Authenticated by the signed, expiring state rather than an Oasis Key."""
    _require_gmail(ctx.settings)
    grant_id, user_id = _verify_state(ctx.settings, state)
    async with ctx.db.session() as s0:
        g0 = await s0.get(MailGrant, grant_id)
        if g0 is None or g0.user_id != user_id or g0.status != "pending":
            raise NotFound("pending grant not found")
    s = ctx.settings
    assert s.gmail_client_secret is not None
    resp = await ctx.google_client.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": s.gmail_client_id,
            "client_secret": s.gmail_client_secret.get_secret_value(),
            "redirect_uri": s.gmail_redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    if resp.status_code != 200:
        raise Forbidden("Google rejected the authorization code")
    tok = resp.json()
    granted = set(str(tok.get("scope", "")).split())
    refresh = tok.get("refresh_token")
    if granted != set(SCOPES) or not refresh:
        # Over-broad (or missing) grants are refused outright and revoked: minimum necessary access only.
        await _revoke_token(ctx, str(refresh or tok.get("access_token") or ""))
        async with ctx.db.transaction() as s1:
            g1 = await s1.get(MailGrant, grant_id)
            if g1 is not None:
                g1.status = "rejected"
            await audit.record(
                s1,
                "mail.grant_rejected",
                actor_user_id=user_id,
                target_type="mail_grant",
                target_id=grant_id,
                details={"granted_scopes": sorted(granted)},
            )
        raise Forbidden(
            "granted scopes do not match the requested minimal scope", details={"expected": list(SCOPES)}
        )
    sealed = ctx.vault_.seal({"refresh_token": refresh}, _aad(grant_id))
    async with ctx.db.transaction() as s2:
        g = await s2.get(MailGrant, grant_id)
        assert g is not None
        cred = Credential(
            kek_id=sealed.kek_id, wrapped_dek=sealed.wrapped_dek, ciphertext=sealed.ciphertext, aad=sealed.aad
        )
        s2.add(cred)
        await s2.flush()
        g.credential_id, g.status = cred.id, "active"
        await audit.record(
            s2,
            "mail.grant_activated",
            actor_user_id=user_id,
            target_type="mail_grant",
            target_id=grant_id,
            details={"scopes": sorted(granted)},
        )
        return g


async def _revoke_token(ctx: AppContext, token: str) -> None:
    if not token:
        return
    try:
        await ctx.google_client.post(REVOKE_URL, data={"token": token})
    except Exception:  # pragma: no cover - local revocation proceeds regardless
        log.warning("google token revocation failed; local revocation continues")


async def _refresh_secret(ctx: AppContext, g: MailGrant) -> str | None:
    if g.credential_id is None:
        return None
    async with ctx.db.session() as s:
        cred = await s.get(Credential, g.credential_id)
    if cred is None:
        return None
    from oasis_route.security.vault import SealedSecret

    data = ctx.vault_.open(
        SealedSecret(
            kek_id=cred.kek_id, wrapped_dek=cred.wrapped_dek, ciphertext=cred.ciphertext, aad=cred.aad
        ),
        _aad(g.id),
    )
    return str(data.get("refresh_token") or "") or None


async def revoke_grant(ctx: AppContext, user: User, grant_id: str, *, purge: bool = False) -> dict[str, Any]:
    async with ctx.db.session() as s:
        g = await s.get(MailGrant, grant_id)
        if g is None or g.user_id != user.id:
            raise NotFound("grant not found")
        snapshot = MailGrant(id=g.id, user_id=g.user_id, credential_id=g.credential_id, consent_version="")
    refresh = await _refresh_secret(ctx, snapshot)
    if refresh and gmail_gate(ctx.settings)[0]:
        await _revoke_token(ctx, refresh)
    async with ctx.db.transaction() as s:
        g = await s.get(MailGrant, grant_id)
        assert g is not None
        cred_id = g.credential_id
        g.status, g.revoked_at, g.credential_id = "revoked", utcnow(), None
        await s.flush()
        if cred_id:
            cred = await s.get(Credential, cred_id)
            if cred is not None:
                await s.delete(cred)  # crypto-shred: the refresh token is gone
        purged = await purge_mail_data(s, user.id) if purge else 0
        await audit.record(
            s,
            "mail.grant_revoked",
            actor_user_id=user.id,
            target_type="mail_grant",
            target_id=grant_id,
            details={"purged": purged},
        )
    return {"status": "revoked", "purged_notifications": purged}


def _header(msg: dict[str, Any], name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if str(h.get("name", "")).lower() == name:
            return str(h.get("value", ""))
    return ""


async def sync_grant(ctx: AppContext, grant_id: str) -> dict[str, Any]:
    """Fetch recent inbox message *headers* and record provider notices. Nothing else is read or stored."""
    _require_gmail(ctx.settings)
    async with ctx.db.session() as s:
        g = await s.get(MailGrant, grant_id)
        if g is None or g.status != "active":
            raise NotFound("active grant not found")
        user_id, since = g.user_id, g.last_sync_at
        snapshot = MailGrant(id=g.id, user_id=g.user_id, credential_id=g.credential_id, consent_version="")
        providers = await _providers(s)
    refresh = await _refresh_secret(ctx, snapshot)
    st = ctx.settings
    assert st.gmail_client_secret is not None
    tr = await ctx.google_client.post(
        TOKEN_URL,
        data={
            "client_id": st.gmail_client_id,
            "client_secret": st.gmail_client_secret.get_secret_value(),
            "refresh_token": refresh or "",
            "grant_type": "refresh_token",
        },
    )
    if tr.status_code != 200:
        async with ctx.db.transaction() as s:
            g = await s.get(MailGrant, grant_id)
            assert g is not None
            g.status = "error"
            from oasis_route.notifications.service import notify

            await notify(
                s,
                kind="mail.grant_error",
                severity="warning",
                user_id=user_id,
                title="Gmail access needs to be reconnected",
                body="Google rejected the stored authorization. Revoke and reconnect if you still "
                "want provider notices from Gmail.",
                dedupe_key=f"mail_grant:{grant_id}",
            )
        return {"status": "error", "recorded": 0}
    access = str(tr.json().get("access_token", ""))
    hdrs = {"authorization": f"Bearer {access}"}
    lr = await ctx.google_client.get(
        f"{GMAIL_API}/messages", params={"labelIds": "INBOX", "maxResults": SYNC_PAGE}, headers=hdrs
    )
    lr.raise_for_status()
    ids = [str(m["id"]) for m in (lr.json().get("messages") or [])][:SYNC_PAGE]
    recorded = scanned = 0
    async with ctx.db.transaction() as s:
        for mid in ids:
            mr = await ctx.google_client.get(
                f"{GMAIL_API}/messages/{mid}",
                params=[
                    ("format", "metadata"),
                    ("metadataHeaders", "From"),
                    ("metadataHeaders", "Subject"),
                    ("metadataHeaders", "Date"),
                ],
                headers=hdrs,
            )
            if mr.status_code != 200:
                continue
            msg = mr.json()
            internal = int(msg.get("internalDate") or 0) / 1000
            when = datetime.fromtimestamp(internal, UTC) if internal else None
            if since is not None and when is not None and when <= since:
                continue
            scanned += 1
            sender, subject = _header(msg, "from"), _header(msg, "subject")
            # Headers only: the snippet and any body fields are deliberately ignored.
            facts = classify(sender, subject, "", providers, date=when)
            if facts is None:
                continue
            ref = message_ref(user_id, sender, subject, when, f"gmail:{mid}")
            if await _already_recorded(s, user_id, ref):
                continue
            await _record(s, st, user_id, facts, "gmail", ref)
            recorded += 1
        g = await s.get(MailGrant, grant_id)
        assert g is not None
        g.last_sync_at = utcnow()
        await audit.record(
            s,
            "mail.synced",
            actor_user_id=user_id,
            target_type="mail_grant",
            target_id=grant_id,
            details={"scanned": scanned, "recorded": recorded},
        )
    return {"status": "ok", "scanned": scanned, "recorded": recorded}


async def sync_all(ctx: AppContext) -> dict[str, Any]:
    if not gmail_gate(ctx.settings)[0]:
        return {"skipped": "gmail_disabled"}
    async with ctx.db.session() as s:
        ids = list((await s.execute(select(MailGrant.id).where(MailGrant.status == "active"))).scalars())
    total = 0
    for gid in ids:
        try:
            total += int((await sync_grant(ctx, gid)).get("recorded", 0))
        except Exception:  # pragma: no cover - one bad grant must not block others
            log.exception("gmail sync failed", extra={"grant_id": gid})
    return {"grants": len(ids), "recorded": total}
