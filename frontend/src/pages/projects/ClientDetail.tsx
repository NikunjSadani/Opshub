import { useEffect, useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
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
import {
  useAddAddress,
  useAddContact,
  useAddGstin,
  useClientDetail,
  useDeactivateAddress,
  useDeactivateContact,
  useDeactivateGstin,
  useUpdateAddress,
  useUpdateClient,
  useUpdateContact,
  useUpdateGstin,
  type Address,
  type Contact,
  type Gstin,
} from '../../api/projects';
import { errorMessage } from './projectsFormat';

const PROJECTS_BASE = '/m/projects';

/** Small default marker used across the three child sections. */
function DefaultBadge() {
  return <Badge tone="blue">Default</Badge>;
}

/** A labelled checkbox row (no CheckboxField exists in the form kit). */
function CheckboxRow({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="flex items-center gap-2 text-sm text-slate-700">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-2 focus:ring-brand-500/40"
      />
      {label}
    </label>
  );
}

/** Card wrapper for a child collection: header + add action + a table (or empty state). */
function Section({
  title,
  count,
  canManage,
  onAdd,
  isEmpty,
  emptyText,
  children,
}: {
  title: string;
  count: number;
  canManage: boolean;
  onAdd: () => void;
  isEmpty: boolean;
  emptyText: string;
  children: ReactNode;
}) {
  return (
    <Card className="mt-5">
      <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3">
        <h2 className="text-sm font-semibold text-slate-900">
          {title} <span className="text-slate-400">({count})</span>
        </h2>
        {canManage && (
          <Button size="sm" variant="secondary" onClick={onAdd}>
            Add
          </Button>
        )}
      </div>
      {isEmpty ? (
        <div className="px-5 py-6 text-center text-sm text-slate-400">{emptyText}</div>
      ) : (
        <div className="px-1 py-1">{children}</div>
      )}
    </Card>
  );
}

// ============================================================================
// Client header (name / code / PAN / credit terms) + its edit modal.
// ============================================================================

function ClientEditModal({
  clientId,
  open,
  initial,
  onClose,
}: {
  clientId: string;
  open: boolean;
  initial: {
    name: string;
    pan: string | null;
    credit_terms_days: number | null;
    active: boolean;
    access_pin: string | null;
  };
  onClose: () => void;
}) {
  const toast = useToast();
  const update = useUpdateClient(clientId);

  const [name, setName] = useState(initial.name);
  const [pan, setPan] = useState(initial.pan ?? '');
  const [creditTerms, setCreditTerms] = useState(
    initial.credit_terms_days == null ? '' : String(initial.credit_terms_days),
  );
  const [active, setActive] = useState(initial.active);
  const [accessPin, setAccessPin] = useState(initial.access_pin ?? '');

  useEffect(() => {
    if (!open) return;
    setName(initial.name);
    setPan(initial.pan ?? '');
    setCreditTerms(initial.credit_terms_days == null ? '' : String(initial.credit_terms_days));
    setActive(initial.active);
    setAccessPin(initial.access_pin ?? '');
    update.reset();
    // Re-seed only on open transitions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const trimmedName = name.trim();
  const termsValid = creditTerms === '' || /^\d{1,5}$/.test(creditTerms);
  // Blank clears the PIN; a set value must be 8-32 chars (mirrors the backend).
  const trimmedPin = accessPin.trim();
  const pinValid = trimmedPin === '' || (trimmedPin.length >= 8 && trimmedPin.length <= 32);
  const canSubmit = trimmedName.length > 0 && termsValid && pinValid && !update.isPending;

  function close() {
    if (update.isPending) return;
    onClose();
  }

  function submit() {
    if (!canSubmit) return;
    update.mutate(
      {
        name: trimmedName,
        pan: pan.trim() ? pan.trim().toUpperCase() : null,
        credit_terms_days: creditTerms === '' ? null : Number(creditTerms),
        active,
        // "" clears the PIN on the backend; a set value is sent verbatim.
        access_pin: trimmedPin === '' ? '' : trimmedPin,
      },
      {
        onSuccess: () => {
          toast.success('Client updated.');
          onClose();
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  }

  return (
    <Modal
      open={open}
      title="Edit client"
      onClose={close}
      busy={update.isPending}
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={update.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} loading={update.isPending} disabled={!canSubmit}>
            Save
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
          maxLength={120}
        />
        <TextField
          label="PAN"
          className="font-mono uppercase"
          value={pan}
          hint="10-character PAN (optional)."
          onChange={(e) =>
            setPan(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 10))
          }
          maxLength={10}
        />
        <TextField
          label="Credit terms (days)"
          type="text"
          inputMode="numeric"
          value={creditTerms}
          error={creditTerms !== '' && !termsValid ? 'Whole number of days.' : undefined}
          hint="Payment terms in days (optional)."
          onChange={(e) => setCreditTerms(e.target.value.replace(/[^0-9]/g, '').slice(0, 5))}
        />
        <TextField
          label="Access PIN (challan QR)"
          value={accessPin}
          error={!pinValid ? '8-32 characters.' : undefined}
          hint="Used as the password for the QR on this client's delivery challans, together with the challan number. Share it with the client directly; it's never printed. Leave blank to clear."
          onChange={(e) => setAccessPin(e.target.value.slice(0, 32))}
          maxLength={32}
        />
        <CheckboxRow label="Active" checked={active} onChange={setActive} disabled={update.isPending} />
      </div>
    </Modal>
  );
}

// ============================================================================
// GSTINs
// ============================================================================

function GstinModal({
  clientId,
  open,
  editing,
  onClose,
}: {
  clientId: string;
  open: boolean;
  editing: Gstin | null;
  onClose: () => void;
}) {
  const toast = useToast();
  const add = useAddGstin(clientId);
  const update = useUpdateGstin(clientId);
  const isEdit = editing != null;
  const busy = add.isPending || update.isPending;

  const [gstin, setGstin] = useState('');
  const [legalName, setLegalName] = useState('');
  const [stateCode, setStateCode] = useState('');
  const [isDefault, setIsDefault] = useState(false);

  useEffect(() => {
    if (!open) return;
    setGstin(editing?.gstin ?? '');
    setLegalName(editing?.legal_name ?? '');
    setStateCode(editing?.state_code ?? '');
    setIsDefault(editing?.is_default ?? false);
    add.reset();
    update.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const gstinValid = gstin.trim().length === 15;
  // The GSTIN value is the key; on edit it is fixed, so only the other fields matter.
  const canSubmit = (isEdit || gstinValid) && !busy;

  function close() {
    if (busy) return;
    onClose();
  }

  function submit() {
    if (!canSubmit) return;
    const done = {
      onSuccess: () => {
        toast.success(isEdit ? 'GSTIN updated.' : 'GSTIN added.');
        onClose();
      },
      onError: (err: unknown) => toast.error(errorMessage(err)),
    };
    if (isEdit) {
      update.mutate(
        {
          gstin_id: editing.id,
          patch: {
            legal_name: legalName.trim() || undefined,
            state_code: stateCode.trim() || undefined,
            is_default: isDefault,
          },
        },
        done,
      );
    } else {
      add.mutate(
        {
          gstin: gstin.trim().toUpperCase(),
          legal_name: legalName.trim() || undefined,
          state_code: stateCode.trim() || undefined,
          is_default: isDefault,
        },
        done,
      );
    }
  }

  return (
    <Modal
      open={open}
      title={isEdit ? 'Edit GSTIN' : 'Add GSTIN'}
      onClose={close}
      busy={busy}
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={submit} loading={busy} disabled={!canSubmit}>
            {isEdit ? 'Save' : 'Add'}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <TextField
          label="GSTIN"
          required
          className="font-mono uppercase"
          value={gstin}
          disabled={isEdit}
          hint={isEdit ? 'The GSTIN itself cannot be changed.' : 'Exactly 15 characters.'}
          error={!isEdit && gstin.length > 0 && !gstinValid ? '15 characters required.' : undefined}
          onChange={(e) =>
            setGstin(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 15))
          }
          maxLength={15}
        />
        <TextField
          label="Legal name"
          value={legalName}
          onChange={(e) => setLegalName(e.target.value)}
          maxLength={160}
        />
        <TextField
          label="State code"
          className="font-mono"
          value={stateCode}
          onChange={(e) => setStateCode(e.target.value.replace(/[^0-9]/g, '').slice(0, 2))}
          maxLength={2}
        />
        <CheckboxRow label="Default GSTIN" checked={isDefault} onChange={setIsDefault} disabled={busy} />
      </div>
    </Modal>
  );
}

function GstinSection({
  clientId,
  gstins,
  canManage,
}: {
  clientId: string;
  gstins: Gstin[];
  canManage: boolean;
}) {
  const toast = useToast();
  const deactivate = useDeactivateGstin(clientId);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Gstin | null>(null);
  const [confirm, setConfirm] = useState<Gstin | null>(null);

  function openAdd() {
    setEditing(null);
    setModalOpen(true);
  }
  function openEdit(row: Gstin) {
    setEditing(row);
    setModalOpen(true);
  }
  function doDeactivate() {
    if (!confirm) return;
    deactivate.mutate(confirm.id, {
      onSuccess: () => {
        toast.success('GSTIN removed.');
        setConfirm(null);
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  return (
    <>
      <Section
        title="GSTINs"
        count={gstins.length}
        canManage={canManage}
        onAdd={openAdd}
        isEmpty={gstins.length === 0}
        emptyText="No GSTINs on file."
      >
        <Table>
          <THead>
            <Tr>
              <Th>GSTIN</Th>
              <Th>Legal name</Th>
              <Th>State</Th>
              <Th>Default</Th>
              {canManage && <Th>Actions</Th>}
            </Tr>
          </THead>
          <tbody>
            {gstins.map((g) => (
              <Tr key={g.id}>
                <Td className="font-mono text-slate-900">{g.gstin}</Td>
                <Td>{g.legal_name ?? '—'}</Td>
                <Td className="font-mono">{g.state_code ?? '—'}</Td>
                <Td>{g.is_default ? <DefaultBadge /> : '—'}</Td>
                {canManage && (
                  <Td>
                    <div className="flex gap-2">
                      <Button size="sm" variant="ghost" onClick={() => openEdit(g)}>
                        Edit
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirm(g)}>
                        Deactivate
                      </Button>
                    </div>
                  </Td>
                )}
              </Tr>
            ))}
          </tbody>
        </Table>
      </Section>

      {/* Modals live OUTSIDE <Section> so Add works even when the collection is
          empty (Section hides its children in the empty branch). */}
      {canManage && (
        <>
          <GstinModal
            clientId={clientId}
            open={modalOpen}
            editing={editing}
            onClose={() => setModalOpen(false)}
          />
          <ConfirmDialog
            open={confirm !== null}
            title="Deactivate GSTIN"
            danger
            confirmLabel="Deactivate"
            loading={deactivate.isPending}
            message={
              <>
                Remove GSTIN <span className="font-mono">{confirm?.gstin}</span> from this client?
              </>
            }
            onConfirm={doDeactivate}
            onCancel={() => setConfirm(null)}
          />
        </>
      )}
    </>
  );
}

// ============================================================================
// Addresses
// ============================================================================

function AddressModal({
  clientId,
  open,
  editing,
  gstins,
  onClose,
}: {
  clientId: string;
  open: boolean;
  editing: Address | null;
  gstins: Gstin[];
  onClose: () => void;
}) {
  const toast = useToast();
  const add = useAddAddress(clientId);
  const update = useUpdateAddress(clientId);
  const isEdit = editing != null;
  const busy = add.isPending || update.isPending;

  const [label, setLabel] = useState('');
  const [line1, setLine1] = useState('');
  const [line2, setLine2] = useState('');
  const [city, setCity] = useState('');
  const [state, setState] = useState('');
  const [pincode, setPincode] = useState('');
  const [gstinId, setGstinId] = useState('');
  const [isDefault, setIsDefault] = useState(false);

  useEffect(() => {
    if (!open) return;
    setLabel(editing?.label ?? '');
    setLine1(editing?.line1 ?? '');
    setLine2(editing?.line2 ?? '');
    setCity(editing?.city ?? '');
    setState(editing?.state ?? '');
    setPincode(editing?.pincode ?? '');
    setGstinId(editing?.gstin_id ?? '');
    setIsDefault(editing?.is_default ?? false);
    add.reset();
    update.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const canSubmit = line1.trim().length > 0 && !busy;

  function close() {
    if (busy) return;
    onClose();
  }

  function submit() {
    if (!canSubmit) return;
    const done = {
      onSuccess: () => {
        toast.success(isEdit ? 'Address updated.' : 'Address added.');
        onClose();
      },
      onError: (err: unknown) => toast.error(errorMessage(err)),
    };
    const body = {
      gstin_id: gstinId || undefined,
      label: label.trim() || undefined,
      line1: line1.trim(),
      line2: line2.trim() || undefined,
      city: city.trim() || undefined,
      state: state.trim() || undefined,
      pincode: pincode.trim() || undefined,
      is_default: isDefault,
    };
    if (isEdit) update.mutate({ address_id: editing.id, patch: body }, done);
    else add.mutate(body, done);
  }

  return (
    <Modal
      open={open}
      title={isEdit ? 'Edit address' : 'Add address'}
      onClose={close}
      busy={busy}
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={submit} loading={busy} disabled={!canSubmit}>
            {isEdit ? 'Save' : 'Add'}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <TextField label="Label" value={label} onChange={(e) => setLabel(e.target.value)} maxLength={80} />
        <TextField
          label="Address line 1"
          required
          value={line1}
          onChange={(e) => setLine1(e.target.value)}
          maxLength={160}
        />
        <TextField label="Address line 2" value={line2} onChange={(e) => setLine2(e.target.value)} maxLength={160} />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <TextField label="City" value={city} onChange={(e) => setCity(e.target.value)} maxLength={80} />
          <TextField label="State" value={state} onChange={(e) => setState(e.target.value)} maxLength={80} />
          <TextField
            label="Pincode"
            className="font-mono"
            value={pincode}
            onChange={(e) => setPincode(e.target.value.replace(/[^0-9]/g, '').slice(0, 6))}
            maxLength={6}
          />
        </div>
        {gstins.length > 0 && (
          <label className="block">
            <span className="mb-1 flex items-center gap-1 text-xs font-medium text-slate-600">
              GSTIN (optional)
            </span>
            <select
              value={gstinId}
              onChange={(e) => setGstinId(e.target.value)}
              className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/40"
            >
              <option value="">None</option>
              {gstins.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.gstin}
                </option>
              ))}
            </select>
          </label>
        )}
        <CheckboxRow label="Default address" checked={isDefault} onChange={setIsDefault} disabled={busy} />
      </div>
    </Modal>
  );
}

function AddressSection({
  clientId,
  addresses,
  gstins,
  canManage,
}: {
  clientId: string;
  addresses: Address[];
  gstins: Gstin[];
  canManage: boolean;
}) {
  const toast = useToast();
  const deactivate = useDeactivateAddress(clientId);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Address | null>(null);
  const [confirm, setConfirm] = useState<Address | null>(null);

  function doDeactivate() {
    if (!confirm) return;
    deactivate.mutate(confirm.id, {
      onSuccess: () => {
        toast.success('Address removed.');
        setConfirm(null);
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  return (
    <>
      <Section
        title="Addresses"
        count={addresses.length}
        canManage={canManage}
        onAdd={() => {
          setEditing(null);
          setModalOpen(true);
        }}
        isEmpty={addresses.length === 0}
        emptyText="No addresses on file."
      >
        <Table>
          <THead>
            <Tr>
              <Th>Label</Th>
              <Th>Address</Th>
              <Th>City</Th>
              <Th>Pincode</Th>
              <Th>Default</Th>
              {canManage && <Th>Actions</Th>}
            </Tr>
          </THead>
          <tbody>
            {addresses.map((a) => (
              <Tr key={a.id}>
                <Td>{a.label ?? '—'}</Td>
                <Td className="text-slate-900">
                  {a.line1}
                  {a.line2 ? `, ${a.line2}` : ''}
                  {a.state ? <span className="text-slate-500">{`, ${a.state}`}</span> : null}
                </Td>
                <Td>{a.city ?? '—'}</Td>
                <Td className="font-mono">{a.pincode ?? '—'}</Td>
                <Td>{a.is_default ? <DefaultBadge /> : '—'}</Td>
                {canManage && (
                  <Td>
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setEditing(a);
                          setModalOpen(true);
                        }}
                      >
                        Edit
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirm(a)}>
                        Deactivate
                      </Button>
                    </div>
                  </Td>
                )}
              </Tr>
            ))}
          </tbody>
        </Table>
      </Section>

      {/* Modals live OUTSIDE <Section> so Add works even when the collection is empty. */}
      {canManage && (
        <>
          <AddressModal
            clientId={clientId}
            open={modalOpen}
            editing={editing}
            gstins={gstins}
            onClose={() => setModalOpen(false)}
          />
          <ConfirmDialog
            open={confirm !== null}
            title="Deactivate address"
            danger
            confirmLabel="Deactivate"
            loading={deactivate.isPending}
            message={<>Remove this address from the client?</>}
            onConfirm={doDeactivate}
            onCancel={() => setConfirm(null)}
          />
        </>
      )}
    </>
  );
}

// ============================================================================
// Contacts
// ============================================================================

function ContactModal({
  clientId,
  open,
  editing,
  onClose,
}: {
  clientId: string;
  open: boolean;
  editing: Contact | null;
  onClose: () => void;
}) {
  const toast = useToast();
  const add = useAddContact(clientId);
  const update = useUpdateContact(clientId);
  const isEdit = editing != null;
  const busy = add.isPending || update.isPending;

  const [name, setName] = useState('');
  const [designation, setDesignation] = useState('');
  const [email, setEmail] = useState('');
  const [phone, setPhone] = useState('');
  const [isDefault, setIsDefault] = useState(false);

  useEffect(() => {
    if (!open) return;
    setName(editing?.name ?? '');
    setDesignation(editing?.designation ?? '');
    setEmail(editing?.email ?? '');
    setPhone(editing?.phone ?? '');
    setIsDefault(editing?.is_default ?? false);
    add.reset();
    update.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const canSubmit = name.trim().length > 0 && !busy;

  function close() {
    if (busy) return;
    onClose();
  }

  function submit() {
    if (!canSubmit) return;
    const done = {
      onSuccess: () => {
        toast.success(isEdit ? 'Contact updated.' : 'Contact added.');
        onClose();
      },
      onError: (err: unknown) => toast.error(errorMessage(err)),
    };
    const body = {
      name: name.trim(),
      designation: designation.trim() || undefined,
      email: email.trim() || undefined,
      phone: phone.trim() || undefined,
      is_default: isDefault,
    };
    if (isEdit) update.mutate({ contact_id: editing.id, patch: body }, done);
    else add.mutate(body, done);
  }

  return (
    <Modal
      open={open}
      title={isEdit ? 'Edit contact' : 'Add contact'}
      onClose={close}
      busy={busy}
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={submit} loading={busy} disabled={!canSubmit}>
            {isEdit ? 'Save' : 'Add'}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <TextField label="Name" required value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
        <TextField
          label="Designation"
          value={designation}
          onChange={(e) => setDesignation(e.target.value)}
          maxLength={120}
        />
        <TextField label="Email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} maxLength={160} />
        <TextField label="Phone" value={phone} onChange={(e) => setPhone(e.target.value)} maxLength={20} />
        <CheckboxRow label="Default contact" checked={isDefault} onChange={setIsDefault} disabled={busy} />
      </div>
    </Modal>
  );
}

function ContactSection({
  clientId,
  contacts,
  canManage,
}: {
  clientId: string;
  contacts: Contact[];
  canManage: boolean;
}) {
  const toast = useToast();
  const deactivate = useDeactivateContact(clientId);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Contact | null>(null);
  const [confirm, setConfirm] = useState<Contact | null>(null);

  function doDeactivate() {
    if (!confirm) return;
    deactivate.mutate(confirm.id, {
      onSuccess: () => {
        toast.success('Contact removed.');
        setConfirm(null);
      },
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  return (
    <>
      <Section
        title="Contacts"
        count={contacts.length}
        canManage={canManage}
        onAdd={() => {
          setEditing(null);
          setModalOpen(true);
        }}
        isEmpty={contacts.length === 0}
        emptyText="No contacts on file."
      >
        <Table>
          <THead>
            <Tr>
              <Th>Name</Th>
              <Th>Designation</Th>
              <Th>Email</Th>
              <Th>Phone</Th>
              <Th>Default</Th>
              {canManage && <Th>Actions</Th>}
            </Tr>
          </THead>
          <tbody>
            {contacts.map((c) => (
              <Tr key={c.id}>
                <Td className="text-slate-900">{c.name}</Td>
                <Td>{c.designation ?? '—'}</Td>
                <Td>{c.email ?? '—'}</Td>
                <Td className="font-mono">{c.phone ?? '—'}</Td>
                <Td>{c.is_default ? <DefaultBadge /> : '—'}</Td>
                {canManage && (
                  <Td>
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setEditing(c);
                          setModalOpen(true);
                        }}
                      >
                        Edit
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirm(c)}>
                        Deactivate
                      </Button>
                    </div>
                  </Td>
                )}
              </Tr>
            ))}
          </tbody>
        </Table>
      </Section>

      {/* Modals live OUTSIDE <Section> so Add works even when the collection is empty. */}
      {canManage && (
        <>
          <ContactModal
            clientId={clientId}
            open={modalOpen}
            editing={editing}
            onClose={() => setModalOpen(false)}
          />
          <ConfirmDialog
            open={confirm !== null}
            title="Deactivate contact"
            danger
            confirmLabel="Deactivate"
            loading={deactivate.isPending}
            message={<>Remove {confirm?.name} from the client's contacts?</>}
            onConfirm={doDeactivate}
            onCancel={() => setConfirm(null)}
          />
        </>
      )}
    </>
  );
}

// ============================================================================
// Screen
// ============================================================================

/** A read/write field on the header summary grid. */
function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-900">{children}</dd>
    </div>
  );
}

/**
 * Client Master detail/editor. Reads need projects VIEW; every edit (header + the
 * three child collections) is gated on projects MANAGE — a non-manager sees the
 * full profile read-only, with no add / edit / deactivate affordances. The backend
 * enforces `client.manage` regardless; the gating here is UX.
 */
export function ClientDetail() {
  const params = useParams();
  const id = params.id ?? null;
  const perms = usePermissions();
  const canManage = perms.atLeast('projects', 'MANAGE');

  const query = useClientDetail(id);
  const [editOpen, setEditOpen] = useState(false);

  const backLink = (
    <Link
      to={`${PROJECTS_BASE}/clients`}
      className="text-sm font-medium text-brand-600 hover:text-brand-700"
    >
      ← Back to clients
    </Link>
  );

  if (id == null) {
    return (
      <div>
        <div className="mb-4">{backLink}</div>
        <StatePanel tone="red" title="Invalid client">
          That client link is not valid.
        </StatePanel>
      </div>
    );
  }

  if (query.isPending) return <Loading label="Loading client…" />;
  if (query.isError || !query.data) {
    return (
      <div>
        <div className="mb-4">{backLink}</div>
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );
  }

  const client = query.data;

  return (
    <div>
      <div className="mb-4">{backLink}</div>

      <PageHeader
        title={client.name}
        subtitle="Client master — profile, GST registrations, addresses and contacts."
        actions={
          canManage ? (
            <Button size="sm" variant="secondary" onClick={() => setEditOpen(true)}>
              Edit client
            </Button>
          ) : undefined
        }
      />

      <Card className="px-5 py-4">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Detail label="Code">
            <span className="font-mono">{client.code}</span>
          </Detail>
          <Detail label="PAN">
            <span className="font-mono">{client.pan ?? '—'}</span>
          </Detail>
          <Detail label="Credit terms">
            {client.credit_terms_days == null ? '—' : `${client.credit_terms_days} days`}
          </Detail>
          <Detail label="Status">
            <Badge tone={client.active ? 'green' : 'slate'}>
              {client.active ? 'Active' : 'Inactive'}
            </Badge>
          </Detail>
          <Detail label="Challan QR PIN">
            <Badge tone={client.access_pin ? 'green' : 'slate'}>
              {client.access_pin ? 'Set' : 'Not set'}
            </Badge>
          </Detail>
        </dl>
      </Card>

      <GstinSection clientId={client.id} gstins={client.gstins} canManage={canManage} />
      <AddressSection
        clientId={client.id}
        addresses={client.addresses}
        gstins={client.gstins}
        canManage={canManage}
      />
      <ContactSection clientId={client.id} contacts={client.contacts} canManage={canManage} />

      {canManage && (
        <ClientEditModal
          clientId={client.id}
          open={editOpen}
          initial={{
            name: client.name,
            pan: client.pan,
            credit_terms_days: client.credit_terms_days,
            active: client.active,
            access_pin: client.access_pin,
          }}
          onClose={() => setEditOpen(false)}
        />
      )}
    </div>
  );
}
