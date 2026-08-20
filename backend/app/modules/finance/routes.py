"""Finance (P&L) API — filled by the Wave-2 finance agent. RBAC key ``finance``."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

MODULE_KEY = "finance"
