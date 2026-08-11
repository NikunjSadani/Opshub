import { Td, TextField, TextArea } from '../../../ui';
import type { Hsn, HsnInput } from '../../../api/masterdata';
import { EntityManager } from './EntityManager';

const BLANK: HsnInput = { hsn: '', description: '', gst_rate: '' };

/** HSN code registry — code + description + GST rate used on challan lines. */
export function HsnScreen() {
  return (
    <EntityManager<Hsn, HsnInput>
      kind="hsn"
      title="HSN Codes"
      subtitle="Product HSN codes and their GST rate."
      addLabel="HSN Code"
      emptyTitle="No HSN codes yet"
      columns={['HSN', 'Description', 'GST %']}
      blankInput={BLANK}
      rowId={(r) => r.id}
      rowActive={(r) => r.active}
      rowName={(r) => r.hsn}
      toInput={(r) => ({ hsn: r.hsn, description: r.description, gst_rate: r.gst_rate })}
      renderCells={(r) => (
        <>
          <Td className="font-mono text-slate-900">{r.hsn}</Td>
          <Td>{r.description || '—'}</Td>
          <Td>{r.gst_rate}%</Td>
        </>
      )}
      renderFields={(form, patch, errors) => (
        <>
          <TextField
            label="HSN"
            required
            inputMode="numeric"
            maxLength={12}
            className="font-mono"
            value={form.hsn}
            error={errors.hsn}
            hint="2–12 digits."
            onChange={(e) => patch({ hsn: e.target.value.replace(/[^0-9]/g, '') })}
          />
          <TextArea
            label="Description"
            rows={2}
            value={form.description}
            error={errors.description}
            onChange={(e) => patch({ description: e.target.value })}
          />
          <TextField
            label="GST rate (%)"
            required
            inputMode="decimal"
            value={form.gst_rate}
            error={errors.gst_rate}
            hint="0–100, up to 2 decimal places."
            onChange={(e) => patch({ gst_rate: e.target.value })}
          />
        </>
      )}
    />
  );
}
