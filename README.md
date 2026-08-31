# Gifsy OpsHub

**Live at https://opshub.gifsy.in (production)** — Cloud Run `opshub-api`, GCP `opshub-506704`.

Internal operations platform, built as a modular monolith. Modules: Delivery Challan generation,
Expense/Invoice management, Projects, Purchase Orders, Billing/AR (client invoices + credit notes),
Finance/P&L, Logistics, Action Center, and Audit & Access reporting.

- **Backend:** Python / FastAPI (`backend/`)
- **Frontend:** React SPA served same-origin (`frontend/`)
- **DB:** PostgreSQL (Cloud SQL); sqlite for the local bootstrap
- **Auth:** Firebase Authentication (email/password) — **live** (real logins in prod)

## Docs
- `docs/DESIGN.md` / `docs/design-doc.html` — finalised architecture (v4)
- `docs/BUILD-PLAN.md` — build governance + phases
- `docs/RESUME.md` — current build state (read to continue)

## Run the backend gate
```
cd backend
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe -m ruff check app tests && \
./.venv/Scripts/python.exe -m mypy app && \
./.venv/Scripts/python.exe -m pytest
```
