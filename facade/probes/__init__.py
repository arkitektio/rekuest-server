"""Probes — hover-effect-grade invocations that never touch the database.

A *Probe* is the throwaway sibling of a :class:`facade.models.Task`: same agent wire
protocol (agents receive a normal ``Assign`` and report normal lifecycle events), but all
server-side state lives in redis under a TTL — no Task row, no TaskEvent rows, no replay,
no crash-recovery sweep, no provenance lineage. Probes are for high-frequency interactive
work (previews, live parameter tweaks) where latency and cancellation matter and history
does not.

The two concepts are separated by the id space: Task ids are integer PKs, Probe ids are
``p-<hex>`` strings (agentd mints them), so every router can branch with a
prefix check.
"""

