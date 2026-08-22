"""Public (no-login) challan-QR invoice viewer.

In-memory sqlite (StaticPool, FK-pragma ON), FILES_DIR -> tmp, and the feature flag
flipped per-test via a fake settings object patched onto the routes module. Seeds a
client (+ PIN), an ACTIVE project, a Challan bearing an access_token, and — where a
case needs it — a CONFIRMED SalesInvoice whose source blob lives in LocalStorage.

Security properties under test: feature-off is a hard 404 wall; unknown token and
wrong password are indistinguishable (no enumeration oracle); a per-token throttle
turns to 429 after N failures; only a correct password reveals the (streamed) PDF or
the "not available yet" page; the compare is constant-time.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.db import Base, get_db
from app.modules.billing import models as _billing_models  # noqa: F401  (register tables)
from app.modules.challan.models import Challan, ChallanBatch
from app.modules.files.models import StoredFile
from app.modules.numbering.models import NumberingAllocation
from app.modules.projects import service as projects_service
from app.modules.public_docs import routes, service
from app.modules.sales_orders import models as _so_models  # noqa: F401  (register tables)
from app.platform.storage import get_storage

PIN = "4821"
CHALLAN_NUMBER = "GIF/DC/26-27/L/000189"
INVOICE_NUMBER = "CINV-777"
PDF_BYTES = b"%PDF-1.4 fake invoice bytes \n%%EOF"


class _FakeSettings:
    """Minimal stand-in exposing just the flag the routes read."""

    def __init__(self, enabled: bool) -> None:
        self.qr_invoice_access_enabled = enabled


def _seed_challan(
    db: Session,
    *,
    token: str,
    number: str = CHALLAN_NUMBER,
    project_code: str,
    invoice_number: str = INVOICE_NUMBER,
) -> Challan:
    batch = ChallanBatch()
    db.add(batch)
    db.flush()
    alloc = NumberingAllocation(
        series="L", fy="26-27", number=189, formatted=number, status="ISSUED"
    )
    db.add(alloc)
    db.flush()
    challan = Challan(
        batch_id=batch.id,
        allocation_id=alloc.id,
        number=number,
        series="L",
        fy="26-27",
        number_int=189,
        challan_date=date(2026, 6, 15),
        project_code=project_code,
        invoice_number=invoice_number,
        access_token=token,
        consignor_name="Gifsy",
        consignor_gstin="27AAACG1234A1Z5",
        consignor_state="Maharashtra",
        consignee_name="Client Co",
        consignee_gstin="29AABCC1111C1Z0",
        consignee_state="Karnataka",
        ship_to_name="Client Co",
        ship_to_address="2 Client Ave",
        ship_to_state="Karnataka",
    )
    db.add(challan)
    db.flush()
    return challan


def _seed_confirmed_invoice(db: Session, *, client_id: int, invoice_number: str) -> int:
    """Store a PDF blob + a CONFIRMED SalesInvoice pointing at it. Returns invoice id."""
    from app.modules.billing.models import SalesInvoice

    storage_ref = get_storage().save("inv-777.pdf", PDF_BYTES)
    blob = StoredFile(
        kind="upload",
        filename="invoice.pdf",
        content_type="application/pdf",
        size=len(PDF_BYTES),
        storage_ref=storage_ref,
        uploaded_by="ops",
        module_key="billing",
    )
    db.add(blob)
    db.flush()
    inv = SalesInvoice(
        client_id=client_id,
        source_file_id=blob.id,
        invoice_number=invoice_number,
        status="CONFIRMED",
    )
    db.add(inv)
    db.flush()
    return inv.id


@pytest.fixture
def ctx(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("FILES_DIR", f"{tmp_path}/_files")
    service.reset_rate_limits()

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_conn: Any, _record: Any) -> None:  # noqa: ANN401
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    # Feature flag: mutable list so a test can flip it; routes read via patched get_settings.
    enabled = [True]
    monkeypatch.setattr(routes, "get_settings", lambda: _FakeSettings(enabled[0]))

    seed = TestSession()
    seed_client = projects_service.create_client(
        seed, name="Client Co", code="CLI", actor_uid="adm")
    seed.flush()
    seed_client.access_pin = PIN
    project = projects_service.create_project(
        seed, client_id=seed_client.id, name="Spine Project", actor_uid="adm")
    seed.flush()
    challan = _seed_challan(seed, token="tok-good", project_code=project.code)
    seed.commit()
    client_id = seed_client.id
    project_code = project.code
    challan_number = challan.number
    seed.close()

    def _db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = _db

    yield {
        "client": TestClient(app),
        "TestSession": TestSession,
        "client_id": client_id,
        "project_code": project_code,
        "challan_number": challan_number,
        "enabled": enabled,
        "good_password": PIN + challan_number,
    }
    engine.dispose()


# --------------------------------------------------------------------- tests

def test_feature_off_get_and_post_both_404(ctx: dict[str, Any]) -> None:
    ctx["enabled"][0] = False
    client: TestClient = ctx["client"]
    assert client.get("/d/tok-good").status_code == 404
    assert client.post("/d/tok-good", data={"password": ctx["good_password"]}).status_code == 404


def test_unknown_token_get_and_post_404(ctx: dict[str, Any]) -> None:
    client: TestClient = ctx["client"]
    assert client.get("/d/nope").status_code == 404
    assert client.post("/d/nope", data={"password": "whatever"}).status_code == 404


def test_get_valid_token_shows_form_with_challan_number(ctx: dict[str, Any]) -> None:
    client: TestClient = ctx["client"]
    r = client.get("/d/tok-good")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # Shows the challan number as context; leaks nothing sensitive (no PIN, no invoice no.).
    assert ctx["challan_number"] in r.text
    assert PIN not in r.text
    assert INVOICE_NUMBER not in r.text
    assert "<form" in r.text and 'name="password"' in r.text


def test_wrong_password_rejected_generic_404(ctx: dict[str, Any]) -> None:
    client: TestClient = ctx["client"]
    r = client.post("/d/tok-good", data={"password": "wrong"})
    assert r.status_code == 404
    # No hint about why (constant generic page).
    assert PIN not in r.text and INVOICE_NUMBER not in r.text


def test_rate_limit_after_n_attempts_429(ctx: dict[str, Any]) -> None:
    client: TestClient = ctx["client"]
    for _ in range(service.MAX_FAILED_ATTEMPTS):
        assert client.post("/d/tok-good", data={"password": "wrong"}).status_code == 404
    # The next attempt is locked out — even a correct password now gets 429.
    r = client.post("/d/tok-good", data={"password": ctx["good_password"]})
    assert r.status_code == 429
    assert r.headers.get("retry-after") == str(service.COOLDOWN_SECONDS)


def test_correct_password_no_invoice_returns_not_available(ctx: dict[str, Any]) -> None:
    # No SalesInvoice seeded -> authenticated but nothing to serve.
    client: TestClient = ctx["client"]
    r = client.post("/d/tok-good", data={"password": ctx["good_password"]})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "application/pdf" not in r.headers["content-type"]
    assert "not available yet" in r.text.lower()


def test_correct_password_streams_pdf(ctx: dict[str, Any]) -> None:
    db = ctx["TestSession"]()
    _seed_confirmed_invoice(db, client_id=ctx["client_id"], invoice_number=INVOICE_NUMBER)
    db.commit()
    db.close()

    client: TestClient = ctx["client"]
    r = client.post("/d/tok-good", data={"password": ctx["good_password"]})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert "inline" in r.headers["content-disposition"]
    assert r.content == PDF_BYTES


def test_access_pin_not_configured_is_generic_404(ctx: dict[str, Any]) -> None:
    # Clear the PIN -> access not configured; must look exactly like a wrong password.
    db = ctx["TestSession"]()
    from app.modules.projects.models import ProjectClient
    row = db.get(ProjectClient, ctx["client_id"])
    assert row is not None
    row.access_pin = None
    db.commit()
    db.close()

    client: TestClient = ctx["client"]
    # Even the "would-be-correct" concat (empty PIN + number) must NOT authenticate.
    r = client.post("/d/tok-good", data={"password": ctx["challan_number"]})
    assert r.status_code == 404


def test_enumeration_wrong_password_vs_invalid_token_indistinguishable(
    ctx: dict[str, Any],
) -> None:
    client: TestClient = ctx["client"]
    valid_wrong = client.post("/d/tok-good", data={"password": "definitely-wrong"})
    invalid_token = client.post("/d/does-not-exist", data={"password": "definitely-wrong"})
    # Same status AND byte-identical body — no oracle distinguishing the two.
    assert valid_wrong.status_code == invalid_token.status_code == 404
    assert valid_wrong.content == invalid_token.content
    assert valid_wrong.headers["content-type"] == invalid_token.headers["content-type"]


def test_constant_time_compare_is_used(
    ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hmac as _hmac

    calls = {"n": 0}
    real = _hmac.compare_digest

    def _spy(a: Any, b: Any) -> bool:  # noqa: ANN401
        calls["n"] += 1
        return real(a, b)

    monkeypatch.setattr(routes.hmac, "compare_digest", _spy)
    client: TestClient = ctx["client"]
    client.post("/d/tok-good", data={"password": ctx["good_password"]})
    assert calls["n"] >= 1


def test_resolve_confirmed_invoice_helper(ctx: dict[str, Any]) -> None:
    db = ctx["TestSession"]()
    inv_id = _seed_confirmed_invoice(
        db, client_id=ctx["client_id"], invoice_number=INVOICE_NUMBER)
    db.commit()

    challan = service.resolve_challan_by_token(db, "tok-good")
    assert challan is not None
    resolved = service.resolve_confirmed_invoice(db, challan)
    assert resolved is not None and resolved.id == inv_id

    # A non-CONFIRMED invoice with the same number is NOT served.
    from app.modules.billing.models import SalesInvoice
    other = db.get(SalesInvoice, inv_id)
    assert other is not None
    other.status = "NEEDS_REVIEW"
    db.commit()
    assert service.resolve_confirmed_invoice(db, challan) is None
    db.close()
