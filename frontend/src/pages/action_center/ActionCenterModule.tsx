import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  Badge,
  Card,
  ErrorState,
  Loading,
  PageHeader,
  StatePanel,
  Table,
  THead,
  Th,
  Tr,
  Td,
} from '../../ui';
import {
  useActionCenter,
  DEFAULT_HORIZON_DAYS,
  type ProcurementItem,
  type InvoicingDueItem,
  type ArOverdueItem,
} from '../../api/actionCenter';

const SALES_ORDERS_BASE = '/m/sales_orders';
const RECEIVABLES_BASE = '/m/billing/receivables';

/** Format integer PAISE as an Indian-Rupee string, e.g. 20000000 -> "₹2,00,000.00". */
function rupees(paise: number): string {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(paise / 100);
}

/**
 * Format a plain YYYY-MM-DD as DD/MM/YYYY by slicing the parts directly — never
 * `new Date(iso)`, whose UTC parse can shift the day in timezones behind UTC.
 */
function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

/** Human "in N days" / "N days ago" / "today" for a signed day offset. */
function relativeDays(daysUntil: number): string {
  if (daysUntil === 0) return 'today';
  if (daysUntil > 0) return `in ${daysUntil} ${daysUntil === 1 ? 'day' : 'days'}`;
  const ago = Math.abs(daysUntil);
  return `${ago} ${ago === 1 ? 'day' : 'days'} ago`;
}

/** Empty placeholder + spacing shared by every section. */
function Empty() {
  return <StatePanel title="Nothing needs attention here">You are all caught up.</StatePanel>;
}

// --- count tiles --------------------------------------------------------------

function CountTile({ label, value, hint }: { label: string; value: number; hint: string }) {
  return (
    <Card className="p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-900">{value}</p>
      <p className="mt-0.5 text-xs text-slate-400">{hint}</p>
    </Card>
  );
}

// --- section shell ------------------------------------------------------------

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mb-8">
      <h2 className="mb-2 text-sm font-semibold text-slate-900">{title}</h2>
      {children}
    </section>
  );
}

/** Brand-coloured deep link cell used by every row's identifier column. */
function LinkCell({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link to={to} className="font-medium text-brand-600 hover:text-brand-700">
      {children}
    </Link>
  );
}

// --- section tables -----------------------------------------------------------

function ProcurementTable({ items }: { items: ProcurementItem[] }) {
  if (items.length === 0) return <Empty />;
  return (
    <Table>
      <THead>
        <Tr>
          <Th>PO</Th>
          <Th>Client</Th>
          <Th>Project</Th>
          <Th>Expected</Th>
          <Th>When</Th>
        </Tr>
      </THead>
      <tbody>
        {items.map((r) => {
          const past = r.days_until < 0;
          return (
            <Tr key={r.po_id}>
              <Td>
                <LinkCell to={`${SALES_ORDERS_BASE}/${r.po_id}`}>{r.po_number}</LinkCell>
              </Td>
              <Td className="text-slate-700">{r.client_name ?? '—'}</Td>
              <Td className="text-slate-700">{r.project_code ?? '—'}</Td>
              <Td className="whitespace-nowrap tabular-nums">
                {formatDate(r.expected_procurement_date)}
              </Td>
              <Td className="whitespace-nowrap">
                <span className={past ? 'font-medium text-rose-600' : 'font-medium text-amber-700'}>
                  {relativeDays(r.days_until)}
                </span>
              </Td>
            </Tr>
          );
        })}
      </tbody>
    </Table>
  );
}

function InvoicingDueTable({ items }: { items: InvoicingDueItem[] }) {
  if (items.length === 0) return <Empty />;
  return (
    <Table>
      <THead>
        <Tr>
          <Th>PO</Th>
          <Th>Client</Th>
          <Th>Project</Th>
          <Th className="text-right">Uninvoiced qty</Th>
          <Th className="text-right">Uninvoiced value</Th>
        </Tr>
      </THead>
      <tbody>
        {items.map((r) => (
          <Tr key={r.po_id}>
            <Td>
              <LinkCell to={`${SALES_ORDERS_BASE}/${r.po_id}`}>{r.po_number}</LinkCell>
            </Td>
            <Td className="text-slate-700">{r.client_name ?? '—'}</Td>
            <Td className="text-slate-700">{r.project_code ?? '—'}</Td>
            <Td className="text-right tabular-nums">{r.uninvoiced_qty}</Td>
            <Td className="text-right tabular-nums font-medium text-slate-900">
              {rupees(r.uninvoiced_value_paise)}
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

function ArOverdueTable({ items }: { items: ArOverdueItem[] }) {
  if (items.length === 0) return <Empty />;
  return (
    <Table>
      <THead>
        <Tr>
          <Th>Invoice</Th>
          <Th>Client</Th>
          <Th className="text-right">Outstanding</Th>
          <Th>Due</Th>
          <Th className="text-right">Days overdue</Th>
          <Th>Aging</Th>
        </Tr>
      </THead>
      <tbody>
        {items.map((r) => (
          <Tr key={r.invoice_id}>
            <Td>
              <LinkCell to={`${RECEIVABLES_BASE}/${r.invoice_id}`}>{r.invoice_number}</LinkCell>
            </Td>
            <Td className="text-slate-700">{r.client_name ?? '—'}</Td>
            <Td className="text-right tabular-nums font-medium text-slate-900">
              {rupees(r.outstanding_paise)}
            </Td>
            <Td className="whitespace-nowrap tabular-nums">
              <span className="font-medium text-rose-600">{formatDate(r.due_date)}</span>
            </Td>
            <Td className="text-right tabular-nums">{r.days_overdue}</Td>
            <Td>
              <Badge tone="red">{r.aging_bucket}</Badge>
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

/**
 * Action Center — a "what needs attention" dashboard. Three count tiles
 * (procurement follow-ups / invoicing due / overdue receivables) over three
 * sections deep-linking each item to its PO or AR detail. VIEW-gated by the
 * `/m/action_center` route; money is integer paise → rendered with `rupees()`.
 */
export function ActionCenterModule() {
  const query = useActionCenter(DEFAULT_HORIZON_DAYS);

  return (
    <div>
      <PageHeader
        title="Action Center"
        subtitle="What needs attention: procurement follow-ups, invoicing due, and overdue receivables."
      />

      {query.isPending ? (
        <Loading label="Loading action center…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <>
          <div className="mb-6 grid grid-cols-1 gap-3 sm:grid-cols-3">
            <CountTile
              label="Procurement follow-ups"
              value={query.data.counts.procurement}
              hint={`Due within ${DEFAULT_HORIZON_DAYS} days`}
            />
            <CountTile
              label="Invoicing due"
              value={query.data.counts.invoicing_due}
              hint="Delivered but not yet invoiced"
            />
            <CountTile
              label="Overdue receivables"
              value={query.data.counts.ar_overdue}
              hint="Past their due date"
            />
          </div>

          <Section title="Procurement follow-ups">
            <ProcurementTable items={query.data.procurement} />
          </Section>

          <Section title="Invoicing due">
            <InvoicingDueTable items={query.data.invoicing_due} />
          </Section>

          <Section title="Overdue receivables">
            <ArOverdueTable items={query.data.ar_overdue} />
          </Section>
        </>
      )}
    </div>
  );
}
