"""Delivery Challan generator (module key `document_automation`).

Excel upload -> validate (downloadable English error report) -> reserve numbers
-> render faithful HTML->PDF challans -> ZIP + merged PDF -> register.

Consumes the numbering engine (reserve-before-generate) and master data
(consignor + Brand->State consignee registry + HSN). The consignee is resolved
by (brand, ship-to state) and SNAPSHOTTED onto each challan.
"""
