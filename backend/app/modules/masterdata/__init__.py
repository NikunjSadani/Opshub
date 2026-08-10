"""Master data — the reference entities the Delivery Challan module draws on.

Consignor (dispatching party) · Consignee registry (Brand -> State -> GSTIN +
address, snapshotted onto each challan) · HSN codes (with GST rate for
validation) · challan series. Admin-maintained config, not transactional data.
"""
