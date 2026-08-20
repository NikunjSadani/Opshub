# Project Spine — PO-to-Cash, Project P&L, Logistics & Quote History (DESIGN)

**Status: DESIGN — the frozen contract for a multi-increment build.** Target increments: **inc 28+** (foundation first, then modules in waves). Owner decisions locked (2026-08-20) — see §3. This doc is the source of truth for the whole "everything about a project in one place" build; individual increments each get their own build wave under §14 and may spawn a per-module design doc, but must not contradict the decisions here.

This reconciles against the **real** current model (grounded 2026-08-20): `project` + `project_client` (`backend/app/modules/projects/models.py`), the vendor-side `expense_invoice` (`expense/models.py`), the reusable numbering ledger (`app/modules/numbering/`), RBAC v2 (`app/platform/rbac.py`), and the module `REGISTRY` (`app/platform/module_registry.py`). Where a new module *changes* something already built, it is called out explicitly.

---

## 1. What this is, in one line

**A lightweight ERP with the Project as the spine.** A Project (already client-linked) gets linked to a client **PO**; everything about that PO — its priced line items, deliveries, client invoices, payments, logistics, and P&L — lives in one place, and the accumulated line-item history becomes a searchable quote/price book.

## 2. The three chains

Everything hangs off the Project as three chains that meet at P&L:

| Chain | Flow | Industry name | Status |
|---|---|---|---|
| **Revenue** | Product → PO line item → **client invoice** → delivery challan → payment | Order-to-Cash (O2C) | NEW (this doc) |
| **Cost** | vendor invoice / **vendor credit note** / expense → tagged to `project_id` | Procure-to-Pay (P2P) | **BUILT** (inc 24/27); vendor credit note NEW (§5h) |
| **Fulfilment** | delivery challan → logistics → proof of delivery | — | Challan BUILT (Phase 1); logistics NEW |

> **Project P&L is simply where the revenue chain meets the cost chain.** That one sentence is the architecture.

## 3. Owner decisions (locked 2026-08-20)

1. **Line item is the atomic unit.** The money spine per line is **ordered (PO) → invoiced → paid**; **delivered is a fulfilment metric, not a money balance** (revenue is on invoice — decision 3). Guards: **over-invoicing** (can't invoice more than a PO line's open qty) and **over-delivery** (can't ship more than its invoice covers).
2. **Client PO soft copy is uploaded and stored** against the PO (document vault).
3. **Invoice-first flow, revenue recognised on invoice.** The order is **PO → client invoice(s) → delivery challan(s)** — goods move *against* an invoice, not the reverse. **One PO → many invoices; one invoice → many challans** (partial shipments). Delivery is therefore a fulfilment concern, decoupled from money.
4. **Credit notes are first-class on BOTH sides:**
   - **Client (outbound)** — we *issue* against a **client invoice**: damaged goods → credit note (reduces recognised revenue + AR, **writes the invoiced quantity back into the PO-line balance**) → re-deliver → re-invoice.
   - **Vendor (inbound)** — we *receive* against a **vendor expense invoice**: captured/stored exactly like an expense invoice (§5h) and **reduces project cost**.
5. **No per-project overhead allocation now.** Project P&L = its PO revenue − expenses **explicitly tagged to that `project_id`**. Overhead **and consolidated freight bills** go to the **general bucket (existing `GEN` / `GEN-001`)** and appear **only in the consolidated P&L**, never split across projects. (Freight quoted *as a PO line item* is that project's revenue/cost — different thing.)
6. **Client master promoted to first-class**, with **multiple GSTINs and multiple billing addresses** per client (state-wise registrations), credit terms, and contacts.
7. **Advances / client deposits built end-to-end in v1** — money can arrive before any invoice (deal closed now, procurement months later); an advance offsets future invoices.
8. **Closure is derived by default, with an explicit audited manual short-close** — when the PO value is higher but the job completes at lower cost/quantity, a user may deliberately close the remaining open balance, recording a **reason + the variance**. Balances stay the truth; the manual close is a captured override event, never a silent toggle.
9. **Google Sheets live sync deferred; Excel downloads now.**
10. **Product Master required** — a normalized product identity (brand / model / category / UOM) that line items reference, so quote history aggregates cleanly instead of fragmenting on free text.
11. **Price history is a business asset → soft-delete / void only.** A PO or line item is never hard-deleted (it would erase searchable pricing history).
12. **GST is a pass-through, not revenue.** P&L uses **net-of-GST taxable value** on both sides.
13. **Quote/price-book search in v1; the quote-builder (CPQ) that converts a won quote into a PO is a fast-follow**, but the schema anticipates it.

## 4. New concepts & entities

- **Product Master** — normalized `product` (name, brand, model number, category, UOM, optional HSN). Line items reference it; entry autocompletes and allows add-on-the-fly. Foundation for quote search.
- **Client Master (promoted `project_client`)** — the existing client entity gains child tables for **GSTINs**, **billing addresses**, **contacts**, and a **credit-terms** field. Same entity that already anchors the 3-letter project code — we **extend in place**, we do not fork a parallel `Client` (keeps `<CLIENT>-NNN` project codes working; per WAYS-OF-WORKING #9).
- **Purchase Order + line items** — the client's PO, its soft copy, and its priced line items (cost, sell, freight, packaging, handling, tax, UOM, quantity, expected procurement date).
- **Line-item running balances** — delivered / invoiced / credited / short-closed quantities, computed from movement rows (see §6).
- **Client invoice (outbound)** + **client credit note** — the sales-side invoice, distinct from the vendor-side `expense_invoice`. Tables are prefixed `billing_` to avoid any confusion with `expense_`.
- **Vendor credit note (inbound)** — captured through the existing expense module via a `doc_type` discriminator, reducing project cost (§5h).
- **Payments & advances** — receipts against invoices; advances that sit as client credit and later offset invoices.
- **Shipment** — logistics row keyed on the delivery-challan number, with tracking + POD.
- **Quote / price-book search** — a search/analytics view over accumulated PO line items.

## 5. Data model (new)

Conventions, all matching what's already in the tree: money is **integer paise in `BigInteger`** columns (never float; convert via `Decimal`, `ROUND_HALF_UP`); model files declaring mapped classes **must NOT** `from __future__ import annotations`; SQLite migrations use `batch_alter_table`; **no hard cross-module ORM `relationship()`** across module boundaries — reference by ID and resolve by explicit `select`/join (as the expense module already does for `Project`).

### 5a. Client master — extend `projects` module

Extend existing `project_client` (add `pan String(10)|None`, `credit_terms_days int|None`), plus child tables:

```
client_gstin      (id, client_id FK→project_client, gstin String(15), legal_name,
                   state_code String(2), is_default bool, active bool, created_at/by)
client_address    (id, client_id FK, gstin_id FK→client_gstin NULL, label,
                   line1, line2, city, state, pincode, is_default bool, active)
client_contact    (id, client_id FK, name, email, phone, designation, is_default)
```
An invoice/PO references a **specific** `client_gstin_id` + `client_address_id` (the state-wise entity it is billed to/from).

### 5b. Product master — `sales_orders` module

```
product   (id, code String|NULL uniq, name String(200), brand String(120)|NULL,
           model_number String(120)|NULL, category String(120)|NULL,
           uom String(20), hsn String(10)|NULL, active bool, created_at/by)
```
Case-insensitive uniqueness on the natural identity uses a **`lower(...)` functional unique index** (an app-level `func.lower` check alone races — learned on `expense_payment_method`).

### 5c. Purchase order + line items — `sales_orders` module

```
purchase_order  (id, po_number String, client_id FK, client_gstin_id FK NULL,
                 project_id FK→project, po_date Date, expected_procurement_date Date|NULL,
                 status String(16) [DRAFT|CONFIRMED|IN_PROGRESS|CLOSED|CANCELLED],
                 soft_copy_file_id FK→files_stored_file|NULL, notes String|NULL,
                 close_reason String(500)|NULL, closed_by/at, created_by/at)
                 -- unique(client_id, po_number)

po_line_item    (id, po_id FK CASCADE, product_id FK→product, description String(500),
                 uom String(20), ordered_qty Numeric,
                 cost_price_paise BigInt, sell_price_paise BigInt,
                 freight_paise BigInt, packaging_paise BigInt, handling_paise BigInt,
                 other_paise BigInt, tax_rate Numeric,           -- GST %
                 line_status String(16) [OPEN|SHORT_CLOSED|CLOSED] (derived+override),
                 short_close_reason String|NULL, created_at)

po_amendment    (id, po_id FK, version int, summary String(500),
                 snapshot JSON, created_by/at)   -- immutable, versioned (never overwrite)
```
`po_number` is the **client's own PO reference** (received from the client, entered/uploaded — **not** minted by us), unique within a client. One project may have several POs; each PO belongs to one project. Bulk Excel upload **and** manual entry both land here; the Excel path reuses the resume-aware, dedup-by-identity+byte-hash discipline already proven on challan/expense (never silently drop a row).

### 5d. Delivery-challan linkage (change to the challan module)

Because the flow is **invoice-first** and one invoice can ship on several challans, the challan ties to its invoice and its lines tie to the PO lines they physically carry:
- `challan.invoice_id` (nullable FK→`billing_invoice`) — the invoice this delivery fulfils (nullable so legacy Phase-1 challans are unaffected; required for new-flow challans).
- `challan_line.po_line_item_id` (nullable FK) — the PO line each challan row delivers, with the **quantity the challan already carries** — this is what makes *delivered* reconcilable per line when an invoice is split across challans.

This is the one change this design imposes on the already-built Phase-1 challan module — additive/nullable. It also nudges challan *generation* toward being **invoice-driven** (generate the challan(s) for an invoice's goods) rather than the current standalone Excel upload; the exact generation UX is settled in the challan wave, not here.

### 5e. Billing — `billing` module (OUTBOUND / sales side)

> **REVISED 2026-08-20 (owner):** client invoices + credit notes are **created in the accounting software and UPLOADED here** — OpsHub does **NOT** generate them, mint their numbers, or compute their GST. `billing` is an **upload → extract → match → review → confirm** module: we reuse the **existing expense pdfplumber text-layer `Extractor`** to pull the uploaded invoice's **line items**, **auto-match** them to the PO's lines (product code / HSN / description + qty + price), flag mismatches on a **review screen**, and let the operator **manually map/correct** any line (the manual fallback; scanned/image invoices with no text layer fall straight to manual, OCR deferred). On confirm the invoice lines tie to PO lines → exact **invoiced-qty per line** (§6). Invoice number + GST are the document's own (captured as printed, never derived). This is **money-critical** → an **eval/gold harness before the engine** + a **DUAL** audit.

```
billing_invoice       (id, invoice_number String, po_id FK, client_id FK,   -- number is EXTERNAL
                       invoice_date Date, due_date Date NULL,   -- from client credit terms or entered
                       taxable_paise BigInt, cgst_paise, sgst_paise, igst_paise, -- captured as printed
                       round_off_paise BigInt (SIGNED), grand_total_paise BigInt,
                       source_engine String, review_reasons JSON,  -- extraction provenance (as expense)
                       soft_copy_file_id FK→files_stored_file, status String(16), notes, created_by/at)
                       -- unique(client_id, invoice_number); paid-status DERIVED; CANCELLED is explicit
billing_invoice_line  (id, invoice_id FK CASCADE, po_line_item_id FK NULL, -- NULL until matched/mapped
                       description String, qty Numeric, unit_price_paise BigInt,
                       taxable_paise BigInt, tax_rate Numeric, match_status String)  -- MATCHED/MANUAL/UNMATCHED
credit_note           (id, cn_number String, invoice_id FK→billing_invoice, client_id FK, -- number EXTERNAL
                       cn_date Date, taxable_paise, cgst/sgst/igst_paise, round_off_paise (SIGNED),
                       grand_total_paise, soft_copy_file_id FK, reason String(500), created_by/at)
                       -- unique(client_id, cn_number)
credit_note_line      (id, cn_id FK CASCADE, po_line_item_id FK NULL, qty Numeric, ...)  -- writes qty back
payment_receipt       (id, client_id FK, invoice_id FK→billing_invoice, amount_paise BigInt,
                       received_on Date, mode String, reference String, note, created_by/at)
client_advance        (id, client_id FK, po_id FK NULL, amount_paise BigInt,
                       received_on Date, mode, reference, note, created_by/at)
advance_application   (id, advance_id FK, invoice_id FK, amount_paise BigInt)  -- advance → invoice
```
Confirmed invoice/CN lines drive **§6's per-line `invoiced_qty`** (ordered→invoiced reconciliation + over-billing detection). A client-invoice's **outstanding** = `grand_total_paise − Σ(credit_note.grand_total for this invoice) − Σ payment_receipt.amount − Σ advance_application.amount`; **status** (UNPAID/PART_PAID/PAID) and **overdue** are **derived**. An **advance's remaining** = `amount_paise − Σ advance_application`. Advance application is a **hybrid**: the app suggests **FIFO** (oldest first) + pre-fills it; the operator can override. Header totals are reconciled against the summed lines (a mismatch is a review flag, not a silent accept).

### 5f. Logistics — `logistics` module

```
shipment  (id, challan_id FK→challan NULL, challan_number String,  -- join key
           po_id FK NULL, tracking_id String, delivery_partner String,
           status String(24), consignee_name, address, phone, pincode,
           dispatched_on Date|NULL, delivered_on Date|NULL,
           pod_file_id FK→files_stored_file NULL, notes, created_by/at)
```
Bulk Excel upload (a dump from the logistics partner) keyed on **challan number**; manual entry also supported. POD captured for dispute/payment defence.

### 5g. Reminders / Action Center

**No new table in v1** — due/overdue items are **computed on read** (see §8). A `reminder`/`notification` table + assignee arrives only with the push-notification wave (owner-gated cloud, §14).

### 5h. Vendor credit notes — extend the EXISTING expense module (cost side)

Credit notes we **receive from vendors** are captured and stored exactly like vendor invoices, through the **already-built** expense pipeline (pdfplumber extraction → canonical schema → review → confirm → register) — the narrowest change, not a parallel module:
- Add a discriminator `doc_type String(16) [INVOICE | CREDIT_NOTE]` (default `INVOICE`, nullable-backfilled) to the expense capture (`expense_invoice`), plus an optional `against_invoice_id` FK→`expense_invoice` linking a credit note to the vendor invoice it credits.
- A `CREDIT_NOTE` carries the same money envelope (supplier, GSTIN, `total_taxable_paise`, GST, `grand_total_paise`) and the same **required Project + Payment-method tagging** as an invoice (inc 27), so it lands in the same register and dashboards.
- In **all aggregation** (spend dashboard, project cost, P&L) a `CREDIT_NOTE` is treated as a **reduction** of cost — i.e. `net cost = Σ INVOICE − Σ CREDIT_NOTE` for a project. Every existing `sum(grand_total_paise)`/`sum(total_taxable_paise)` aggregate must become sign-aware — a **money-path change, so it gets a DUAL audit** (a missed sign flips a cost into extra spend).
- Dedup (identity + byte-hash) and soft-delete rules apply identically. This reuses the extractor, review UI, and confirm guard — the change is a discriminator + a sign rule, not a new capture flow.

## 6. Line-item lifecycle & running balances (the heart)

**Movements are the source of truth; balances are computed** from the rows that reference a `po_line_item`. Because the flow is invoice-first, the **money spine and the physical spine are separate**:

Money (drives revenue, AR, closure):
- `invoiced_qty` = Σ `billing_invoice_line.qty` − Σ `credit_note_line.qty` (a client credit note is **against an invoice** and **writes its quantity back**, so those units can be re-invoiced after redelivery).
- `open_to_invoice` = `ordered_qty − invoiced_qty − short_closed_qty`.

Physical (drives fulfilment/logistics only, never money):
- `delivered_qty` = Σ `challan_line.qty` for that PO line.

**Guards (default-deny at the service layer):**
- **Over-invoicing** — an invoice line cannot invoice more than the PO line's `open_to_invoice`.
- **Over-delivery** — a challan cannot deliver more than its **invoice** covers (you ship against an invoice; `delivered ≤ invoiced` for that invoice).
- **Closure is derived**: a line is financially `CLOSED` when `open_to_invoice == 0`; a PO closes when all its lines do. **Manual short-close (§8)** sets `line_status = SHORT_CLOSED` with a reason, removing the remaining `open_to_invoice` from "expected" (e.g. PO was for 100 but only 80 was ever needed/invoiced) — never pretending anything was delivered.
- **Fulfilment completeness** (per invoice) = each invoice line fully covered by its challans' `delivered_qty`; surfaced in logistics, independent of money.

Optional cached counters may exist for query speed but the computed movement sums are authoritative.

## 7. Project & Consolidated P&L — `finance` module

- **Revenue (project)** = Σ `billing_invoice.taxable_paise` for that project's PO(s) **− Σ credit-note taxable** (net-of-output-GST; revenue-on-invoice per decision 3).
- **Cost (project)** = Σ tagged `expense_invoice.total_taxable_paise` **of `doc_type=INVOICE`** − Σ **of `doc_type=CREDIT_NOTE`** (vendor credit notes, §5h) where `project_id =` that project (net-of-input-GST; GST is creditable/pass-through per decision 12). The aggregate is **sign-aware**.
- **Project margin** = revenue − cost; margin % shown.
- **Overhead + consolidated freight** are tagged to `GEN` / `GEN-001` (or any general-bucket project) → **excluded from every specific project's P&L** (decision 5).
- **Consolidated P&L** = Σ project P&Ls − general-bucket (GEN) expenses. This is the only place overhead and cross-project freight land.
- **Budget-vs-actual (high-value view)** — because a PO line carries an *estimated* cost, project P&L can show **PO budgeted cost vs booked actual expense** and flag variance. Built once revenue + cost both exist.
- **Excel export** of project and consolidated P&L (decision 9).

## 8. Reminders / Action Center (computed v1, push later)

One cross-cutting engine, not per-module features. **v1 = an "Action Center / What needs attention" view computed on read**, zero new infra:
- **Procurement follow-up** — PO with `expected_procurement_date` within T-15 days.
- **Invoicing due** — delivered-but-not-yet-invoiced lines, or an owner-entered invoice-by date passed.
- **AR aging / dunning** — issued invoices past `due_date`, bucketed 0–15 / 16–30 / 31–45 / 45+.

**Deferred to the owner-gated cloud wave:** push email/WhatsApp at T-15 and dunning cadences, per-user assignee, and any scheduler (Cloud Scheduler → Cloud Run job). The computed view delivers ~80% of the value with no background job.

## 9. Quote / price-book search — `sales_orders` module

A search/analytics view over accumulated `po_line_item` rows (joined to `product`, `purchase_order`, `project_client`). **Because every line already carries product + cost + sell + freight + packaging + handling + date + client + qty, this is mostly free once §5c exists** — which is the payoff for the line-item foundation.

- **Multi-keyword + faceted filters** — brand / model / category (via `product`), client, **date range**, and a **budget/price band**.
- **Full component visibility** — cost, sell, **margin %**, freight, packaging, handling.
- **Date is first-class**, most-recent-first, with an optional **per-product price trend** (last N quotes over time) to see inflation — an explicit owner ask.
- **Quantity context** — always show the qty a price was quoted at (a price at qty 1,000 ≠ qty 50).
- **Client context** — "what did we quote before" is the goal; this is your own internal history (fine), just deliberate.
- **CPQ fast-follow (not v1)** — assemble a quote from history → export/send → on win, **convert to a PO with no re-entry**. Schema anticipates it; UI deferred.

## 10. RBAC integration

New modules register via `ModuleSpec(key, title, router, nav_group="Operations")` + `register_module(spec)`; each becomes grantable in the roles editor automatically (the editor reads the live `REGISTRY`). Proposed **new module keys** and their VIEW/OPERATE/MANAGE meaning:

| Module key | Title | VIEW | OPERATE | MANAGE |
|---|---|---|---|---|
| `sales_orders` | Purchase Orders | see POs + quote search | create/amend PO | short-close, void, product master, series seed |
| `billing` | Invoicing & Receivables | see invoices/AR | raise invoice, record payment/advance, issue credit note | void/adjust, billing settings |
| `logistics` | Logistics Tracking | view shipments | upload/edit shipments | manage/delete |
| `finance` | P&L & Finance | view project + consolidated P&L, export | — | — |

Client-master edits attach to the **existing `projects`** module at MANAGE. New `ACTION_CATALOG` entries (same `_ModuleReq(key, level)` / `_PlatformReq(perm)` shape, default-deny):

```
po.create (sales_orders,OPERATE)     po.amend (sales_orders,OPERATE)
po.short_close (sales_orders,MANAGE) po.void (sales_orders,MANAGE)
product.manage (sales_orders,MANAGE) quote.search (sales_orders,VIEW)
invoice.raise (billing,OPERATE)      creditnote.issue (billing,OPERATE)
payment.record (billing,OPERATE)     advance.record (billing,OPERATE)
invoice.void (billing,MANAGE)        shipment.upload (logistics,OPERATE)
shipment.manage (logistics,MANAGE)   client.manage (projects,MANAGE)
pnl.view (finance,VIEW)
```
Administrator (`role.is_system`) keeps MANAGE-everywhere + all platform perms. `GET /me` continues to drive front-end gating.

## 11. Numbering reuse (+ one small generalization)

The numbering **ledger** (`numbering_counter` + `numbering_allocation`, `seed_series`/`allocate`/`issue`/`void`, `entity`/`entity_id` binding) is reused for **our outbound invoice / credit-note** numbers — each a distinct `series` (`"INV"`, `"CN"`) seeded once via `seed_series`, then `allocate` + `issue(entity="billing_invoice"|"credit_note", entity_id=...)`. (The **PO number is client-supplied**, so it is NOT minted here.) **Generalization (done in the foundation wave, Stage 0):** `format_number` hardcoded `_ISSUER_PREFIX = "GIF/DC"` (challan format); the counter now carries a per-series **`prefix`** (default `"GIF/DC"`, backward-compatible with the "L" challan series) so INV/CN can seed their own (`"GIF"` → `GIF/26-27/INV/000001`).

## 12. Cross-cutting principles (apply to every wave)

- **Soft-delete / void only** for POs, line items, invoices — price history is an asset (decision 11).
- **Derived state, with audited manual override** for closure and invoice paid-status (decisions 8, and §5e/§6).
- **Reference by ID, resolve by join** across module boundaries — no hard cross-module ORM relationships (matches expense↔project today).
- **Money = integer paise in `BigInteger`**, `Decimal`/`ROUND_HALF_UP`; GST separated from taxable value everywhere so P&L stays net (decision 12).
- **Model files with mapped classes: no `from __future__ import annotations`.**
- **SQLite migrations: `batch_alter_table`**; Postgres enum/type handling is invisible to the SQLite suite → **verify every migration on real Postgres before deploy**.
- **Case-insensitive uniqueness needs a `lower()` functional index.**
- **Audit** money-adjacent actions (short-close, void, payment/advance, credit note) to the append-only `AuditLog`.

## 13. Scope boundaries (explicitly NOT in v1)

- Live Google Sheet sync, push email/WhatsApp reminders, and **any scheduler** → owner-gated cloud wave.
- The CPQ **quote-builder** that converts a quote to a PO (search only in v1).
- **Per-project overhead / freight allocation** (consolidated only — decision 5).
- Multi-currency (INR only; state the assumption).
- e-invoice (IRN) / e-way-bill generation — flagged as a compliance placeholder, not built now.

## 14. Build sequence (waves)

Each wave is an increment: I own the shared foundation + the gate + runtime verify + an independent adversarial audit (**DUAL** for money/auth/destructive) + a UI/UX lane; parallel sub-agents build slices (**told NO git commands**); `develop` stays unpushed until green.

1. **Foundation** — Product Master + Client Master (multi-GSTIN/address/contacts/credit-terms) + PO/line-item entry (Excel + manual, amendments, soft-copy upload) + numbering-prefix generalization + the `sales_orders` module & RBAC wiring. *Everything else depends on this.*
2. **Revenue + AR (parallel after 1)** — client invoice + client credit note + payments + advances + AR tracker (`billing` module); **and** Project + Consolidated P&L + Excel export (`finance` module). **Also here (cost side): vendor credit notes** — the `doc_type` + sign-aware-aggregate extension to the existing expense module (§5h, money-path → DUAL audit). These share no code and can run in parallel.
3. **Fulfilment** — challan → `invoice_id` + challan-line → `po_line_item` linkage (the §5d challan change) + Logistics tracking module (Excel/manual, POD).
4. **Action Center** — computed reminders across PO / invoicing / AR (no infra).
5. **Owner-gated cloud (batches with deploy)** — live Sheet sync, push reminders/schedulers, and any e-invoice/e-way integration.

**Critical path:** Wave 1 must land first and land clean — the line-item model is the fork the whole spine rests on.

---

*Grounded against the live tree 2026-08-20. Update the decisions in §3 and the waves in §14 as they change; keep `docs/RESUME.md` the authoritative live-state log.*
