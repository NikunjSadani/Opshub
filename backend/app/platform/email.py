"""Email-sending primitive — a pluggable sender behind a tiny Protocol.

Ships **FAIL-CLOSED**: `get_email_sender()` returns a no-op `NoopEmailSender`
until the MSG91 SMTP relay password is configured, so deploying this module
changes nothing until the owner adds the secret. Selection is env-driven with no
branching in callers — the exact style of `get_storage()` in `storage.py`.

Transport is the **MSG91 SMTP RELAY** (Domain Settings -> SMTP Integration) over
STARTTLS, reusing loyalty's ALREADY-VERIFIED sending domain `notify.gifsy.in`
(SPF/DKIM/DMARC). This is a faithful port of loyalty's WORKING email sender
(`api/src/notifications/msg91.service.ts` `sendEmail`, which uses nodemailer/SMTP:
host `smtp.mailer91.com`, port 587 STARTTLS, user `emailer@notify.gifsy.in`, the
`MSG91_SMTP_PASS` secret as the credential).

NOTE — why SMTP and not the MSG91 v5 email HTTP API: that HTTP API is TEMPLATE-only
(a panel-registered `template_id` + short variables) and cannot carry an arbitrary
HTML body; loyalty deliberately chose SMTP for exactly this reason (see its
`sendEmail` comment). Our `send()` takes a raw `html_body`, which only SMTP can
deliver — so this sender mirrors loyalty's verified SMTP contract, not the HTTP API.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol, runtime_checkable

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Bounded SMTP timeout (connect + I/O) so a wedged relay fails fast instead of
# hanging the request. Loyalty uses 10s connect / 20s socket; 20s is the ceiling.
_SMTP_TIMEOUT_SECONDS = 20.0


@runtime_checkable
class EmailSender(Protocol):
    """Send one transactional email. Implementations are best-effort at the call
    site (the invite flow swallows failures), but a real send that fails DOES raise
    so the caller can log it; the no-op never raises."""

    def send(
        self, to: str, subject: str, html_body: str, text_body: str | None = None
    ) -> None: ...


class NoopEmailSender:
    """The FAIL-CLOSED default when unconfigured: log and return, never send/raise.

    This is what runs until the owner sets `MSG91_SMTP_PASS`, so shipping the
    feature is inert until deliberately activated.
    """

    def send(
        self, to: str, subject: str, html_body: str, text_body: str | None = None
    ) -> None:
        # INFO, not WARNING: pre-activation this is expected (not a fault), and it keeps a
        # recipient address out of the higher-severity log stream.
        logger.info(
            "email not configured (MSG91_SMTP_PASS unset) — would send to %s: %s",
            to,
            subject,
        )


class Msg91EmailSender:
    """Send transactional email via the MSG91 SMTP relay.

    Config comes from `Settings` (all env-overridable): `msg91_smtp_pass` (the
    credential — its presence is what selects this sender), `email_from` (must be on
    the verified `notify.gifsy.in` domain), and host/port/user defaulting to MSG91's
    relay. Secrets are `.strip()`-ed to defend against a trailing CR/newline/BOM that
    Cloud Run secret mounts can introduce (a stray `\\r` in the password fails auth).

    On a successful send: one info log (never the password). On any SMTP failure:
    raise a clear `RuntimeError` (the invite flow catches it — email is best-effort).
    """

    def __init__(self, settings: Settings) -> None:
        self._password = (settings.msg91_smtp_pass or "").strip()
        self._host = (settings.msg91_smtp_host or "").strip() or "smtp.mailer91.com"
        self._port = settings.msg91_smtp_port
        self._user = (settings.msg91_smtp_user or "").strip() or "emailer@notify.gifsy.in"
        self._from = (settings.email_from or "").strip() or "opshub@notify.gifsy.in"

    def _build_message(
        self, to: str, subject: str, html_body: str, text_body: str | None
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self._from
        msg["To"] = to
        msg["Subject"] = subject
        # Prefer a multipart/alternative (text + html) for deliverability; if the
        # caller gave no plain-text part, send the HTML as the sole content.
        if text_body is not None:
            msg.set_content(text_body)
            msg.add_alternative(html_body, subtype="html")
        else:
            msg.set_content(html_body, subtype="html")
        return msg

    def send(
        self, to: str, subject: str, html_body: str, text_body: str | None = None
    ) -> None:
        msg = self._build_message(to, subject, html_body, text_body)
        context = ssl.create_default_context()
        try:
            if self._port == 465:
                # Implicit TLS.
                with smtplib.SMTP_SSL(
                    self._host, self._port, timeout=_SMTP_TIMEOUT_SECONDS, context=context
                ) as smtp:
                    smtp.login(self._user, self._password)
                    smtp.send_message(msg)
            else:
                # STARTTLS (require it — never send the password in cleartext).
                with smtplib.SMTP(
                    self._host, self._port, timeout=_SMTP_TIMEOUT_SECONDS
                ) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                    smtp.login(self._user, self._password)
                    smtp.send_message(msg)
        except Exception as exc:  # noqa: BLE001 - re-raised with context; password never logged
            logger.error("MSG91 SMTP email %r to %s failed: %s", subject, to, exc)
            raise RuntimeError(f"failed to send email {subject!r} to {to}: {exc}") from exc
        logger.info("email %r sent to %s via MSG91 SMTP relay", subject, to)


def get_email_sender(settings: Settings | None = None) -> EmailSender:
    """Return the active email sender: `Msg91EmailSender` when the SMTP relay
    password is set (non-empty after stripping), else the FAIL-CLOSED `NoopEmailSender`.

    Env-driven, mirroring `get_storage()` — callers never branch on configuration.
    """
    settings = settings or get_settings()
    if (settings.msg91_smtp_pass or "").strip():
        return Msg91EmailSender(settings)
    return NoopEmailSender()
