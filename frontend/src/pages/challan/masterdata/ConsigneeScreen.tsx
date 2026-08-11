import { Td, TextField, TextArea } from '../../../ui';
import type { Consignee, ConsigneeInput } from '../../../api/masterdata';
import { EntityManager } from './EntityManager';

const BLANK: ConsigneeInput = {
  brand: '',
  state: '',
  name: '',
  gstin: '',
  address: '',
  phone: '',
};

/** Consignee registry — one ship-to party per (Brand, State). */
export function ConsigneeScreen() {
  return (
    <EntityManager<Consignee, ConsigneeInput>
      kind="consignee"
      title="Consignee"
      subtitle="Ship-to party registry, unique per Brand + State."
      addLabel="Consignee"
      emptyTitle="No consignees yet"
      columns={['Brand', 'State', 'Name', 'GSTIN', 'Phone']}
      filterKeys={['brand', 'state']}
      blankInput={BLANK}
      rowId={(r) => r.id}
      rowActive={(r) => r.active}
      rowName={(r) => `${r.brand} · ${r.state}`}
      toInput={(r) => ({
        brand: r.brand,
        state: r.state,
        name: r.name,
        gstin: r.gstin,
        address: r.address,
        phone: r.phone,
      })}
      renderCells={(r) => (
        <>
          <Td className="font-medium text-slate-900">{r.brand}</Td>
          <Td>{r.state}</Td>
          <Td>{r.name}</Td>
          <Td className="font-mono text-xs">{r.gstin}</Td>
          <Td>{r.phone || '—'}</Td>
        </>
      )}
      renderFields={(form, patch, errors) => (
        <>
          <TextField
            label="Brand"
            required
            value={form.brand}
            error={errors.brand}
            hint="Unique together with State (case-insensitive)."
            onChange={(e) => patch({ brand: e.target.value })}
          />
          <TextField
            label="State"
            required
            value={form.state}
            error={errors.state}
            onChange={(e) => patch({ state: e.target.value })}
          />
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
            hint="15 characters. Its first 2 digits (state code) must match the State above."
            onChange={(e) => patch({ gstin: e.target.value.toUpperCase() })}
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
