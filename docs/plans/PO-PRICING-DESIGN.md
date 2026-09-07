# PO Line-Item Pricing — Design & Business Rules

Status: **built, gate-green, not yet deployed** (see the build plan for cutover state).
Owner decisions are logged at the bottom with dates.

The purchase-order line item carries a full pricing model so we can record what we quote
the client, what we actually transact, and what the vendor sees — and compute the revenue we
bill the client. Money is always **integer paise**; per-unit vs per-line is called out below.

---

## 1. Price tiers (per line)

Each line has **three sell tiers** and **three freight tiers**:

| Tier | Column | Per | Visibility | Meaning |
|------|--------|-----|------------|---------|
| Client sell | `client_sell_price_paise` | unit | **all users** | The price quoted to the client (the primary shown sell). Required. |
| Actual sell | `sell_price_paise` | unit | **admin only** | What we actually transact at — the **margin basis**. |
| Vendor sell | `vendor_sell_price_paise` | unit | all users | The vendor-disclosed sell (optional; phase-2 use). |
| Client freight | `client_freight_paise` | line (flat) | all users | Freight quoted to the client — **revenue** (see §4). |
| Actual freight | `freight_paise` | line (flat) | **admin only** | Freight we actually incur/transact. |
| Vendor freight | `vendor_freight_paise` | line (flat) | all users | Vendor-disclosed freight (optional). |

**Cost** has two fields: `original_cost_price_paise` (Original CP, optional) and
`cost_price_paise` (Our / billed CP — the existing cost field). Incorporating the vendor's
50%-profit-share into the billed CP is **phase-2** and not yet modelled.

`packaging_paise` / `handling_paise` / `other_paise` are flat per-line charges, single-tier
(no client/vendor/actual split). They **are** client charges — billed to the client, so they
count as revenue and toward the agency base (see §3–§4), exposed aggregated as
`total_client_extras_paise`.

## 2. Admin-only "actual" masking (RBAC)

The **actual** sell and **actual** freight (and anything derived from them — margin, the actual
sell total) are sensitive and returned **only** to platform-**IAM** admins
(`has_platform(user, PlatformPerm.IAM)`). For every other user the API returns `null`.

Enforced consistently across **every** surface:
- All PO endpoints (list / detail / create / amend / confirm / short-close / void) mask the
  per-line actuals and the actual sell total.
- A **non-admin cannot write an actual** on create — it defaults to the client figure; the
  request body's `sell_price_paise`/`freight_paise` are ignored.
- On **amend**, the admin-set actual is **carried forward** (matched by *product*, not list
  position, so a reorder/insert never wipes it) whenever the amended line omits an actual —
  for admins-who-omit as well as non-admins.
- **Quote search** masks the actual sell, the actual freight, and the margin for non-admins,
  and its budget filter bounds the *client* price (never the actual). The visible client
  freight is returned instead.

## 3. Agency fee

The agency fee is **additional revenue** we charge the client (not a cost). Header-level, one
per PO:

- `agency_fee_type` ∈ `NONE` | `PERCENT` | `FIXED`.
- `PERCENT`: `agency_fee_percent` (Numeric 6,3) applied to the **entire client billing** —
  goods client-sell **+** client freight **+** packaging/handling/other (HALF-UP).
- `FIXED`: `agency_fee_amount_paise`, a flat amount.
- Visible to **all** users (it is not a sensitive/actual figure).

## 4. Revenue & how it nets to what's invoiceable

**Total client revenue** = the entire client billing (goods + client freight + packaging/
handling/other) **+** agency fee. Exposed as `total_with_agency_paise`, with the components
`total_client_sell_paise`, `total_client_freight_paise`, `total_client_extras_paise`,
`agency_fee_computed_paise` alongside. The agency fee is a % of the billing base; it is **not**
compounded on itself.

All revenue reflects only what is **actually invoiceable**:

- **Void (PO → CANCELLED):** every money total (goods, freight, agency, and the admin actual
  total) collapses to **0**.
- **Short-close:** a line's billable quantity is `ordered_qty − short_closed_qty` (floored at
  0). Goods and the PERCENT agency base shrink with it. **Client freight** is a flat per-line
  charge: a **fully** short-closed line (nothing ships) drops its freight; a **partial**
  short-close keeps the flat freight (the shipment still happens).
- **FIXED agency fee** does **not** shrink under a short-close — it stays flat (a flat
  contractual charge) and is zeroed **only** by a void.

Key helpers (`app/modules/sales_orders/po_service.py`; the only consumer is the route
serializer `po_routes._summary_out`): `_net_qty`, `_is_voided`, `line_client_sell_paise`,
`po_total_client_sell_paise`, `line_client_freight_paise`, `po_total_client_freight_paise`,
`agency_fee_paise`, `po_total_with_agency_paise`.

## 5. Optional PO number

The PO number is now **optional** (pricing is agreed with the client before the PO arrives;
the number is added later via amend). Uniqueness is a **partial** unique index on
`(client_id, po_number) WHERE po_number IS NOT NULL`, so multiple no-number POs for one client
coexist while a duplicate *non-null* number still collides. Migration `a7f3c1b2d4e5`.

---

## Owner decisions log

- **2026-09-07** — Three sell prices: client-quoted (shown), **actual** (admin-only, the P&L
  basis), vendor (shown). Same three-tier split for freight (actual admin-only). Agency fee is
  a % of the order value, sometimes a fixed amount instead; it is **visible** revenue we charge
  the client. Cost has Original CP + Our/billed CP (vendor 50%-profit-share deferred to
  phase-2). PO number optional / addable later.
- **2026-09-07** — Revenue nets to what's invoiceable: **void → 0**; **short-close → net qty**.
  A **FIXED** agency fee **stays flat** under a (even full) short-close; only a void zeroes it.
- **2026-09-07** — **Client freight is revenue** we get from the client → counted in
  `total_with_agency` (goods + freight + agency).
- **2026-09-07** — Agency **%** is charged on the **entire client billing** (goods + client
  freight), not goods alone.
- **2026-09-08** — Packaging / handling / other **are** client charges → they count as client
  revenue and toward the agency base. "Entire client billing" now = goods + client freight +
  packaging + handling + other.
