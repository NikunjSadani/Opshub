"""Email sender: FAIL-CLOSED selection + the MSG91 SMTP relay contract.

Selection mirrors storage's `get_storage()` — env/config-driven, no branching in
callers: with `msg91_smtp_pass` unset the sender is a no-op; set, it is the real
MSG91 SMTP sender. The real send is exercised against a MOCKED `smtplib.SMTP` so we
assert the verified contract (host/port, STARTTLS, login credentials, message
headers, HTML body) without a network call, and that any SMTP failure raises.

Contract note: this ports loyalty's WORKING email path, which is the MSG91 SMTP
relay (nodemailer), NOT the v5 email HTTP API — that API is template-only and can't
carry a raw HTML body. So the asserted contract is SMTP (host/STARTTLS/login), not
an HTTP endpoint/auth-header/2xx.
"""
from __future__ import annotations

from email.message import EmailMessage
from typing import Any

import pytest

from app.config import Settings
from app.platform import email as em


def _settings(**overrides: Any) -> Settings:
    # `_env_file=None` keeps the test deterministic regardless of a local .env.
    return Settings(_env_file=None, **overrides)


# --------------------------------------------------------------- selection (fail-closed)

def test_get_email_sender_defaults_to_noop_when_unset() -> None:
    sender = em.get_email_sender(_settings(msg91_smtp_pass=None))
    assert isinstance(sender, em.NoopEmailSender)


def test_get_email_sender_noop_when_blank() -> None:
    # A whitespace-only secret (e.g. a stray newline mount) is treated as unset.
    sender = em.get_email_sender(_settings(msg91_smtp_pass="   "))
    assert isinstance(sender, em.NoopEmailSender)


def test_get_email_sender_selects_msg91_when_set() -> None:
    sender = em.get_email_sender(_settings(msg91_smtp_pass="a-real-secret"))
    assert isinstance(sender, em.Msg91EmailSender)


def test_noop_sender_never_raises() -> None:
    # The fail-closed default must be a safe no-op.
    em.NoopEmailSender().send("u@x.com", "Subj", "<p>hi</p>", "hi")


# ----------------------------------------------------------- MSG91 SMTP send contract

class _FakeSMTP:
    """Captures the MSG91 SMTP relay interaction (STARTTLS path), including the ORDER
    of calls so a test can prove STARTTLS happens before login (never cleartext auth)."""

    captured: dict[str, Any] = {}

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        _FakeSMTP.captured = {"host": host, "port": port, "timeout": timeout, "calls": []}

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def ehlo(self) -> None:
        _FakeSMTP.captured["calls"].append("ehlo")

    def starttls(self, context: object = None) -> None:
        _FakeSMTP.captured["starttls"] = True
        _FakeSMTP.captured["calls"].append("starttls")

    def login(self, user: str, password: str) -> None:
        _FakeSMTP.captured["login"] = (user, password)
        _FakeSMTP.captured["calls"].append("login")

    def send_message(self, msg: EmailMessage) -> None:
        _FakeSMTP.captured["msg"] = msg
        _FakeSMTP.captured["calls"].append("send")


def test_msg91_sender_uses_verified_relay_and_starttls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(em.smtplib, "SMTP", _FakeSMTP)
    sender = em.Msg91EmailSender(
        _settings(msg91_smtp_pass="pw", email_from="opshub@notify.gifsy.in")
    )
    sender.send("staff@example.com", "Welcome", "<p>hello</p>", "hello")

    cap = _FakeSMTP.captured
    assert cap["host"] == "smtp.mailer91.com"        # loyalty's verified relay
    assert cap["port"] == 587
    assert cap["starttls"] is True                    # STARTTLS required
    assert cap["login"] == ("emailer@notify.gifsy.in", "pw")

    msg = cap["msg"]
    assert msg["To"] == "staff@example.com"
    assert msg["From"] == "opshub@notify.gifsy.in"    # on the verified domain
    assert msg["Subject"] == "Welcome"
    # multipart/alternative: an HTML part carrying the body is present.
    html_part = next(p for p in msg.walk() if p.get_content_type() == "text/html")
    assert "hello" in html_part.get_content()


def test_msg91_sender_strips_secret_and_from(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cloud Run secret mounts can append a trailing CR/newline — it must be stripped
    # (a stray \r in the password/header would fail SMTP auth).
    monkeypatch.setattr(em.smtplib, "SMTP", _FakeSMTP)
    sender = em.Msg91EmailSender(
        _settings(msg91_smtp_pass="pw\r\n", email_from="opshub@notify.gifsy.in\n")
    )
    sender.send("staff@example.com", "S", "<p>x</p>", "x")
    assert _FakeSMTP.captured["login"] == ("emailer@notify.gifsy.in", "pw")
    assert _FakeSMTP.captured["msg"]["From"] == "opshub@notify.gifsy.in"


def test_starttls_precedes_login_never_cleartext(monkeypatch: pytest.MonkeyPatch) -> None:
    # Security regression guard: the password must NEVER be sent before STARTTLS
    # upgrades the channel. If a refactor reordered login() before starttls() (or
    # dropped starttls), this fails — the other contract tests would not catch it.
    monkeypatch.setattr(em.smtplib, "SMTP", _FakeSMTP)
    em.Msg91EmailSender(_settings(msg91_smtp_pass="pw")).send("s@x.com", "S", "<p>x</p>", "x")
    calls = _FakeSMTP.captured["calls"]
    assert "starttls" in calls and "login" in calls
    assert calls.index("starttls") < calls.index("login")  # TLS up BEFORE credentials
    assert calls.index("login") < calls.index("send")


def test_port_465_uses_implicit_tls_no_starttls(monkeypatch: pytest.MonkeyPatch) -> None:
    # The 465 branch uses SMTP_SSL (implicit TLS) and must NOT call starttls.
    class _FakeSMTPSSL(_FakeSMTP):
        def __init__(
            self, host: str, port: int, timeout: float | None = None, context: object = None
        ) -> None:
            super().__init__(host, port, timeout)

    monkeypatch.setattr(em.smtplib, "SMTP_SSL", _FakeSMTPSSL)
    sender = em.Msg91EmailSender(_settings(msg91_smtp_pass="pw", msg91_smtp_port=465))
    sender.send("s@x.com", "S", "<p>x</p>", "x")
    cap = _FakeSMTP.captured
    assert cap["port"] == 465
    assert "starttls" not in cap["calls"]          # implicit TLS — no upgrade step
    assert cap["login"] == ("emailer@notify.gifsy.in", "pw")
    assert cap["calls"].index("login") < cap["calls"].index("send")


def test_msg91_sender_raises_on_smtp_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomSMTP(_FakeSMTP):
        def login(self, user: str, password: str) -> None:
            raise em.smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(em.smtplib, "SMTP", _BoomSMTP)
    sender = em.Msg91EmailSender(_settings(msg91_smtp_pass="hunter2secret"))
    with pytest.raises(RuntimeError) as excinfo:
        sender.send("staff@example.com", "S", "<p>x</p>", "x")
    # The SMTP password must never appear in the raised error (it can reach a log).
    assert "hunter2secret" not in str(excinfo.value)
