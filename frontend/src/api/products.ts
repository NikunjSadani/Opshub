import { useCallback } from 'react';
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi, ApiError } from './client';

/**
 * Typed contracts + React Query hooks for the Product Master (module key
 * `sales_orders`). Mirrors the backend `/products` routes (ProductOut). All paths
 * are relative to `/api/v1` (added by `useApi()`).
 *
 * Ids are treated as strings (mirroring api/projects.ts): they only ever flow
 * through `<select>`/query-string/equality contexts, never arithmetic, so a string
 * is the safe, UI-correct representation regardless of the backend's id kind.
 */

export interface Product {
  id: string;
  /** Optional human/import code (unique when present). */
  code: string | null;
  name: string;
  brand: string | null;
  model_number: string | null;
  category: string | null;
  /** Unit of measure, e.g. "PCS". */
  uom: string;
  /** HSN/SAC tax code. */
  hsn: string | null;
  /** GST rate percent as a string, e.g. "18.00" (backend `Decimal`, serialized like the
   * PO line / project-products `tax_rate`). null when unset. */
  gst_rate: string | null;
  active: boolean;
  /** ISO datetime string (backend `datetime`). */
  created_at: string;
}

/** Body for creating a product — name + uom required, the rest optional. */
export interface ProductInput {
  code?: string;
  name: string;
  brand?: string;
  model_number?: string;
  category?: string;
  uom: string;
  hsn?: string;
  /** GST % 0..100 as a string (mirrors `tax_rate`); omit to leave unset. */
  gst_rate?: string;
}

/** Body for editing a product — every field optional, plus the active toggle. */
export interface ProductUpdate {
  code?: string;
  name?: string;
  brand?: string;
  model_number?: string;
  category?: string;
  uom?: string;
  hsn?: string;
  /** GST % 0..100 as a string; send `null` to clear it (an omitted key is left untouched). */
  gst_rate?: string | null;
  active?: boolean;
}

export interface ProductFilters {
  /** Free-text search over code / name / brand / model. */
  q?: string;
  category?: string;
  /** Tri-state active filter: '' = any, 'true' = active only, 'false' = inactive only. */
  active?: 'true' | 'false' | '';
}

/**
 * Build a `?q=&category=&active=` query string for the products list, appending
 * only non-empty (trimmed) params. Exported + pure so it can be unit-tested
 * without a hook.
 */
export function buildProductsQuery(filters: ProductFilters): string {
  const params = new URLSearchParams();
  if (filters.q?.trim()) params.set('q', filters.q.trim());
  if (filters.category?.trim()) params.set('category', filters.category.trim());
  if (filters.active === 'true' || filters.active === 'false') params.set('active', filters.active);
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

// --------------------------------------------------------------- query keys
export const productKeys = {
  list: (filters: ProductFilters) => ['products', 'list', filters] as const,
  detail: (id: string) => ['products', 'detail', id] as const,
};

// ------------------------------------------------------------------ queries

/** Products, filtered by free-text / category / active. */
export function useProductsQuery(filters: ProductFilters): UseQueryResult<Product[], Error> {
  const { get } = useApi();
  return useQuery<Product[], Error>({
    queryKey: productKeys.list(filters),
    queryFn: ({ signal }) => get<Product[]>(`/products${buildProductsQuery(filters)}`, signal),
  });
}

/** A single product by id. */
export function useProductQuery(id: string | null): UseQueryResult<Product, Error> {
  const { get } = useApi();
  return useQuery<Product, Error>({
    queryKey: productKeys.detail(id ?? ''),
    enabled: id != null,
    queryFn: ({ signal }) => get<Product>(`/products/${id}`, signal),
  });
}

// ---------------------------------------------------------------- mutations

/** Create a product (MANAGE). Invalidates the product list on success. */
export function useCreateProduct(): UseMutationResult<Product, ApiError, ProductInput> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<Product, ApiError, ProductInput>({
    mutationFn: (body) => post<Product>('/products', body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['products', 'list'] });
    },
  });
}

/** Edit a product (MANAGE). Refreshes the list + that product's detail. */
export function useUpdateProduct(): UseMutationResult<
  Product,
  ApiError,
  { id: string } & ProductUpdate
> {
  const { patch } = useApi();
  const qc = useQueryClient();
  return useMutation<Product, ApiError, { id: string } & ProductUpdate>({
    mutationFn: ({ id, ...body }) => patch<Product>(`/products/${id}`, body),
    onSuccess: (product) => {
      qc.setQueryData(productKeys.detail(product.id), product);
      void qc.invalidateQueries({ queryKey: ['products', 'list'] });
    },
  });
}

// ----------------------------------------------------- HSN-master reconcile

/** One HSN whose master rate was corrected from the product data (old → new). */
export interface HsnSyncUpdated {
  hsn: string;
  old_rate: string;
  new_rate: string;
}

/** One HSN where active products disagree on the rate (a deterministic rate was still
 * applied; the operator should fix the underlying product data). */
export interface HsnSyncConflict {
  hsn: string;
  /** The distinct rates the active products disagree on, e.g. ["18", "12"]. */
  rates: string[];
  /** The active products carrying this HSN. */
  product_ids: number[];
}

/** Result of `POST /products/sync-hsn-master` (backend `ProductsHsnSyncOut`). */
export interface ProductsHsnSyncResult {
  /** HSN codes newly added to the challan HSN master. */
  created: string[];
  updated: HsnSyncUpdated[];
  conflicts: HsnSyncConflict[];
}

/**
 * Rebuild the challan HSN master (`md_hsn`) from every product carrying both an HSN and a
 * GST rate (MANAGE). Returns the created / rate-corrected / conflicting HSNs so the UI can
 * surface conflicts for the operator to fix. Takes no body.
 */
export function useSyncHsnMaster(): UseMutationResult<ProductsHsnSyncResult, ApiError, void> {
  const { post } = useApi();
  return useMutation<ProductsHsnSyncResult, ApiError, void>({
    mutationFn: () => post<ProductsHsnSyncResult>('/products/sync-hsn-master'),
  });
}

// --------------------------------------------------------- bulk .xlsx upload

/** One row-level error in a products bulk upload (unknown/malformed row). */
export interface ProductsBulkError {
  row: number;
  message: string;
}

/** The result of a products bulk .xlsx upload (backend `ProductsBulkOut`).
 * `created`/`updated` are product CODES. */
export interface ProductsBulkResult {
  created: string[];
  updated: string[];
  errors: ProductsBulkError[];
}

/** The auth-gated endpoint that serves the products bulk-upload .xlsx template (MANAGE). */
export const PRODUCTS_BULK_TEMPLATE_PATH = '/products/bulk-template.xlsx';
/** The filename the products template downloads as (fallback if the server omits Content-Disposition). */
export const PRODUCTS_BULK_TEMPLATE_FILENAME = 'products-bulk-template.xlsx';

/**
 * Download the products bulk-upload .xlsx template through the authed blob helper. The
 * endpoint is auth-gated (MANAGE), so a bare `<a href>` would 401 — this reuses
 * `downloadUrl`, which attaches the bearer token, streams the blob, and saves it with the
 * server's filename. Returns a callback the button can await.
 */
export function useDownloadProductsTemplate(): () => Promise<void> {
  const { downloadUrl } = useApi();
  return useCallback(async () => {
    await downloadUrl(PRODUCTS_BULK_TEMPLATE_PATH, PRODUCTS_BULK_TEMPLATE_FILENAME);
  }, [downloadUrl]);
}

/**
 * Bulk-create/update products from an .xlsx (MANAGE). Sends a multipart form with `file`.
 * Returns the created / updated (codes) + row-error breakdown. Invalidates the product
 * list on success.
 */
export function useBulkUploadProducts(): UseMutationResult<ProductsBulkResult, ApiError, File> {
  const { postForm } = useApi();
  const qc = useQueryClient();
  return useMutation<ProductsBulkResult, ApiError, File>({
    mutationFn: (file) => {
      const form = new FormData();
      form.append('file', file);
      return postForm<ProductsBulkResult>('/products/upload', form);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['products', 'list'] });
    },
  });
}
