import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Badge,
  Button,
  ErrorState,
  Loading,
  Modal,
  PageHeader,
  SearchableSelect,
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
import { usePermissions } from '../../auth/AuthProvider';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import {
  PROJECT_STATUSES,
  useClientsQuery,
  useCreateProject,
  useProjectsQuery,
  useUpdateProject,
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
        <SearchableSelect
          label="Client"
          required
          value={clientId}
          hint="The project code is generated as <CLIENT_CODE>-<number>."
          onChange={setClientId}
          placeholder="Select a client…"
          options={clients.map((c) => ({ value: String(c.id), label: `${c.code} — ${c.name}` }))}
        />
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

/**
 * Edit-project modal (MANAGE): correct a project's typed details — name (required),
 * start date, description. CODE + CLIENT are system-assigned identity and are shown
 * read-only, never edited. Sends only editable fields; nullable ones are cleared by
 * sending an empty value.
 */
function EditProjectModal({
  project,
  onClose,
}: {
  project: Project | null;
  onClose: () => void;
}) {
  const toast = useToast();
  const updateProject = useUpdateProject();

  const [name, setName] = useState('');
  const [startDate, setStartDate] = useState('');
  const [description, setDescription] = useState('');

  const open = project !== null;
  const trimmedName = name.trim();
  const canSubmit = trimmedName.length > 0 && !updateProject.isPending;

  // Seed the fields from the project whenever the modal opens (or the row changes).
  useEffect(() => {
    if (!project) return;
    setName(project.name);
    setStartDate(project.start_date ?? '');
    setDescription(project.description ?? '');
    updateProject.reset();
    // Re-run only when the edited project changes; reset is stable enough here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project]);

  function close() {
    if (updateProject.isPending) return;
    onClose();
  }

  function submit() {
    if (!project || !canSubmit) return;
    updateProject.mutate(
      {
        id: project.id,
        // Nullable fields: send null (not undefined) so an emptied value CLEARS it.
        patch: {
          name: trimmedName,
          start_date: startDate || null,
          description: description.trim() || null,
        },
      },
      {
        onSuccess: (updated) => {
          toast.success(`Project ${updated.code} updated.`);
          onClose();
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  return (
    <Modal
      open={open}
      title="Edit project"
      onClose={close}
      busy={updateProject.isPending}
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={updateProject.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} loading={updateProject.isPending} disabled={!canSubmit}>
            Save changes
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <TextField
          label="Code"
          value={project?.code ?? ''}
          readOnly
          disabled
          hint="System-assigned and permanent — it can't be changed."
        />
        <TextField
          label="Client"
          value={project ? `${project.client_code} — ${project.client_name}` : ''}
          readOnly
          disabled
        />
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
  const perms = usePermissions();
  // Changing a project's status is a Manage action; creating a project is an
  // Operate (write) action. Gate each on the right level for the projects module.
  const canManage = perms.atLeast('projects', 'MANAGE');
  const canCreate = perms.atLeast('projects', 'OPERATE');

  const [clientId, setClientId] = useState('');
  const [status, setStatus] = useState<ProjectStatus | ''>('');
  const [q, setQ] = useState('');
  const [modalOpen, setModalOpen] = useState(false);
  // The project currently open in the edit modal, or null when closed.
  const [editing, setEditing] = useState<Project | null>(null);

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
          canCreate ? (
            <Button
              size="sm"
              onClick={() => setModalOpen(true)}
              disabled={!hasClients}
              title={hasClients ? undefined : 'Register a client first.'}
            >
              New project
            </Button>
          ) : undefined
        }
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <SearchableSelect
          label="Client"
          value={clientId}
          onChange={setClientId}
          noneLabel="All clients"
          placeholder="All clients"
          options={clients.map((c) => ({ value: String(c.id), label: `${c.code} — ${c.name}` }))}
        />
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
              {canManage && (
                <Th>
                  <span className="sr-only">Actions</span>
                </Th>
              )}
            </Tr>
          </THead>
          <tbody>
            {rows.map((p) => (
              <Tr key={p.id}>
                <Td className="font-mono font-medium">
                  <Link
                    to={`/m/projects/${p.id}`}
                    className="text-brand-600 hover:text-brand-700"
                  >
                    {p.code}
                  </Link>
                </Td>
                <Td className="text-slate-900">{p.name}</Td>
                <Td>
                  <span className="font-mono text-slate-900">{p.client_code}</span>
                  <span className="text-slate-400"> — </span>
                  <span className="text-slate-600">{p.client_name}</span>
                </Td>
                <Td className="whitespace-nowrap">{formatDate(p.start_date)}</Td>
                <Td>
                  {canManage ? (
                    <StatusControl project={p} />
                  ) : (
                    <Badge tone={PROJECT_STATUS_TONE[p.status]}>
                      {PROJECT_STATUS_LABEL[p.status]}
                    </Badge>
                  )}
                </Td>
                {canManage && (
                  <Td className="whitespace-nowrap text-right">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setEditing(p)}
                      aria-label={`Edit ${p.code}`}
                    >
                      Edit
                    </Button>
                  </Td>
                )}
              </Tr>
            ))}
          </tbody>
        </Table>
      )}

      <NewProjectModal open={modalOpen} clients={clients} onClose={() => setModalOpen(false)} />
      <EditProjectModal project={editing} onClose={() => setEditing(null)} />
    </div>
  );
}
