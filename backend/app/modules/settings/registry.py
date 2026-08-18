"""Settings registry — the governance layer for the key→value `Setting` store.

The `Setting` table is a schemaless key→value bucket. Left ungoverned it is the
natural (dangerous) place for a secret to land, and every row would be world-readable
to any authenticated user. This registry makes the store DELIBERATE:

  * **Only declared keys are writable.** A key not listed here is rejected on write,
    so a secret (an SMTP password, an API key, a token) has NO path into this table —
    secrets belong in Secret Manager, where the sweep/Firebase credentials already live.
  * **Reads are gated by each key's declared `visibility`.** `PUBLIC` settings are
    readable by any authenticated user (non-sensitive operational config the UI may
    need); `ADMIN` settings are readable only by admins.
  * **Unknown/legacy keys FAIL CLOSED** — a row whose key isn't declared reads as
    admin-only, so a stray value can never be silently exposed to non-admins.

To add a setting: declare it here with a deliberate visibility (something in the app
must read it anyway, so declaring it at the same time is no extra friction).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class Visibility(str, enum.Enum):
    PUBLIC = "public"  # any authenticated user may read
    ADMIN = "admin"    # only admins may read


@dataclass(frozen=True)
class SettingSpec:
    key: str
    visibility: Visibility
    description: str


SETTINGS_REGISTRY: dict[str, SettingSpec] = {
    "eway_threshold": SettingSpec(
        key="eway_threshold",
        visibility=Visibility.PUBLIC,  # a non-sensitive threshold; UI may read it
        description="Value (in paise) at or above which a challan flags an e-way bill.",
    ),
}


def is_writable(key: str) -> bool:
    """A key may be written only if it is declared — blocks smuggling a secret in."""
    return key in SETTINGS_REGISTRY


def can_read(key: str, *, is_admin: bool) -> bool:
    """Whether a caller may read `key`. Unknown/legacy keys fail closed to admin-only."""
    spec = SETTINGS_REGISTRY.get(key)
    if spec is None:
        return is_admin
    return is_admin or spec.visibility is Visibility.PUBLIC
