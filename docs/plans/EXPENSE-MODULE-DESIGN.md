# Expense / Invoice Module — FROZEN DESIGN (Phase 2 v1, invoice-first, zero-cost)

Status: contract frozen 2026-08-19 for build. Module key `expense_invoice`, code in
`backend/app/modules/expense/` + `frontend/src/pages/expense/`. Mirrors the Challan
module's structure and house conventions.

## Scope & posture
Ingest **inbound, vendor-issued GST tax invoices** (expense/purchase capture) → extract
fields → per-field envelope → human review of low-confidence fields → confirmed canonical
record + searchable register. **No numbering engine** (the invoice number is the vendor's,
extracted, not minted). The hard problem is faithful **extraction → review → confirm**.

Document-Intelligence guardrails (DESIGN §4) honoured: **engine-neutral canonical schema
first**, **eval harness before trusting the engine**, one engine now (`TextLayerExtractor`
over `pdfplumber`, deterministic, zero-cost, egress-locked), a config-flip slot for a paid
engine later. House conventions carried over verbatim from Challan: **money = integer paise
via `Decimal` + `ROUND_HALF_UP`, never float**; ORM-free dataclasses are the seams; every
parse helper returns `None`/clean error on malformed input, never a 500; models use
`str`-Enum + `CheckConstraint` + explicit `String(n)` widths; **no `from __future__ import
annotations`** in models (py3.14 SQLAlchemy crash).

**Reuse (do not re-implement):** `app.modules.masterdata.normalize` — `valid_gstin` (full
checksum + state code), `gstin_matches_state`, `match_key`, `collapse_ws`,
`STATE_NAME_TO_CODE`, `VALID_STATE_CODES`. The `parse_paise` `Decimal×100 ROUND_HALF_UP`
discipline (PDF free-text needs its own tokenizer, but the discipline is the same).

## Owner decisions (locked)
- **F2 — HARD dedup + delete-and-re-upload.** `dedup_key = sha256(norm_gstin | match_key(inv_no) | date | grand_total_paise)` has a **UNIQUE** constraint. A bulk upload is **N PDF files, one invoice each** (a single multi-invoice PDF is deferred to v2). On a dedup collision the upload of that file is **rejected (409)** carrying the existing invoice's id + summary; the FE shows a **"invoice already exists — delete the previous to re-upload?"** dialog; confirming calls `DELETE /expense/invoices/{id}` (audited) then re-uploads. Supports re-scan/correction without silent double-entry.
- **F3 — buyer validation DEFERRED.** Extract + store `buyer_gstin`, but do NOT hard-validate against Gifsy's own entity in v1 (needs the owner's real GSTIN(s) as config). Additive follow-up.
- **F1 — per-field envelope stored as rows** (`expense_invoice_field`), not a JSON blob (query + correction-audit).
- **F4 — static confidence ladder** tuned by the eval reliability table (no learned calibrator yet).
- **F5 — tolerances:** `TOTALS_TOL_PAISE = 0` (round-off is its own explicit line), `LINE_TAX_TOL_PAISE = 100` (₹1 vendor rounding).

## Canonical schema — `expense/canonical.py` (engine-neutral, ORM-free)
`CANONICAL_SCHEMA_VERSION = "gst_invoice/1.0.0"` — bump on any field add/rename/semantics
change; travels inside every `ExtractedInvoice`.

Per-field envelope `Field[T]`: `value_normalized: T|None` · `value_raw: str` ·
`confidence: float (0..1, calibrated)` · `source_engine: str` ("text_layer/1.0" | "human") ·
`page: int|None` · `bbox: (x0,y0,x1,y1)|None` · `status: OK|LOW_CONFIDENCE|MISSING|CORRECTED`.
Typed aliases: `MoneyField=Field[int]` (paise), `DateField=Field[date]`,
`NumField=Field[Decimal]` (qty, gst%), `TextField=Field[str]`.

- `InvoiceHeader`: supplier_name/gstin/address, buyer_name/gstin/address, invoice_number,
  invoice_date, place_of_supply, po_ref.
- `InvoiceLine` (first-class list): line_no(int, structural), description, hsn_sac, quantity,
  unit, unit_rate_paise, taxable_paise, gst_rate, cgst_paise, sgst_paise, igst_paise,
  line_total_paise.
- `InvoiceTotals`: total_taxable_paise, total_cgst/sgst/igst_paise, round_off_paise(SIGNED),
  grand_total_paise, amount_in_words.
- `ArithmeticChecks`: lines_sum_matches_taxable, per_line_tax_consistent, totals_add_to_grand,
  supply_type_consistent, max_abs_delta_paise.
- `ExtractedInvoice`: schema_version, doc_type("gst_invoice"), source_engine, page_count,
  needs_ocr(bool), review_needed(bool), review_reasons(list[str]), header, lines, totals,
  arithmetic, raw_text, content_hash, dedup_key.

## Extraction — `expense/text_layer.py` (`Extractor` Protocol in `extractor.py`)
`Extractor.extract(pdf_bytes, *, doc_type="gst_invoice") -> ExtractedInvoice`. One impl now:
`TextLayerExtractor` (name "text_layer/1.0"). `get_extractor(setting="text_layer")` factory;
"docai" raises `NotImplementedError` (deferred). Non-`gst_invoice` doc_type → clean `ValueError`.

> **UPDATE:** `get_extractor()` now defaults to **`setting="auto"` → `TallyAwareExtractor`**
> (`app/modules/expense/tally.py`), which routes Tally 'Tax Invoice' PDFs to a dedicated
> `TallyInvoiceExtractor` and delegates every other document unchanged to `TextLayerExtractor`.
> `"text_layer"` still forces the generic engine (used by the eval gold-set harness).

1. **Text-layer gate (fail soft):** `Σ page.extract_text()`; if `< ~20×page_count` chars →
   `needs_ocr=True`, all fields MISSING, `review_reasons=["no text layer — scanned/image PDF, OCR deferred"]`. Encrypted/corrupt/zero-page → caught → REJECTED quality-gate (never a 500).
2. **Header fields:** label-anchored regex over `extract_words()` positions. GSTIN regex +
   `valid_gstin` checksum; supplier = GSTIN nearest the supplier block (top-left) / buyer =
   nearest "Bill To". invoice_number/date/place_of_supply by label anchors; date tokenizer
   DMY-first (DD-MM-YYYY, DD/MM/YYYY, DD-Mon-YYYY, ISO) → `date`.
3. **Line items:** `page.extract_tables()`; map header row → canonical columns by fuzzy
   header match (resolve by header, NEVER by index — column order varies). Money → paise via
   the `parse_paise` discipline. Multi-page tables concatenated before mapping.
4. **Arithmetic cross-checks (validator, all integer paise):** Σ line taxable == total taxable
   (±TOTALS_TOL); per line `round(taxable×rate/100) == cgst+sgst+igst` (±LINE_TAX_TOL);
   taxable+taxes+round_off == grand_total; supply-type (place_of_supply state vs
   supplier_gstin[:2] → intra=CGST+SGST/IGST=0, inter=IGST/CGST+SGST=0).
5. **Calibrated confidence ladder:** anchored+type-valid+arithmetic-corroborated → 0.95 OK;
   anchored+type-valid, no arithmetic → 0.80 OK; positional/weak/arithmetic-fails → 0.50
   LOW_CONFIDENCE; not found → MISSING. Ladder thresholds tuned by the eval reliability table.
6. **review_needed** if ANY: a REQUIRED field (supplier_gstin, invoice_number, invoice_date,
   grand_total_paise, total_taxable_paise, ≥1 line) is MISSING/LOW_CONFIDENCE; a GSTIN fails
   checksum; any ArithmeticChecks flag false; line table missing / Σ mismatch; needs_ocr.
   Collect ALL reasons (never stop at first).

## Eval harness — `expense/eval/` + `tests/expense/gold/`
Gold fixture per dir: `source.pdf` + `expected.json` (labelled canonical values only) +
`meta.json`. Field scorers: **exact** (ids/GSTIN via collapse_ws; names via match_key +
token-overlap partial credit), **numeric-tolerance** (money ±tol_paise; qty/gst% exact
Decimal), **date-normalized**. `score(predicted, gold) -> EvalReport` with per-field accuracy,
line-item P/R/F1, doc-exact rate, **calibration** (confidence-bin → actual accuracy, tunes the
ladder), **review precision/recall** (does the gate flag exactly the wrong docs?). A test
asserts accuracy/doc-exact don't regress below frozen thresholds.
**Synthetic gold fixtures (generated deterministically from an HTML template via the existing
WeasyPrint render path so labels are known by construction):** (1) `intra_single_line`
(KA→KA, CGST+SGST, happy path); (2) `inter_multi_line` (MH→KA, IGST, 3 lines/2 HSNs/mixed
rates, non-zero round-off); (3) `edge_twopage_reorder` (line items span a page break, column
order Qty|Rate|Description|HSN|Amount, a discount sub-total row, one line needing ±₹1);
(4) `scanned_no_text` (image-only → asserts needs_ocr + all-MISSING).

## Dedup + quality gate — `expense/dedup.py`
`dedup_key` (see F2) UNIQUE. Belt-and-suspenders `content_hash = sha256(normalized raw_text)`.
On upload: compute dedup_key; if it collides an existing invoice → **409 with the existing
invoice id + summary** (drives the delete-and-re-upload dialog). Quality gate: unreadable/
encrypted/zero-page/non-PDF → REJECTED (clean operator message, log detail server-side, no
stack). HEIC/image content-type → NEEDS_OCR immediately (never fed to the text engine).

## Data model — `expense/models.py` (tables prefixed `expense_`)
- `InvoiceBatch` (`expense_batch`): one upload run; source_file_id→files_stored_file, status
  (PENDING/EXTRACTING/COMPLETED/FAILED), invoice_count, message, created_by, created_at,
  updated_at. (A batch is 1:N over uploaded files; each file → one Invoice.)
- `Invoice` (`expense_invoice`): batch_id, source_file_id, schema_version, source_engine,
  status (UPLOADED/EXTRACTED/NEEDS_REVIEW/NEEDS_OCR/CONFIRMED/REJECTED + CheckConstraint),
  needs_ocr, review_reasons(JSON), **dedup_key(String(64) UNIQUE), content_hash(index)**,
  snapshotted canonical scalars (supplier_name/gstin(15)/address, buyer_gstin(15),
  invoice_number(64), invoice_date(Date,index), place_of_supply, total_taxable/cgst/sgst/igst/
  round_off(SIGNED)/grand_total_paise: BigInteger, amount_in_words(600)), created_by/at,
  confirmed_by/at.
- `InvoiceLineItem` (`expense_invoice_line`): invoice_id(CASCADE,index), line_no, description,
  hsn_sac, quantity Numeric(14,3), unit, unit_rate/taxable/cgst/sgst/igst/line_total_paise
  BigInteger, gst_rate Numeric(5,2).
- `InvoiceField` (`expense_invoice_field`): invoice_id(CASCADE,index), field_path(64)
  ("header.supplier_gstin" | "line.3.taxable_paise" | "totals.grand_total_paise"),
  value_normalized(600), value_raw(600), confidence Numeric(4,3), source_engine, page, bbox
  JSON, status + CheckConstraint, UNIQUE(invoice_id, field_path).
- `InvoiceCorrection` (`expense_invoice_correction`): invoice_id(CASCADE,index), field_path,
  old_value, new_value, corrected_by, corrected_at, promoted_to_gold(bool).

**State machine:** UPLOADED →extract→ EXTRACTED →(no review)→ [accept] → CONFIRMED;
EXTRACTED/NEEDS_REVIEW →[correct+accept]→ CONFIRMED; no-text → NEEDS_OCR (parked, deferred
engine); unreadable → REJECTED (terminal). A human correction flips the `InvoiceField` to
CORRECTED (source_engine="human"), records an `InvoiceCorrection`, overwrites the canonical
scalar. On CONFIRMED the canonical scalars are frozen (Challan snapshot rule). Every CONFIRMED
invoice is a **gold candidate** (a promote-to-gold job writes source.pdf + expected.json to
the gold set — corrections continuously harden the eval set).

## Routes — `expense/routes.py` (mounted `/api/v1`, `MODULE_KEY="expense_invoice"`)
`_require_module(user)` gates every route on `can_access_module`. `POST /expense/invoices`
(multipart, bulk N files) → creates a batch, stores each PDF, extracts, returns per-file
outcome (incl. 409-duplicate carrying the existing id). `GET /expense/invoices` (searchable
register + filters + `.csv`, CSV-injection-guarded). `GET /expense/invoices/{id}` (canonical +
fields + lines). `GET/PATCH /expense/invoices/{id}/reviews` (per-field corrections; module-
gated, audited). `DELETE /expense/invoices/{id}` (audited + confirm; deleting a **CONFIRMED**
invoice requires `expense.delete` ∈ ADMIN_ONLY — the audit will finalise the exact gate).

## RBAC / audit
Ordinary review/register/corrections → `can_access_module("expense_invoice")` only (like
challan decisions). ADMIN_ONLY: `expense.delete` (delete a CONFIRMED record). Every mutation
`audit.log(...)`.

## Frontend — `frontend/src/pages/expense/`
`ExpenseModule.tsx` (shell + Tabs, `BASE='/m/expense_invoice'`), `Upload.tsx` (bulk
`<input type=file accept=application/pdf multiple>` via `postForm`; the 409-duplicate →
delete-and-re-upload `ConfirmDialog`), `Register.tsx` (searchable Table + CSV), `ReviewPanel.tsx`
(per-invoice low-confidence field correction, mirror `challan/ReviewPanel.tsx`). `api/expense.ts`
typed hooks; `/m/expense_invoice/*` route in `App.tsx`; nav auto-lists from `GET /modules`.

## DEFERRED (do NOT build now)
Standalone product-agnostic `/extract` HTTP service · `DocumentAIEngine`/swappable paid engine ·
the OCR/scanned-image path (NEEDS_OCR is detected + parked) · perceptual/image hashing ·
multi-doc-type generalization (`doc_type` param exists, only `gst_invoice` implemented) ·
learned confidence calibrator · queue-backed worker (in-process background + polling for v1) ·
multi-invoice single PDF · buyer-GSTIN validation (F3).

## Build plan (waves)
- **Wave 0 (me):** this doc + `canonical.py` + `models.py` + migration + `main.py`/alembic
  wiring + `pdfplumber` in requirements. Gate: import + `alembic check`.
- **Wave 1 (4 parallel agents):** extractor · eval+fixtures · service+routes+BE-tests · frontend.
- **Wave 2 (me):** integrate + FULL gate.
- **Wave 3 (3 parallel agents):** DUAL audit (extraction-correctness · money/data-integrity) +
  UI/UX audit.
- **Wave 4 (me):** fix findings + E2E (upload→extract→review→confirm) + commit + docs/memory.
