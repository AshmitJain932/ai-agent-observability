"""Ingest pipeline: validate -> normalize -> enforce limits and privacy -> merge -> store.

``prepare`` holds the pure per-event steps (no I/O); ``pipeline`` runs one batch inside a database
transaction, storing through ``aobs_server.storage.traces``.
"""
