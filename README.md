# Gifsy OpsHub

Internal operations platform (Delivery Challan generation + Expense/Invoice management), built as a modular monolith.

- **Backend:** Python / FastAPI (`backend/`)
- **Frontend:** React SPA served same-origin (`frontend/`)
- **DB:** PostgreSQL (Cloud SQL); sqlite for the local bootstrap
- **Auth:** Firebase Authentication (email/password)

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
