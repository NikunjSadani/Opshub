"""Logistics API — filled by the Wave-3 logistics agent. RBAC key ``logistics``:
``shipment.upload`` = OPERATE, ``shipment.manage`` = MANAGE, reads = VIEW.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

MODULE_KEY = "logistics"
