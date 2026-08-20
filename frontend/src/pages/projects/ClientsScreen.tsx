import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Badge,
  Button,
  ErrorState,
  Loading,
  Modal,
  PageHeader,
  StatePanel,
  Table,
  TextField,
  THead,
  Th,
  Tr,
  Td,
  useToast,
} from '../../ui';
import { usePermissions } from '../../auth/AuthProvider';
import { useClientsQuery, useCreateClient } from '../../api/projects';
import { errorMessage } from './projectsFormat';

const CODE_RE = /^[A-Z]{3}$/;

/**
 * Clients registry. Lists registered clients (VIEW — any projects-module user can
 * read the list and open a client's detail) and creates new ones (MANAGE). The code
 * is a 3-letter A–Z key, uppercased as typed and validated inline before the create
 * button enables; a 409 (duplicate/invalid) surfaces via toast. The backend enforces
 * `client.manage` regardless; the New-client gating here is UX.
 */
export function ClientsScreen() {
  const toast = useToast();
  const perms = usePermissions();
  const canManage = perms.atLeast('projects', 'MANAGE');
  const query = useClientsQuery();
  const createClient = useCreateClient();

  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [code, setCode] = useState('');

  const trimmedName = name.trim();
  const codeValid = CODE_RE.test(code);
  const canSubmit = trimmedName.length > 0 && codeValid && !createClient.isPending;
  // Only nag about the code once the user has typed something.
  const codeError = code.length > 0 && !codeValid ? '3 letters A–Z.' : undefined;

  function openModal() {
    setName('');
    setCode('');
    createClient.reset();
    setOpen(true);
  }

  function closeModal() {
    if (createClient.isPending) return;
    setOpen(false);
  }

  function submit() {
    if (!canSubmit) return;
    createClient.mutate(
      { name: trimmedName, code },
      {
        onSuccess: (client) => {
          toast.success(`Client ${client.code} registered.`);
          setOpen(false);
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  return (
    <div>
      <PageHeader
        title="Clients"
        subtitle="Registered clients. Each has a unique 3-letter code used to number projects."
        actions={
          canManage ? (
            <Button size="sm" onClick={openModal}>
              New client
            </Button>
          ) : undefined
        }
      />

      {query.isPending ? (
        <Loading label="Loading clients…" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.length === 0 ? (
        <StatePanel title="No clients yet">
          Register a client to start creating projects under it.
        </StatePanel>
      ) : (
        <Table>
          <THead>
            <Tr>
              <Th>Code</Th>
              <Th>Name</Th>
              <Th>PAN</Th>
              <Th>Credit terms</Th>
              <Th>Status</Th>
            </Tr>
          </THead>
          <tbody>
            {query.data.map((c) => (
              <Tr key={c.id}>
                <Td className="font-mono font-medium text-slate-900">{c.code}</Td>
                <Td>
                  <Link
                    to={`${c.id}`}
                    className="font-medium text-brand-600 hover:text-brand-700 hover:underline"
                  >
                    {c.name}
                  </Link>
                </Td>
                <Td className="font-mono">{c.pan ?? '—'}</Td>
                <Td>{c.credit_terms_days == null ? '—' : `${c.credit_terms_days} days`}</Td>
                <Td>
                  <Badge tone={c.active ? 'green' : 'slate'}>
                    {c.active ? 'Active' : 'Inactive'}
                  </Badge>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}

      {canManage && (
        <Modal
          open={open}
          title="Register client"
          onClose={closeModal}
          busy={createClient.isPending}
          footer={
            <>
              <Button variant="secondary" onClick={closeModal} disabled={createClient.isPending}>
                Cancel
              </Button>
              <Button onClick={submit} loading={createClient.isPending} disabled={!canSubmit}>
                Register
              </Button>
            </>
          }
        >
          <div className="space-y-3">
            <TextField
              label="Name"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Britannia Industries"
              maxLength={120}
            />
            <TextField
              label="Code"
              required
              className="font-mono uppercase"
              value={code}
              error={codeError}
              hint="Exactly 3 letters A–Z (e.g. BRI). Uppercased automatically."
              onChange={(e) => setCode(e.target.value.toUpperCase().replace(/[^A-Z]/g, '').slice(0, 3))}
              maxLength={3}
            />
          </div>
        </Modal>
      )}
    </div>
  );
}
