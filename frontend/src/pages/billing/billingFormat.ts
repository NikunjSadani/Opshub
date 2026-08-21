/** Shared constants + formatters for the Billing & AR module. */
export const BILLING_BASE = '/m/billing';

/** Format integer paise as an Indian-Rupee string, e.g. 20000000 -> "₹2,00,000.00". */
export function rupees(paise: number): string {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency', currency: 'INR', maximumFractionDigits: 2,
  }).format(paise / 100);
}
