import { useEffect, useState } from 'react';
import {
  Badge,
  Button,
  ErrorState,
  Loading,
  Modal,
  PageHeader,
  SelectField,
  StatePanel,
  Table,
  TextArea,
  TextField,
  THead,
  Th,
  Tr,
  Td,
  useToast,
} from '../../ui';
import { useAuth } from '../../auth/AuthProvider';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import {
  PROJECT_STATUSES,
  useClientsQuery,
  useCreateProject,
  useProjectsQuery,
  useUpdateProjectStatus,
  type Client,
  type Project,
  type ProjectFilters,
  type ProjectStatus,
} from '../../api/projects';
import {
  errorMessage,
  formatDate,
  PROJECT_STATUS_LABEL,
  PROJECT_STATUS_TONE,
} from './projectsFormat';

/** Inline status control (ADMIN): change a project's status from the register. */
function StatusControl({ project }: { project: Project }) {
  const toast = useToast();
  const updateStatus = useUpdateProjectStatus();

  function onChange(status: ProjectStatus) {
    if (status === project.status) return;
    updateStatus.mutate(
      { id: project.id, status },
      {
        onSuccess: (updated) =>
          toast.success(`${updated.code} set to ${PROJECT_STATUS_LABEL[updated.status]}.`),
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  return (
    <select
      aria-label={`Status for ${project.code}`}
      value={project.status}
      disabled={updateStatus.isPending}
      onChange={(e) => onChange(e.target.value as ProjectStatus)}
      className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/40 disabled:opacity-50"
    >
      {PROJECT_STATUSES.map((s) => (
        <option key={s} value={s}>
          {PROJECT_STATUS_LABEL[s]}
        </option>
      ))}
    </select>
  );
}

/** New-project modal. Client + name required; start date + description optional. */
function NewProjectModal({
  open,
  clients,
  onClose,
}: {
  open: boolean;
  clients: Client[];
  onClose: () => void;
}) {
  const toast = useToast();
  const createProject = useCreateProject();

  const [clientId, setClientId] = useState('');
  const [name, setName] = useState('');
  const [startDate, setStartDate] = useState('');
  const [description, setDescription] = useState('');

  const trimmedName = name.trim();
  const canSubmit = clientId !== '' && trimmedName.length > 0 && !createProject.isPending;

  function close() {
    if (createProject.isPending) return;
    onClose();
  }

  function submit() {
    if (!canSubmit) return;
    createProject.mutate(
      {
        client_id: clientId,
        name: trimmedName,
        start_date: startDate || undefined,
        description: description.trim() || undefined,
      },
      {
        onSuccess: (project) => {
          toast.success(`Project ${project.code} created.`);
          onClose();
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  // Reset fields + mutation state whenever the modal opens.
  useEffect(() => {
    if (!open) return;
    setClientId('');
    setName('');
    setStartDate('');
    setDescription('');
    createProject.reset();
    // Only re-run on open transitions; createProject.reset is stable enough here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <Modal
      open={open}
      title="New project"
      onClose={close}
      busy={createProject.isPending}
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={createProject.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} loading={createProject.isPending} disabled={!canSubmit}>
            Create project
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <SelectField
          label="Client"
          required
          value={clientId}
          hint="The project code is generated as <CLIENT_CODE>-<number>."
          onChange={(e) => setClientId(e.target.value)}
        >
          <option value="">Select a client…</option>
          {clients.map((c) => (
            <option key={c.id} value={c.id}>
              {c.code} — {c.name}
            </option>
          ))}
        </SelectField>
        <TextField
          label="Name"
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Q3 Trade Scheme"
          maxLength={160}
        />
        <TextField
          label="Start date"
          type="date"
          value={startDate}
          onChange={(e) => setStartDate(e.target.value)}
        />
        <TextArea
          label="Description"
          rows={3}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Optional notes about this project."
          maxLength={2000}
        />
      </div>
    </Modal>
  );
}

/** The projects register: filters + a create action + the project table. */
export function ProjectsList() {
  const { user } = useAuth();
  const isAdmin = user?.role === 'ADMIN';

  const [clientId, setClientId] = useState('');
  const [status, setStatus] = useState<ProjectStatus | ''>('');
  const [q, setQ] = useState('');
  const [modalOpen, setModalOpen] = useState(false);

  const clientsQuery = useClientsQuery();
  const clients = clientsQuery.data ?? [];

  // Debounce the free-text search that feeds the query key (client/status are
  // selects, applied immediately) so each keystroke does not fire its own request.
  const filters: ProjectFilters = { client_id: clientId, status, q: useDebouncedValue(q) };
  const query = useProjectsQuery(filters);
  const rows = query.data ?? [];

  const hasClients = clients.length > 0;

  return (
    <div>
      <PageHeader
        title="Projects"
        subtitle="Projects across all clients, newest first."
        actions={
          <Button
            size="sm"
            onClick={() => setModalOpen(true)}
            disabled={!hasClients}
            title={hasClients ? undefined : 'Register a client first.'}
          >
            New project
          </Button>
        }
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <SelectField
          label="Client"
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
        >
          <option value="">All clients</option>
          {clients.map((c) => (
            <option key={c.id} value={c.id}>
              {c.code} — {c.name}
            </option>
          ))}
        </SelectField>
        <SelectField
          label="Status"
          value={status}
          onChange={(e) => setStatus(e.target.value as ProjectStatus | '')}
        >
          <option value="">All statuses</option>
          {PROJECT_STATUSES.map((s) => (
            <option key={s} value={s}>
              {PROJECT_STATUS_LABEL[s]}
            </option>
          ))}
        </SelectField>
        <TextField
          label="Search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Code or name…"
          maxLength={80}
        />
      </div>

      {query.isPending ? (
        <Loading label="Loading projects…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : rows.length === 0 ? (
        <StatePanel title="No projects found">
          {q.trim() || clientId || status
            ? 'No projects match these filters.'
            : hasClients
              ? 'Create a project to get started.'
              : 'Register a client first, then create a project under it.'}
        </StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>Code</Th>
              <Th>Name</Th>
              <Th>Client</Th>
              <Th>Start date</Th>
              <Th>Status</Th>
            </Tr>
          </THead>
          <tbody>
            {rows.map((p) => (
              <Tr key={p.id}>
                <Td className="font-mono font-medium text-slate-900">{p.code}</Td>
                <Td className="text-slate-900">{p.name}</Td>
                <Td>
                  <span className="font-mono text-slate-900">{p.client_code}</span>
                  <span className="text-slate-400"> — </span>
                  <span className="text-slate-600">{p.client_name}</span>
                </Td>
                <Td className="whitespace-nowrap">{formatDate(p.start_date)}</Td>
                <Td>
                  {isAdmin ? (
                    <StatusControl project={p} />
                  ) : (
                    <Badge tone={PROJECT_STATUS_TONE[p.status]}>
                      {PROJECT_STATUS_LABEL[p.status]}
                    </Badge>
                  )}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}

      <NewProjectModal open={modalOpen} clients={clients} onClose={() => setModalOpen(false)} />
    </div>
  );
}
