"""
Standalone service entrypoints (MULTIAGENT_MIGRATION.md Phase B).

Thin process/scheduler wrappers around the shared logic in app/agents/*.py.
Each file here is its own independently-runnable process (its own
BackgroundScheduler, its own `python3 -m app.services.X` entrypoint) that
persists its output to a shared Postgres table rather than returning it
in-memory - once split out, nothing else in the codebase may import these
modules directly (that would just re-create the in-process coupling this
phase exists to remove).
"""
