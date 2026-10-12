"""Ingest pipeline: validate -> normalize -> enforce limits and privacy -> merge -> store.

``prepare`` holds the pure per-event steps (no I/O). The database transaction that merges and
stores prepared events is added in a later milestone.
"""
