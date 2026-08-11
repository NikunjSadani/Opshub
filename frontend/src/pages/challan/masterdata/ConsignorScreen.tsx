import { Td, TextField, TextArea } from '../../../ui';
import type { Consignor, ConsignorInput } from '../../../api/masterdata';
import { EntityManager } from './EntityManager';

const BLANK: ConsignorInput = { name: '', gstin: '', state: '', address: '', phone: '' };

/** Consignor registry — your own GST registrations that issue delivery challans. */
export function ConsignorScreen() {
  return (
    <EntityManager<Consignor, ConsignorInput>
      kind="consignor"
      title="Consignor"
      subtitle="Your own GST registrations that issue delivery challans."
      addLabel="Consignor"
      emptyTitle="No consignors yet"
      columns={['Name', 'GSTIN', 'State', 'Phone']}
      blankInput={BLANK}
      rowId={(r) => r.id}
      rowActive={(r) => r.active}
      rowName={(r) => r.name}
      toInput={(r) => ({
        name: r.name,
        gstin: r.gstin,
        state: r.state,
        address: r.address,
        phone: r.phone,
      })}
      renderCells={(r) => (
        <>
          <Td className="font-medium text-slate-900">{r.name}</Td>
          <Td className="font-mono text-xs">{r.gstin}</Td>
          <Td>{r.state}</Td>
          <Td>{r.phone || '—'}</Td>
        </>
      )}
      renderFields={(form, patch, errors) => (
        <>
          <TextField
            label="Name"
            required
            value={form.name}
            error={errors.name}
            onChange={(e) => patch({ name: e.target.value })}
          />
          <TextField
            label="GSTIN"
            required
            maxLength={15}
            className="font-mono"
            value={form.gstin}
            error={errors.gstin}
            hint="15 characters. The first 2 digits (state code) must match the state below."
            onChange={(e) => patch({ gstin: e.target.value.toUpperCase() })}
          />
          <TextField
            label="State"
            required
            value={form.state}
            error={errors.state}
            onChange={(e) => patch({ state: e.target.value })}
          />
          <TextArea
            label="Address"
            rows={2}
            value={form.address}
            error={errors.address}
            onChange={(e) => patch({ address: e.target.value })}
          />
          <TextField
            label="Phone"
            value={form.phone}
            error={errors.phone}
            onChange={(e) => patch({ phone: e.target.value })}
          />
        </>
      )}
    />
  );
}
