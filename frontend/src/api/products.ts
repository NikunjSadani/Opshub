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
