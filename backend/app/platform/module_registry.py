"""Module-registration convention (NOT a runtime plugin loader).

Each feature module exports a `ModuleSpec`. `main.py` composes them at startup:
mounts the router under /api/v1/<key>, records nav + the module key used for
per-user module-access checks. Adding a module = add a ModuleSpec + register it.
No dynamic loading, no framework — just a typed object the app wires up.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter


@dataclass(frozen=True)
class ModuleSpec:
    key: str                      # stable id, e.g. "document_automation" (used for module-access)
    title: str                    # human label for nav
    router: APIRouter             # the module's API routes
    nav_group: str = "Operations"  # dashboard grouping
    coming_soon: bool = False      # cosmetic placeholder tile
    permissions: list[str] = field(default_factory=list)  # action keys this module defines


# The single registry the app builds from. Order = nav order.
REGISTRY: list[ModuleSpec] = []


def register_module(spec: ModuleSpec) -> ModuleSpec:
    if any(m.key == spec.key for m in REGISTRY):
        raise ValueError(f"duplicate module key: {spec.key}")
    REGISTRY.append(spec)
    return spec


def grantable_modules() -> list[ModuleSpec]:
    """Real, user-facing modules a role may be granted access to.

    Excludes the `_system` nav group, the health module, and any `coming_soon`
    placeholder (a not-yet-live module must not be grantable — an early grant could
    reach it the moment it ships a router). Read from the live REGISTRY, so a newly
    registered module becomes grantable with no change here. Used by the roles editor
    (per-module level pickers) and to validate role/module grants server-side.
    """
    return [
        m for m in REGISTRY
        if m.nav_group != "_system" and m.key != "health" and not m.coming_soon
    ]


def grantable_module_keys() -> set[str]:
    """The set of module keys `grantable_modules()` exposes (for validation)."""
    return {m.key for m in grantable_modules()}
