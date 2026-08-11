import { Td, TextField } from '../../../ui';
import type { Series, SeriesInput } from '../../../api/masterdata';
import { EntityManager } from './EntityManager';

const BLANK: SeriesInput = { letter: '', label: '' };

/** Challan number series — the leading letter(s) and a human label. */
export function SeriesScreen() {
  return (
    <EntityManager<Series, SeriesInput>
      kind="series"
      title="Series"
      subtitle="Challan numbering series (the leading letter prefix)."
      addLabel="Series"
      emptyTitle="No series yet"
      columns={['Letter', 'Label']}
      blankInput={BLANK}
      rowId={(r) => r.id}
      rowActive={(r) => r.active}
      rowName={(r) => r.letter}
      toInput={(r) => ({ letter: r.letter, label: r.label })}
      renderCells={(r) => (
        <>
          <Td className="font-mono font-semibold text-slate-900">{r.letter}</Td>
          <Td>{r.label || '—'}</Td>
        </>
      )}
      renderFields={(form, patch, errors) => (
        <>
          <TextField
            label="Letter"
            required
            maxLength={8}
            className="font-mono uppercase"
            value={form.letter}
            error={errors.letter}
            hint="1–8 letters or digits. Stored uppercase."
            onChange={(e) => patch({ letter: e.target.value.toUpperCase() })}
          />
          <TextField
            label="Label"
            value={form.label}
            error={errors.label}
            onChange={(e) => patch({ label: e.target.value })}
          />
        </>
      )}
    />
  );
}
