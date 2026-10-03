"""What schedules and triggers share when they are written: the target of their runs.

A rule (a schedule, a trigger) runs an action, optionally pinned to one agent's implementation.
The checks here are made once, when the rule is created, changed or imported with a wiregram,
so a rule that could never run is refused then instead of failing on every firing.
"""

from __future__ import annotations

from django.conf import settings

from facade import models


def check_pin(action: models.Action, agent: models.Agent | None, interface: str | None) -> None:
    """A pin is an agent together with one of its interfaces, implementing ``action``."""
    if (agent is None) != (interface is None):
        raise ValueError("Pin an agent with both agent and interface, or give neither")
    if agent is not None and not models.Implementation.objects.filter(agent=agent, interface=interface, action=action).exists():
        raise ValueError(f"Agent {agent.pk} has no implementation {interface!r} of this action")


def check_provenance(action: models.Action, agent: models.Agent | None, interface: str | None, why: str) -> None:
    """Refuse targets whose runs could never get a provenance token.

    A rule's run has no human request behind it; a strict provenance policy refuses to mint for
    that, so every run would fail. ``why`` says so in the rule's own terms.
    """
    if not settings.PROVENANCE.get("STRICT"):
        return
    implementations = models.Implementation.objects.filter(action=action)
    if agent is not None:
        implementations = implementations.filter(agent=agent, interface=interface)
    if implementations.filter(needs_token=True).exists():
        raise ValueError(why)
