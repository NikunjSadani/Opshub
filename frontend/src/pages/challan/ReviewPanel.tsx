import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Badge, Button, ConfirmDialog, ErrorState, Loading, useToast } from '../../ui';
import { usePermissions } from '../../auth/AuthProvider';
import { ApiError } from '../../api/client';
import {
  challanKeys,
  useBatchDecisions,
  useSubmitDecisions,
  type BatchOut,
  type Decision,
  type DecisionChoice,
} from '../../api/challan';
import { BATCH_STATUS_LABEL, errorMessage } from './challanFormat';

/** Human labels for the consignee fields a contradiction can be raised on. */
export const FIELD_LABELS: Record<string, string> = {
  name: 'Consignee name',
  address_line1: 'Address line 1',
  address_line2: 'Address line 2',
  pincode: 'Pincode',
  state: 'State',
  phone: 'Phone',
};

/**
 * The three ways an operator can resolve one contradiction, ordered
 * least-destructive first so the permanent shared-master change is never the
 * first (default-looking) option. Hints spell out the real consequence — that
 * "Update master" permanently changes the SHARED saved record for that GSTIN.
 */
export const CHOICE_OPTIONS: ReadonlyArray<{
  value: DecisionChoice;
  label: string;
  hint: string;
}> = [
  {
    value: 'THIS_UPLOAD',
    label: 'This upload only',
    hint: 'Use your value on this challan only — saved records unchanged.',
  },
  {
    value: 'REJECT',
    label: 'Reject',
    hint: 'Keep and print our saved value.',
  },
  {
    value: 'UPDATE_MASTER',
    label: 'Update master',
    hint: "Permanently update the saved record for this GSTIN — affects everyone's future uploads (and prints here).",
  },
];

/**
 * The amber review panel for a NEEDS_REVIEW batch: one row per contradicted
 * field (grouped by consignee), each with a 3-way choice, plus a blocking
 * "Save decisions & continue" that flips the batch to VALIDATED once nothing is
 * left PENDING.
 *
 * Self-contained: given only a `batch`, it owns its own decisions query (via the
 * shared `useBatchDecisions` key, so a parent that also observes the same key —
 * e.g. NewChallan's live region — dedupes with it). This lets the panel be
 * mounted from anywhere a batch id is known (the New Challan step 3 flow AND the
 * Batches tab), so a NEEDS_REVIEW batch is never a dead-end after a refresh.
 */
export function ReviewPanel({
  batch,
  onDownload,
  onResolved,
}: {
  batch: BatchOut;
  onDownload: (fileId: number | null, fallback: string) => void;
  onResolved: (updated: BatchOut) => void;
}) {
  const toast = useToast();
  const perms = usePermissions();
  // "Update master" permanently edits shared master data — the backend requires
  // MANAGE on the document_automation module. Gate it in the UI too so a user
  // without MANAGE can't pick an option that only earns them a 403 after clearing
  // the danger confirm. Mirrors how the Register gates Void.
  const isAdmin = perms.atLeast('document_automation', 'MANAGE');
  // Submitting decisions is a WRITE — the backend requires OPERATE. Gate the save
  // in the UI too so a View user isn't shown an action that only 403s. (Entry to
  // this panel from the Batches tab is already OPERATE-gated; this defends the
  // other mount points — e.g. the New Challan flow.)
  const canOperate = perms.atLeast('document_automation', 'OPERATE');
  const qc = useQueryClient();
  const submit = useSubmitDecisions();
  const decisionsQuery = useBatchDecisions(batch.id);
  const decisions = decisionsQuery.data;

  // Selections keyed by decision id; absent = still unselected. Seed from any
  // server choice that isn't PENDING, without clobbering the operator's picks.
  const [choices, setChoices] = useState<Record<number, DecisionChoice>>({});
  // A durable inline error that survives a re-render (unlike a transient toast),
  // shown when a submit is rejected (e.g. a 409 — the batch changed underneath).
  const [submitError, setSubmitError] = useState<string | null>(null);
  // Whether the confirm dialog for a permanent shared-master change is open.
  const [confirmOpen, setConfirmOpen] = useState(false);

  useEffect(() => {
    if (!decisions) return;
    setChoices((prev) => {
      const next = { ...prev };
      for (const d of decisions) {
        if (next[d.id] === undefined && d.choice !== 'PENDING') {
          next[d.id] = d.choice as DecisionChoice;
        }
      }
      return next;
    });
  }, [decisions]);

  const rows = decisions ?? [];
  const decidedCount = rows.filter((d) => choices[d.id] !== undefined).length;
  const allChosen = rows.length > 0 && decidedCount === rows.length;
  const updateMasterCount = rows.filter((d) => choices[d.id] === 'UPDATE_MASTER').length;

  // Group by consignee (GSTIN) for readability, preserving first-seen order.
  const groups: Array<{ gstin: string; name: string; items: Decision[] }> = [];
  const byGstin = new Map<string, { gstin: string; name: string; items: Decision[] }>();
  for (const d of rows) {
    let g = byGstin.get(d.gstin);
    if (!g) {
      g = { gstin: d.gstin, name: d.consignee_name, items: [] };
      byGstin.set(d.gstin, g);
      groups.push(g);
    }
    g.items.push(d);
  }

  function select(id: number, value: DecisionChoice) {
    setSubmitError(null);
    setChoices((prev) => ({ ...prev, [id]: value }));
  }

  function doSubmit() {
    const payload = rows.map((d) => ({ id: d.id, choice: choices[d.id] }));
    submit.mutate(
      { batchId: batch.id, decisions: payload },
      {
        onSuccess: (updated) => {
          setConfirmOpen(false);
          setSubmitError(null);
          onResolved(updated);
          if (updated.status === 'VALIDATED') {
            toast.success('All contradictions resolved — ready to generate.');
          }
        },
        onError: (err) => {
          setConfirmOpen(false);
          // The batch likely changed under the operator (another staffer
          // resolved it, or the master moved). Pull fresh state so the panel
          // re-renders the current contradictions, and leave a durable message.
          void qc.invalidateQueries({ queryKey: challanKeys.batch(batch.id) });
          void qc.invalidateQueries({ queryKey: challanKeys.decisions(batch.id) });
          const isConflict = err instanceof ApiError && err.status === 409;
          setSubmitError(
            isConflict
              ? 'This batch changed since you opened it — someone may have resolved it, or the saved records moved. We refreshed the items below; please re-review and save again.'
              : errorMessage(err),
          );
          toast.error(errorMessage(err));
        },
      },
    );
  }

  function onSave() {
    if (!allChosen) return;
    // A permanent shared-master change gets an explicit confirm first.
    if (updateMasterCount > 0) {
      setConfirmOpen(true);
      return;
    }
    doSubmit();
  }

  function onCheckAgain() {
    setSubmitError(null);
    void qc.invalidateQueries({ queryKey: challanKeys.batch(batch.id) });
    void decisionsQuery.refetch();
  }

  return (
    <div className="rounded-md border border-amber-200 bg-amber-50 p-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge tone="amber">{BATCH_STATUS_LABEL[batch.status]}</Badge>
        <span className="text-sm text-slate-500">Batch #{batch.id}</span>
      </div>
      <p className="text-sm text-amber-800">
        This upload disagrees with your saved records for some consignees. Decide each one
        before generating.
      </p>
      <div className="mt-3">
        <Button
          variant="secondary"
          size="sm"
          onClick={() => onDownload(batch.error_report_file_id, `batch-${batch.id}-review.xlsx`)}
        >
          Download review report (Excel)
        </Button>
      </div>

      {submitError && (
        <p
          role="alert"
          className="mt-3 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700"
        >
          {submitError}
        </p>
      )}

      {decisionsQuery.isPending ? (
        <div className="mt-4">
          <Loading label="Loading contradictions…" />
        </div>
      ) : decisionsQuery.isError ? (
        <div className="mt-4">
          <ErrorState error={decisionsQuery.error} onRetry={() => void decisionsQuery.refetch()} />
        </div>
      ) : rows.length === 0 ? (
        <div className="mt-4">
          <p className="text-sm text-slate-600">
            No contradictions to review right now. This batch may have already been resolved.
          </p>
          <div className="mt-3">
            <Button
              variant="secondary"
              size="sm"
              onClick={onCheckAgain}
              loading={decisionsQuery.isFetching}
            >
              Check again
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-4 space-y-4">
          {groups.map((g) => {
            const consigneeId = `consignee-${batch.id}-${g.gstin}`;
            return (
              <div key={g.gstin} className="rounded-md border border-amber-200 bg-white p-3">
                <div className="mb-2">
                  <p id={consigneeId} className="text-sm font-semibold text-slate-900">
                    {g.name}
                  </p>
                  <p className="text-xs tabular-nums text-slate-500">GSTIN {g.gstin}</p>
                </div>
                <div className="space-y-3">
                  {g.items.map((d) => {
                    const legendId = `legend-${d.id}`;
                    const undecided = choices[d.id] === undefined;
                    return (
                      <fieldset
                        key={d.id}
                        aria-labelledby={`${consigneeId} ${legendId}`}
                        className="border-t border-slate-100 pt-3"
                      >
                        <legend id={legendId} className="text-xs font-semibold text-slate-700">
                          {FIELD_LABELS[d.field] ?? d.field}
                          {undecided && (
                            <span className="ml-1 font-normal text-amber-700">· not decided</span>
                          )}
                        </legend>
                        <dl className="mt-1 grid grid-cols-1 gap-x-4 gap-y-1 text-sm sm:grid-cols-2">
                          <div>
                            <dt className="text-xs text-slate-500">Our records</dt>
                            <dd className="text-slate-900">{d.stored_value || '—'}</dd>
                          </div>
                          <div>
                            <dt className="text-xs text-slate-500">Your upload</dt>
                            <dd className="text-slate-900">{d.uploaded_value || '—'}</dd>
                          </div>
                        </dl>
                        <div className="mt-2 flex flex-col gap-1.5 sm:flex-row sm:flex-wrap sm:gap-4">
                          {CHOICE_OPTIONS.map((opt) => {
                            const inputId = `decision-${d.id}-${opt.value}`;
                            const optDisabled = opt.value === 'UPDATE_MASTER' && !isAdmin;
                            return (
                              <label
                                key={opt.value}
                                htmlFor={inputId}
                                className={`flex items-start gap-2 text-sm ${
                                  optDisabled ? 'text-slate-400' : 'text-slate-700'
                                }`}
                              >
                                <input
                                  id={inputId}
                                  type="radio"
                                  name={`decision-${d.id}`}
                                  value={opt.value}
                                  checked={choices[d.id] === opt.value}
                                  onChange={() => select(d.id, opt.value)}
                                  disabled={optDisabled}
                                  className="mt-0.5 disabled:cursor-not-allowed"
                                />
                                <span>
                                  <span className="font-medium">{opt.label}</span>
                                  <span className="block text-xs text-slate-500">
                                    {optDisabled
                                      ? 'Admin only — choose This upload / Reject, or ask an admin.'
                                      : opt.hint}
                                  </span>
                                </span>
                              </label>
                            );
                          })}
                        </div>
                      </fieldset>
                    );
                  })}
                </div>
              </div>
            );
          })}

          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            {canOperate ? (
              <>
                <Button onClick={onSave} disabled={!allChosen} loading={submit.isPending}>
                  Save decisions &amp; continue
                </Button>
                <span className="text-xs tabular-nums text-slate-500">
                  {decidedCount} of {rows.length} decided
                </span>
                {!allChosen && (
                  <span className="text-xs text-slate-500">
                    Pick an option for every field to continue.
                  </span>
                )}
              </>
            ) : (
              <span className="text-xs text-slate-500">
                Resolving contradictions needs Operate access — ask an operator to save these
                decisions.
              </span>
            )}
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirmOpen}
        title="Update saved records for everyone?"
        confirmLabel="Update saved records"
        danger
        loading={submit.isPending}
        onCancel={() => setConfirmOpen(false)}
        onConfirm={doSubmit}
        message={
          <>
            This will permanently update {updateMasterCount} saved consignee record
            {updateMasterCount === 1 ? '' : 's'} for everyone — it changes what future uploads
            print for {updateMasterCount === 1 ? 'this GSTIN' : 'these GSTINs'}. Continue?
          </>
        }
      />
    </div>
  );
}
