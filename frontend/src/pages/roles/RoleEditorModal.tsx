import { useEffect, useMemo, useState } from 'react';
import { Button, ErrorState, Loading, Modal, TextArea, TextField, useToast } from '../../ui';
import {
  useAssignableModules,
  useCreateRole,
  useUpdateRole,
  type ModuleLevel,
  type Role,
  type RoleInput,
} from '../../api/roles';
import {
  buildTitleByKey,
  errorMessage,
  GrantSummary,
  LEVEL_OPTIONS,
  PLATFORM_PERMISSIONS,
  type LevelValue,
} from './rolesFormat';

/** How the editor was opened: a blank create, an edit, or a clone (create-from). */
export type EditorMode = 'create' | 'edit' | 'clone';

/**
 * Segmented None/View/Operate/Manage control for one module, exposed as a
 * radiogroup (labelled by the module title) so it is keyboard- and
 * screen-reader-navigable. Selecting "None" clears the module's grant.
 */
function LevelSegmented({
  moduleKey,
  moduleTitle,
  value,
  onChange,
  disabled,
}: {
  moduleKey: string;
  moduleTitle: string;
  value: LevelValue;
  onChange: (v: LevelValue) => void;
  disabled?: boolean;
}) {
  const name = `level-${moduleKey}`;
  return (
    <div
      role="radiogroup"
      aria-label={moduleTitle}
      className="inline-flex overflow-hidden rounded-md border border-slate-300"
    >
      {LEVEL_OPTIONS.map((opt) => {
        const selected = value === opt.value;
        return (
          <label
            key={opt.label}
            className={`cursor-pointer px-2.5 py-1 text-xs font-medium transition ${
              selected ? 'bg-brand-600 text-white' : 'bg-white text-slate-600 hover:bg-slate-50'
            } ${disabled ? 'cursor-not-allowed opacity-60' : ''}`}
          >
            <input
              type="radio"
              name={name}
              className="sr-only"
              checked={selected}
              disabled={disabled}
              onChange={() => onChange(opt.value)}
            />
            {opt.label}
          </label>
        );
      })}
    </div>
  );
}

/**
 * Create / Edit / Clone a role. Collects a name + description, a level per
 * assignable module (None omits the module), and the platform permission
 * toggles, showing a live "what this role grants" summary. On save it POSTs
 * (create/clone) or PATCHes (edit) and surfaces 400/409 errors inline + as a
 * toast. System roles are never routed here for edit (the list guards that).
 */
export function RoleEditorModal({
  open,
  mode,
  role,
  onClose,
}: {
  open: boolean;
  mode: EditorMode;
  role?: Role | null;
  onClose: () => void;
}) {
  const toast = useToast();
  const modulesQuery = useAssignableModules();
  const createRole = useCreateRole();
  const updateRole = useUpdateRole();

  const isEdit = mode === 'edit';
  const mutation = isEdit ? updateRole : createRole;
  const busy = mutation.isPending;

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  // Full per-module selection incl. '' (None); the payload omits the '' entries.
  const [levels, setLevels] = useState<Record<string, LevelValue>>({});
  const [platform, setPlatform] = useState<string[]>([]);

  // Seed the form each time the modal opens. Clone pre-fills from the source
  // role but with a fresh "(copy)" name; edit pre-fills as-is; create is blank.
  useEffect(() => {
    if (!open) return;
    const seed = mode === 'create' ? null : role ?? null;
    setName(mode === 'clone' && seed ? `${seed.name} (copy)` : isEdit && seed ? seed.name : '');
    setDescription(seed ? seed.description : '');
    setLevels(seed ? { ...seed.module_levels } : {});
    setPlatform(seed ? [...seed.platform] : []);
    createRole.reset();
    updateRole.reset();
    // Re-seed only on open / target / mode changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, role, mode]);

  const titleByKey = useMemo(
    () => buildTitleByKey(modulesQuery.data ?? []),
    [modulesQuery.data],
  );

  // The payload's module_levels: drop every module left at None ('').
  const moduleLevels = useMemo<Record<string, ModuleLevel>>(() => {
    const out: Record<string, ModuleLevel> = {};
    for (const [key, value] of Object.entries(levels)) {
      if (value) out[key] = value;
    }
    return out;
  }, [levels]);

  const trimmedName = name.trim();
  const canSubmit = trimmedName.length > 0 && !busy;

  function setLevel(key: string, value: LevelValue) {
    setLevels((prev) => ({ ...prev, [key]: value }));
  }

  function togglePlatform(key: string, checked: boolean) {
    setPlatform((prev) => (checked ? [...new Set([...prev, key])] : prev.filter((p) => p !== key)));
  }

  function close() {
    if (busy) return;
    onClose();
  }

  function submit() {
    if (!canSubmit) return;
    const body: RoleInput = {
      name: trimmedName,
      description: description.trim(),
      module_levels: moduleLevels,
      platform,
    };
    if (isEdit && role) {
      updateRole.mutate(
        { id: role.id, ...body },
        {
          onSuccess: (saved) => {
            toast.success(`Saved ${saved.name}.`);
            onClose();
          },
          onError: (err) => toast.error(errorMessage(err)),
        },
      );
    } else {
      createRole.mutate(body, {
        onSuccess: (created) => {
          toast.success(`Created ${created.name}.`);
          onClose();
        },
        onError: (err) => toast.error(errorMessage(err)),
      });
    }
  }

  const title = isEdit
    ? `Edit ${role?.name ?? 'role'}`
    : mode === 'clone'
      ? 'Clone role'
      : 'New role';
  const submitLabel = isEdit ? 'Save changes' : 'Create role';

  return (
    <Modal
      open={open}
      title={title}
      onClose={close}
      busy={busy}
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={submit} loading={busy} disabled={!canSubmit}>
            {submitLabel}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <TextField
          label="Name"
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Operations Lead"
          maxLength={80}
        />
        <TextArea
          label="Description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What is this role for?"
          rows={2}
          maxLength={280}
        />

        <fieldset className="space-y-2">
          <legend className="mb-1 text-xs font-medium text-slate-600">Module access</legend>
          {modulesQuery.isPending ? (
            <Loading label="Loading modules…" />
          ) : modulesQuery.isError ? (
            <ErrorState
              error={modulesQuery.error}
              onRetry={() => void modulesQuery.refetch()}
            />
          ) : modulesQuery.data.length === 0 ? (
            <p className="text-xs text-slate-400">No grantable modules are available.</p>
          ) : (
            <div className="space-y-1.5">
              {modulesQuery.data.map((m) => (
                <div
                  key={m.key}
                  className="flex items-center justify-between gap-3 rounded-md border border-slate-200 px-2.5 py-1.5"
                >
                  <span className="min-w-0 truncate text-sm text-slate-700">{m.title}</span>
                  <LevelSegmented
                    moduleKey={m.key}
                    moduleTitle={m.title}
                    value={levels[m.key] ?? ''}
                    onChange={(v) => setLevel(m.key, v)}
                    disabled={busy}
                  />
                </div>
              ))}
            </div>
          )}
        </fieldset>

        <fieldset className="space-y-2">
          <legend className="mb-1 text-xs font-medium text-slate-600">Platform permissions</legend>
          <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            {PLATFORM_PERMISSIONS.map((p) => (
              <label
                key={p.key}
                className="flex items-start gap-2 rounded-md border border-slate-200 px-2.5 py-1.5 text-sm text-slate-700 hover:bg-slate-50"
              >
                <input
                  type="checkbox"
                  checked={platform.includes(p.key)}
                  disabled={busy}
                  onChange={(e) => togglePlatform(p.key, e.target.checked)}
                  className="mt-0.5 h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500/40"
                />
                <span className="min-w-0">
                  <span className="block font-medium">{p.label}</span>
                  <span className="block text-xs text-slate-400">{p.hint}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="rounded-md border border-slate-200 bg-slate-50/60 px-3 py-2">
          <p className="mb-1.5 text-xs font-medium text-slate-600">What this role grants</p>
          <GrantSummary
            moduleLevels={moduleLevels}
            platform={platform}
            titleByKey={titleByKey}
            emptyLabel="No access yet — pick a level or a permission above."
          />
        </div>

        {mutation.isError && (
          <p role="alert" className="text-sm text-rose-600">
            {errorMessage(mutation.error)}
          </p>
        )}
      </div>
    </Modal>
  );
}
