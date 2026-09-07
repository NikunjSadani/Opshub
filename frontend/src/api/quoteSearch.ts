import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { useApi } from './client';

/**
 * Typed contracts + React Query hooks for the Quote / price-book search.
 *
 * Backend module key `sales_orders` (VIEW-gated). All paths are relative to
 * `/api/v1` (added by `useApi()`). Money is integer PAISE on the wire; the budget
 * FILTER inputs are captured in ₹ (rupees) and converted to paise in the query
 * builder (mirrors `buildProjectsQuery`, appending only non-empty params).
 *
 * Ids are treated as strings: they only flow through `<select>` values, query
 * strings, and equality checks — never arithmetic.
 */

/** One priced PO line, as returned by `GET /quote-search` (most-recent-first). */
export interface QuoteRow {
  po_line_item_id: string;
  product_id: string;
  product_name: string;
  brand: string | null;
  model_number: string | null;
  category: string | null;
  uom: string;
  po_number: string;
  client_id: string;
  client_name: string;
  project_code: string;
  /** ISO date string (backend `date`). */
  po_date: string;
  /** Decimal quantity as a string (never coerced to float). */
  ordered_qty: string;
  cost_price_paise: number;
  /** The VISIBLE client-quoted price (returned to everyone). */
  client_sell_price_paise: number;
  /** ADMIN-ONLY actual sell — `null` for non-IAM users. */
  sell_price_paise: number | null;
  /** Margin over the actual sell — ADMIN-ONLY, `null` for non-IAM users or when uncomputable. */
  margin_pct: number | null;
  /** The VISIBLE client-quoted freight (returned to everyone). */
  client_freight_paise: number;
  /** ADMIN-ONLY actual freight — `null` for non-IAM users. */
  freight_paise: number | null;
  packaging_paise: number;
  handling_paise: number;
  other_paise: number;
  /** Tax rate as a string, e.g. "18.00". */
  tax_rate: string;
}

/** One point on a product's price history, from `GET /quote-search/trend` (oldest-first). */
export interface TrendPoint {
  po_date: string;
  po_number: string;
  client_name: string;
  ordered_qty: string;
  cost_price_paise: number;
  /** The VISIBLE client-quoted price (returned to everyone). */
  client_sell_price_paise: number;
  /** ADMIN-ONLY actual sell — `null` for non-IAM users. */
  sell_price_paise: number | null;
}

/**
 * The quote-search filter state. `q`/`category` are free text; `client_id` comes
 * from the client picker; the dates are YYYY-MM-DD; the budgets are typed in
 * RUPEES (converted to paise on the wire).
 */
export interface QuoteFilters {
  q?: string;
  category?: string;
  client_id?: string;
  date_from?: string;
  date_to?: string;
  /** Minimum CLIENT-quoted price, in RUPEES (converted to `budget_min_paise`). */
  budget_min?: string;
  /** Maximum CLIENT-quoted price, in RUPEES (converted to `budget_max_paise`). */
  budget_max?: string;
}

/** Convert a typed rupee string to integer paise, or null when it isn't a number. */
function rupeesToPaise(input: string | undefined): number | null {
  if (!input?.trim()) return null;
  const n = Number(input.replace(/[₹,\s]/g, ''));
  if (!Number.isFinite(n)) return null;
  return Math.round(n * 100);
}

/**
 * Build a `?q=&category=&client_id=&date_from=&date_to=&budget_min_paise=&budget_max_paise=`
 * query string, appending only non-empty params (budget ₹→paise). Exported + pure
 * so it can be unit-tested without a hook.
 */
export function buildQuoteSearchQuery(filters: QuoteFilters): string {
  const params = new URLSearchParams();
  if (filters.q?.trim()) params.set('q', filters.q.trim());
  if (filters.category?.trim()) params.set('category', filters.category.trim());
  if (filters.client_id?.trim()) params.set('client_id', filters.client_id.trim());
  if (filters.date_from?.trim()) params.set('date_from', filters.date_from.trim());
  if (filters.date_to?.trim()) params.set('date_to', filters.date_to.trim());
  const min = rupeesToPaise(filters.budget_min);
  if (min != null) params.set('budget_min_paise', String(min));
  const max = rupeesToPaise(filters.budget_max);
  if (max != null) params.set('budget_max_paise', String(max));
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

// --------------------------------------------------------------- query keys
export const quoteSearchKeys = {
  search: (filters: QuoteFilters) => ['quote-search', 'search', filters] as const,
  trend: (productId: string) => ['quote-search', 'trend', productId] as const,
};

// ------------------------------------------------------------------ queries

/** Priced PO lines matching the filters (most-recent-first; CANCELLED POs excluded). */
export function useQuoteSearch(filters: QuoteFilters): UseQueryResult<QuoteRow[], Error> {
  const { get } = useApi();
  return useQuery<QuoteRow[], Error>({
    queryKey: quoteSearchKeys.search(filters),
    queryFn: ({ signal }) =>
      get<QuoteRow[]>(`/quote-search${buildQuoteSearchQuery(filters)}`, signal),
  });
}

/** A single product's price history over time (oldest-first). Idle until a product is picked. */
export function usePriceTrend(productId: string | null): UseQueryResult<TrendPoint[], Error> {
  const { get } = useApi();
  return useQuery<TrendPoint[], Error>({
    queryKey: quoteSearchKeys.trend(productId ?? ''),
    enabled: productId != null,
    queryFn: ({ signal }) =>
      get<TrendPoint[]>(`/quote-search/trend?product_id=${encodeURIComponent(productId ?? '')}`, signal),
  });
}
