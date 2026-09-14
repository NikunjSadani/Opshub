import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useApi, ApiError } from './client';
import type { Product } from './products';
import { poKeys } from './purchaseOrders';

/**
 * Typed contracts + React Query hooks for tagging products to a project
 * (money-free curation). Mirrors the backend `/project-products` routes. All
 * paths are relative to `/api/v1` (added by `useApi()`).
 *
 * Ids are treated as strings on the FE (mirroring api/projects.ts + api/products.ts):
 * they only ever flow through query strings / equality checks, never arithmetic. The
 * two mutation bodies coerce to Number() only at the wire, matching the backend
 * contract (`{ project_id: number, product_id: number }`).
 *
 * The tagged list is the full ProductOut projection (reused as `Product`), so the UI
 * can show category + the active flag (an inactive-but-still-tagged product renders
 * an "Inactive" badge). Reads need `sales_orders` VIEW; both mutations need OPERATE
 * (backend gates `product.tag`) — all server-enforced.
 */

// --------------------------------------------------------------- query keys
export const projectProductKeys = {
  list: (projectId: string) => ['project-products', projectId] as const,
};

// ------------------------------------------------------------------ queries

/** The products tagged to a project (incl. inactive). Disabled until a project is set. */
export function useProjectProductsQuery(
  projectId: string | null,
): UseQueryResult<Product[], Error> {
  const { get } = useApi();
  return useQuery<Product[], Error>({
    queryKey: projectProductKeys.list(projectId ?? ''),
    enabled: projectId != null,
    queryFn: ({ signal }) =>
      get<Product[]>(`/project-products?project_id=${encodeURIComponent(projectId ?? '')}`, signal),
  });
}

// ---------------------------------------------------------------- mutations

/** Tag a product to this project (OPERATE). Invalidates the project's tagged list. */
export function useTagProduct(projectId: string): UseMutationResult<Product, ApiError, string> {
  const { post } = useApi();
  const qc = useQueryClient();
  return useMutation<Product, ApiError, string>({
    mutationFn: (productId) =>
      post<Product>('/project-products', {
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
