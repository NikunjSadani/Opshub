import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi, ApiError } from './client';
import { poKeys } from './purchaseOrders';

/**
 * Typed contracts + React Query hooks for a project's PRODUCT TEMPLATE — the
 * per-project curation of catalogue products PLUS a per-project pricing template that
 * pre-fills a New PO line. Mirrors the backend `/project-products` routes. All paths
 * are relative to `/api/v1` (added by `useApi()`).
 *
 * The tagged list is `ProjectProductOut` — the product identity (keyed by
 * `product_id`, NOT `id`) plus the optional per-project template pricing. MONEY is
 * integer PAISE on the wire; `tax_rate` is a percent STRING; every template field is
 * nullable so a template can be saved partially.
 *
 * `sell_price_paise` and `freight_paise` are the ADMIN-ONLY actuals: the backend
 * returns them `null` for non-admins and IGNORES non-admin attempts to set them, so
 * the UI must only show/collect those two behind the same admin gate `POForm` uses
 * (`perms.hasPlatform('iam')`).
 *
 * Reads need `sales_orders` VIEW; tag/untag/patch need OPERATE (backend gates
 * `product.tag`) — all server-enforced.
 */

// ------------------------------------------------------------------- DTOs

/** A product tagged to a project + its (nullable) per-project pricing template. */
export interface ProjectProduct {
  // --- product identity (keyed by product_id, not id) ---
  product_id: number;
  code: string | null;
  name: string;
  brand: string | null;
  model_number: string | null;
  category: string | null;
  active: boolean;
  /** The Product master's own UOM (always present, e.g. "PCS") — the PO pre-fill's
   * fallback when the template `uom` override below is null. */
  product_uom: string;
  // --- per-project template pricing (all nullable) ---
  description: string | null;
  /** Per-project UOM override; falls back to `product_uom` on a PO pre-fill. */
  uom: string | null;
  /** Our CP. */
  cost_price_paise: number | null;
  original_cost_price_paise: number | null;
  client_sell_price_paise: number | null;
  vendor_sell_price_paise: number | null;
  /** Actual sell — ADMIN-ONLY (null for non-admins). */
  sell_price_paise: number | null;
  client_freight_paise: number | null;
  vendor_freight_paise: number | null;
  /** Actual freight — ADMIN-ONLY (null for non-admins). */
  freight_paise: number | null;
  packaging_paise: number | null;
  handling_paise: number | null;
  other_paise: number | null;
  /** Percent as a string, e.g. "18.00". */
  tax_rate: string | null;
}

/**
 * A partial pricing body for PATCH. Money keys are integer PAISE; `tax_rate` a string;
 * `description`/`uom` strings. Omit keys you don't set — the backend leaves unmentioned
 * fields untouched. `sell_price_paise`/`freight_paise` are admin-only (send only for an
 * admin; the backend ignores them from a non-admin).
 */
export interface ProjectProductPricing {
  description?: string | null;
  uom?: string | null;
  cost_price_paise?: number | null;
  original_cost_price_paise?: number | null;
  client_sell_price_paise?: number | null;
  vendor_sell_price_paise?: number | null;
  sell_price_paise?: number | null;
  client_freight_paise?: number | null;
  vendor_freight_paise?: number | null;
  freight_paise?: number | null;
  packaging_paise?: number | null;
  handling_paise?: number | null;
  other_paise?: number | null;
  tax_rate?: string | null;
}

// --------------------------------------------------------------- query keys
export const projectProductKeys = {
  list: (projectId: string) => ['project-products', projectId] as const,
};

// ------------------------------------------------------------------ queries

/** The products tagged to a project (incl. inactive) + their template pricing.
 * Disabled until a project is set. */
export function useProjectProductsQuery(
  projectId: string | null,
): UseQueryResult<ProjectProduct[], Error> {
  const { get } = useApi();
  return useQuery<ProjectProduct[], Error>({
    queryKey: projectProductKeys.list(projectId ?? ''),
    enabled: projectId != null,
    queryFn: ({ signal }) =>
      get<ProjectProduct[]>(
        `/project-products?project_id=${encodeURIComponent(projectId ?? '')}`,
        signal,
      ),
  });
}

// ---------------------------------------------------------------- mutations

/** Tag a product to this project (OPERATE) — tag only, no pricing (an idempotent tag
 * that does NOT overwrite an already-tagged product's fields). Invalidates the list. */
export function useTagProduct(
  projectId: string,
): UseMutationResult<ProjectProduct, ApiError, string> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<ProjectProduct, ApiError, string>({
    mutationFn: (productId) =>
      post<ProjectProduct>('/project-products', {
        project_id: Number(projectId),
        product_id: Number(productId),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: projectProductKeys.list(projectId) });
      // Also refresh the PO product picker so an already-open New PO form reflects the
      // changed tags (the curated default reads from this cache).
      void qc.invalidateQueries({ queryKey: poKeys.products });
    },
  });
}

/** Update a tagged product's per-project pricing template (OPERATE). Sends only the
 * provided (partial) fields. Invalidates the list + the PO product picker. */
export function useUpdateProjectProduct(
  projectId: string,
): UseMutationResult<
  ProjectProduct,
  ApiError,
  { productId: string; body: ProjectProductPricing }
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<ProjectProduct, ApiError, { productId: string; body: ProjectProductPricing }>({
    mutationFn: ({ productId, body }) =>
      patch<ProjectProduct>(`/project-products/${projectId}/${productId}`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: projectProductKeys.list(projectId) });
      void qc.invalidateQueries({ queryKey: poKeys.products });
    },
  });
}

/** Untag a product from this project (OPERATE). Invalidates the project's tagged list. */
export function useUntagProduct(
  projectId: string,
): UseMutationResult<{ deleted: boolean }, ApiError, string> {
  const { del } = useApi();
  const qc = useQueryClient();
  return useMutation<{ deleted: boolean }, ApiError, string>({
    mutationFn: (productId) =>
      del<{ deleted: boolean }>(`/project-products/${projectId}/${productId}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: projectProductKeys.list(projectId) });
      // Also refresh the PO product picker so an already-open New PO form reflects the
      // changed tags (the curated default reads from this cache).
      void qc.invalidateQueries({ queryKey: poKeys.products });
    },
  });
}
