# Root context: builds the React SPA and serves it same-origin from the FastAPI
# image (one image = the whole product, used by both the Cloud Run service and
# the Alembic migrate job).

# ---- stage 1: build the SPA ----
FROM node:24-slim AS frontend
WORKDIR /fe
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # -> /fe/dist

# ---- stage 2: backend + static ----
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY --from=frontend /fe/dist ./static

ENV STATIC_DIR=/app/static PORT=8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
