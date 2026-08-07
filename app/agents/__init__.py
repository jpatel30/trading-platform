"""
Multi-agent modules (MULTIAGENT_MIGRATION.md Phase A).

Phase A extracts each future agent as a module in the same process,
with one clear entry point per agent, so the data contracts between
agents (the shared Postgres tables) can be proven before paying for
real service separation in Phase B. Nothing in here is a separate
service yet — these are import boundaries, not process boundaries.
"""
