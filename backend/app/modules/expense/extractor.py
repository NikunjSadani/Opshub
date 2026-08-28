"""The `Extractor` seam.

A `Protocol` (not an HTTP service) so a paid engine (`DocumentAIEngine`) can slot in
later behind a config flip, WITHOUT building the standalone product-agnostic `/extract`
service now (deferred — see the design doc). `doc_type` is a parameter from day one so the
contract generalizes, but only `"gst_invoice"` is implemented; other types raise cleanly.

The concrete `TextLayerExtractor` lives in `text_layer.py` and is resolved LAZILY by
`get_extractor()` (via importlib) so this seam has no import-time dependency on the engine
— the service layer depends only on this module.
"""
import importlib
from typing import Protocol

from app.modules.expense.canonical import ExtractedInvoice


class Extractor(Protocol):
    name: str

    def extract(self, pdf_bytes: bytes, *, doc_type: str = "gst_invoice") -> ExtractedInvoice:
        ...


def get_extractor(setting: str = "auto") -> Extractor:
    """Config-driven factory.

    ``auto`` (the DEFAULT, so every service call site gets it without change) resolves to the
    `TallyAwareExtractor`: it reads the PDF once and routes a Tally 'Tax Invoice' to the
    dedicated `TallyInvoiceExtractor`, delegating every other document UNCHANGED to the
    zero-cost `TextLayerExtractor`. ``text_layer`` forces the generic engine only (used by the
    eval gold-set harness). ``docai`` (a paid engine) is deferred until a real OCR-volume use
    case arrives.
    """
    if setting in ("auto", "tally_aware"):
        module = importlib.import_module("app.modules.expense.tally")
        aware: Extractor = module.TallyAwareExtractor()
        return aware
    if setting == "text_layer":
        module = importlib.import_module("app.modules.expense.text_layer")
        extractor: Extractor = module.TextLayerExtractor()
        return extractor
    raise NotImplementedError(f"extractor '{setting}' is not available (deferred)")
