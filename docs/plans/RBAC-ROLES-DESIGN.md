# RBAC v2 — Custom roles + per-module access levels (DESIGN)

Status: **DESIGN — awaiting owner go-ahead to build.** Target increment: **inc 26.**
Owner decisions locked (2026-08-19): **per-module access levels** (Option A) **+ a custom
role builder** (Option 2), composed together; **no per-entity data scoping** (modules +
actions only). This document is the frozen contract for the build.

> **Buildable now.** This is *authorization*, not *authentication* — it does not need
> Firebase. It's verifiable today through the dev-auth shim + E2E, exactly like the current
> RBAC. Onboarding real distinct humans still waits on the Firebase wiring in the deploy
> pass, but the whole permission model is provable before then.

---

## 1. What changes, in one line

Today access has two coarse tiers: *you have a module (full non-admin use) or not*, plus a
hard-coded *Admin-only* action set, and the four roles (Admin/MIS/Operations/Finance) are a
fixed enum where only **Admin** actually behaves differently. We replace that with:

- **Access levels per module** — every module grant carries a level: **View → Operate → Manage**.
- **Custom roles** — an admin composes a **named role** in the UI out of `{module → level}`
  entries plus a couple of platform permissions, then assigns **one role per user**.

## 2. The three concepts

### 2a. Access levels (the atomic unit) — ordered View < Operate < Manage
Per module, a user's role grants at most one level. Higher includes lower.

| Level | Means | Delivery Challan | Expense/Invoice | Projects |
|---|---|---|---|---|
| **View** | Read-only | See register/batches/numbering, **download challans**, summary | See invoice register, open an invoice | See clients & projects |
| **Operate** | Do the day-to-day work | Upload → validate → generate, reprint | Upload → extract → review → **confirm** | Create projects |
| **Manage** | Module-admin / sensitive & destructive | **Void**, recover a wedged batch, bulk-override validation, **edit master data**, seed a series, override a number | **Delete** a confirmed invoice | **Register clients**, change project status |
| *(no grant)* | No access | module hidden from nav + screen gated | — | — |

*(No grant for a module = the module is invisible and the endpoints deny — unchanged
fail-closed behaviour.)*

### 2b. Platform permissions (not tied to a module)
A small set of cross-cutting capabilities, held as flags on a role:

- **`iam`** — Manage users **and** roles (create/edit/deactivate users; create/edit/delete
  roles; assign roles). This is the super-power — whoever has it controls everyone's access.
- **`settings`** — Edit platform settings (the governed `Setting` store).

*(Kept to two on purpose. `iam` bundles user+role management because they're the same trust
level — a person who can edit roles can already grant themselves anything.)*

### 2c. Roles (named bundles) — user has exactly ONE
A **role** = a name + description + a set of `{module → level}` entries + a set of platform
permissions. A user is assigned exactly one role (simplest correct mental model; if someone
needs a unique combination, you make a role — they're cheap). *Multi-role/union and per-user
overrides are deliberately out of v1; the model leaves room for them.*

**Built-in protected role**
- **Administrator** — every module at **Manage** + all platform permissions. `is_system`,
  **non-deletable and non-editable**, and **at least one active user must always hold it**
  (extends today's last-admin guard). This is the safety floor.

**Seeded preset roles** (ordinary editable/cloneable roles, provided as a head-start — an
admin can rename, retune, clone, or delete them):
- **Challan Operator** — Delivery Challan: Operate.
- **Challan Manager** — Delivery Challan: Manage.
- **Finance** — Expense/Invoice: Operate.
- **Viewer** — every module: View.

## 3. Data model (new)

```
role
  id, name (unique, case-insensitive), description,
  is_system (bool; true only for Administrator → protected),
  created_at, created_by

role_module_permission
  id, role_id -> role, module_key (e.g. "document_automation"),
  level ENUM(VIEW, OPERATE, MANAGE),
  UNIQUE(role_id, module_key)          # a missing row = no access to that module

role_platform_permission
  id, role_id -> role, permission_key ENUM(iam, settings),
  UNIQUE(role_id, permission_key)

user
  role_id -> role                       # REPLACES the old `role` enum column
```

**Cutover, not migration.** There are **no real users in prod yet** (Firebase isn't wired;
only the mock admin + dev shim exist), so this is a clean replace with **zero production-data
risk**: create the new tables, seed Administrator + the presets, point the seed/mock-admin at
Administrator, and **drop the old `Role` enum column and the `user_module_access` table**
(its binary grants are superseded by role levels). One Alembic migration.

## 4. Enforcement (backend) — the action catalog is the single source of truth

We keep the endpoint call-sites almost unchanged (`can(user, "challan.void")`,
`can_access_module(user, key)`), and back them with a **code registry** (the same pattern as
`settings/registry.py`) that maps every action to its requirement:

```
ACTION_CATALOG = {
  # module-scoped: (module_key, min_level)
  "challan.generate":         ("document_automation", OPERATE),
  "challan.void":             ("document_automation", MANAGE),
  "challan.recover":          ("document_automation", MANAGE),
  "validation.override_bulk": ("document_automation", MANAGE),
  "masterdata.edit":          ("document_automation", MANAGE),
  "series.seed":              ("document_automation", MANAGE),
  "numbering.override":       ("document_automation", MANAGE),
  "project.create":           ("projects", OPERATE),
  "project.manage":           ("projects", MANAGE),
  "expense.confirm":          ("expense_invoice", OPERATE),
  "expense.delete":           ("expense_invoice", MANAGE),
  # platform-scoped: a permission_key
  "user.manage":              PLATFORM("iam"),
  "role.manage":              PLATFORM("iam"),
  "settings.edit":            PLATFORM("settings"),
}
```

New `rbac.py` surface:
- `module_level(user, key) -> Level | None` — Administrator ⇒ MANAGE everywhere; else the
  role's `role_module_permission` row.
- `can_access_module(user, key)` — `module_level(...) is not None` (≥ View).
- `can(user, action)` — resolve `action` in the catalog: a module rule checks
  `module_level(user, key) >= min_level`; a platform rule checks the role's platform
  permission. **Default-deny preserved**: an action absent from the catalog returns `False`
  (keeps today's "a typo can't authorize everyone" property).

Every current call-site keeps working; a handful of read endpoints that were previously
"any module user" get an explicit `>= VIEW` gate, and the ~10 `ADMIN_ONLY` actions move from
"role == ADMIN" to their catalog rule (mostly `MANAGE` on their module).

## 5. Admin UI

Two screens, both gated on the `iam` platform permission:

**A) Roles (new screen, `/admin/roles`)**
- Table of roles (name, description, #users using it, a compact grant summary).
- **Create / Edit / Clone** modal: name + description, a **row per module** with a segmented
  control **None · View · Operate · Manage**, and toggles for the platform permissions
  (`Manage users & roles`, `Edit settings`). A live "what this role can do" preview.
- **Delete** — blocked while any user holds the role (offer "reassign N users first"); the
  Administrator role is shown **read-only** and can't be edited or deleted.

**B) Users (existing screen, reworked)**
- The role-enum dropdown + module checkboxes are **replaced by a single Role picker** that
  lists the custom roles, with an inline preview of what the chosen role grants.
- Invite / edit / enable-disable and the "no password, setup-link" flow are unchanged.

**Action-level UI gating.** Buttons for Operate/Manage actions read the user's module level
(e.g. **Void** and **Edit master data** are hidden/disabled below Manage; **Generate** below
Operate), so the UI never shows an action the API would reject.

## 6. Safety, audit, tests

- **Last-Administrator guard** (extend the existing one): can't delete, deactivate, or
  move-off-Administrator the last active user holding it → 409, unchanged.
- **Role-in-use guard**: a role assigned to any user can't be deleted.
- **No stale grants**: permission checks read the role live per request (no cached grant set),
  so editing a role takes effect immediately — including *reducing* someone's access.
- **Escalation containment**: only `iam` can touch roles/users; a role edit that would remove
  the last Administrator is refused; `iam` holders editing their own role can't drop below
  their current powers in a way that orphans Administrator.
- **Audit**: every role create/edit/delete and every user role change appended to the existing
  hash-chained audit log (actor, before/after).
- **Verification** (per WoW #4/#5): full gate; a **DUAL adversarial audit** (auth/identity
  path) + a **UI/UX audit**; **E2E** extending `nav-auth.spec` to prove: a View user sees a
  module but can't Operate; an Operate user can't Manage (Void/Delete blocked at API + hidden
  in UI); a role edit changes a live session's access; the last-Administrator guard holds.
- **Dev shim**: the local role-switcher is updated to assume a chosen **role** (not the old
  enum), so level-gating is exercisable locally and in E2E without Firebase.

## 7. Scope boundaries (explicitly NOT in v1)

- **No per-entity data scoping** (owner decision) — anyone with a module sees all its data
  (all clients/consignors/series). The `resource` hook in `rbac.py` stays reserved for a
  future "restrict to clients X,Y" axis; adding it later touches every list query, so it's a
  separate, larger increment.
- No multi-role-per-user / per-user overrides on top of a role (make a role instead).
- No time-boxed or approval-gated grants.

## 8. Rough shape of the build (orchestrated increment)

1. **BE foundation (me):** models + enum + migration + `rbac.py` rewrite + action catalog +
   the last-admin/role-in-use guards + seed (Administrator + presets).
2. **BE role CRUD** (agent): `role` service + admin routes (`iam`-gated) + tests.
3. **FE Roles screen** (agent) + **FE Users rework** (agent) + the action-level button gating.
4. **Integration + full gate (me)** → **DUAL + UI/UX audit** (agents) → fold fixes →
   **E2E** level-gating spec → docs + memory sweep.

Not runtime-verifiable with real human logins until Firebase is wired, but fully provable now
via the dev shim + E2E (same standard as the current RBAC).
