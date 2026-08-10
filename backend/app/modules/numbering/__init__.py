"""Document numbering — the statutory-integrity core.

Reserve-before-generate FY-reset document numbers (`GIF/DC/26-27/L/000189`) with
a counter row-lock and a unique partial index on non-void allocations as the
hard backstop against duplicates. See `service.py` for the invariants.
"""
