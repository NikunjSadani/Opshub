"""Shared expense-test fixtures.

The `client` harness (fake extractor + in-memory sqlite + seeded project/method) is
defined in ``test_expense_routes``; re-exporting it here makes it available to every
expense test module without importing the fixture by name (which would shadow the
per-test ``client`` parameter and trip ruff F811)."""
from tests.expense.test_expense_routes import client  # noqa: F401  (re-exported fixture)
