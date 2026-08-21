"""Logistics module — delivery tracking for issued challans.

Upload the logistics partner's dump (Excel) or enter manually, keyed on the
**challan number** (the challan's `GIF/DC/…` formatted number). Tracks delivery
status + tracking id + partner + consignee + proof-of-delivery (POD), and surfaces
the challan's existing invoice#/PO#/project. Does NOT change the statutory challan
generation. RBAC module key: ``logistics``. See `docs/plans/PROJECT-SPINE-DESIGN.md` §5f.
"""
