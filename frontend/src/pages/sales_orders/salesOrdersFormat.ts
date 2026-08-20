/** Shared constants + formatters for the Sales Orders module screens. */

/** Route base for the Sales Orders module (matches the `sales_orders` module key). */
export const SALES_ORDERS_BASE = '/m/sales_orders';

/** Format integer paise as an Indian-Rupee string, e.g. 20000000 -> "₹2,00,000.00". */
export function rupees(paise: number): string {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 2,
  }).format(paise / 100);
}
