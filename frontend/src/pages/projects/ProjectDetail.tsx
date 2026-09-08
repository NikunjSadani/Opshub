import { type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  Badge,
  ErrorState,
  Loading,
  PageHeader,
  StatePanel,
  Table,
  Td,
  THead,
  Th,
  Tr,
} from '../../ui';
import { useProjectQuery } from '../../api/projects';
import {
  PO_STATUS_LABEL,
  PO_STATUS_TONE,
  usePurchaseOrdersQuery,
  type POStatus,
} from '../../api/purchaseOrders';
import { SALES_ORDERS_BASE, rupees } from '../sales_orders/salesOrdersFormat';
import { formatDate, PROJECT_STATUS_LABEL, PROJECT_STATUS_TONE } from './projectsFormat';

const PROJECTS_BASE = '/m/projects';

/** A label/value pair on the header summary grid (mirrors PODetail's DefItem). */
function DefItem({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-800">{children}</dd>
    </div>
  );
}

/** A displayable PO title for the table — the number, or a placeholder when unset. */
function poNumberLabel(poNumber: string | null): string {
  return poNumber ? poNumber : '— (no number)';
}

/**
 * Project detail — the project's header (code / name / client / status / dates /
 * description) plus a drill-through table of its purchase orders. Reached from the
 * Projects register; reads need projects VIEW (server-enforced). Each PO row links
 * out to the PO detail in the Sales Orders module.
 */
export function ProjectDetail() {
  const params = useParams();
  const id = params.id ?? null;

  const projectQuery = useProjectQuery(id);

  const backLink = (
    <Link to={PROJECTS_BASE} className="text-sm font-medium text-brand-600 hover:text-brand-700">
      ← Back to projects
    </Link>
  );

  if (id == null) {
    return (
      <div>
        <div className="mb-4">{backLink}</div>
        <StatePanel tone="red" title="Invalid project">
          That project link is not valid.
        </StatePanel>
      </div>
    );
  }

  if (projectQuery.isPending) return <Loading label="Loading project…" />;
  if (projectQuery.isError || !projectQuery.data) {
    return (
      <div>
        <div className="mb-4">{backLink}</div>
        <ErrorState error={projectQuery.error} onRetry={() => void projectQuery.refetch()} />
      </div>
    );
  }

  const project = projectQuery.data;

  return (
    <div>
      <div className="mb-4">{backLink}</div>

      <PageHeader
        title={project.code}
        subtitle={project.name}
        actions={
          <Badge tone={PROJECT_STATUS_TONE[project.status]}>
            {PROJECT_STATUS_LABEL[project.status]}
          </Badge>
        }
      />

      <div className="mb-6 rounded-xl border border-slate-200 bg-white p-4">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          <DefItem label="Status">
            <Badge tone={PROJECT_STATUS_TONE[project.status]}>
              {PROJECT_STATUS_LABEL[project.status]}
            </Badge>
          </DefItem>
          <DefItem label="Client">
            <span className="font-mono text-slate-900">{project.client_code}</span>
            <span className="text-slate-400"> — </span>
            <span>{project.client_name}</span>
          </DefItem>
          <DefItem label="Start date">{formatDate(project.start_date)}</DefItem>
          <DefItem label="Created">{formatDate(project.created_at)}</DefItem>
        </dl>
        {project.description && (
          <div className="mt-4 border-t border-slate-100 pt-3">
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-400">
              Description
            </dt>
            <dd className="mt-0.5 whitespace-pre-wrap text-sm text-slate-700">
              {project.description}
            </dd>
          </div>
        )}
      </div>

      <h2 className="mb-2 text-sm font-semibold text-slate-900">Purchase orders</h2>
      <PurchaseOrdersSection projectId={id} />
    </div>
  );
}

/** Backend's max page size — a per-project drill-through requests the whole bounded set
 * (a project's POs), not the register's default first page, so nothing is silently hidden. */
const PROJECT_POS_LIMIT = 200;

/** The project's PO drill-through: the register columns, scoped to this project. */
function PurchaseOrdersSection({ projectId }: { projectId: string }) {
  const query = usePurchaseOrdersQuery({ project_id: projectId, limit: PROJECT_POS_LIMIT });

  if (query.isPending) return <Loading label="Loading purchase orders…" />;
  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }

  const rows = query.data ?? [];
  if (rows.length === 0) {
    return <StatePanel title="No purchase orders">No purchase orders for this project yet.</StatePanel>;
  }

  return (
    <>
      <Table>
        <THead>
          <Tr>
            <Th>PO number</Th>
            <Th>PO date</Th>
            <Th>Status</Th>
            <Th className="text-right">Lines</Th>
            <Th className="text-right">Order value</Th>
            <Th className="text-right">Actions</Th>
          </Tr>
        </THead>
        <tbody>
          {rows.map((po) => (
            <Tr key={po.id}>
              <Td className="font-medium text-slate-900">{poNumberLabel(po.po_number)}</Td>
              <Td className="whitespace-nowrap">{formatDate(po.po_date)}</Td>
              <Td>
                <Badge tone={PO_STATUS_TONE[po.status as POStatus]}>
                  {PO_STATUS_LABEL[po.status as POStatus] ?? po.status}
                </Badge>
              </Td>
              <Td className="text-right tabular-nums">{po.line_count}</Td>
              {/* Client-facing order value (entire client billing — goods + freight +
                  packaging/handling/other — plus agency fee). Visible to all; no admin margin. */}
              <Td className="text-right tabular-nums">{rupees(po.total_with_agency_paise)}</Td>
              <Td>
                <div className="flex justify-end">
                  <Link
                    to={`${SALES_ORDERS_BASE}/${po.id}`}
                    className="text-sm font-medium text-brand-600 hover:text-brand-700"
                  >
                    View
                  </Link>
                </div>
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
      <p className="mt-4 text-sm text-slate-500">
        Showing {rows.length}
        {rows.length >= PROJECT_POS_LIMIT && ` (first ${PROJECT_POS_LIMIT} — narrow via the register)`}
      </p>
    </>
  );
}
