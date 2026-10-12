"""PostgreSQL storage: table definitions, migrations, engine and repositories.

Only this package talks to the database. Every function that reads or writes tenant data takes a
``project_id`` and filters on it.
"""
