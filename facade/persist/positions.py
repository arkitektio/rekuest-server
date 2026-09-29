"""Session positions: handle every numbered frame once, however often it is sent.

A numbering agent stamps each frame with ``pos`` (1, 2, 3, … per session) and ``journal_session``,
keeps it until a ``JOURNAL_ACK`` covers it, and re-sends the rest in ``pos`` order after a
reconnect or restart. The server keeps one watermark per session, ``Session.projected_pos``: a
frame at or below it is a resend and is skipped. Nothing else is stored per frame; the frame's
projection (task events, patches, snapshots, transitions) is the record (see
``docs/design/journal.md``).

Handling one frame is claim → project → confirm:

- **claim**: a conditional UPDATE that moves ``claimed_pos`` to ``pos`` only while nothing else is
  in flight (``claimed_pos = projected_pos``). Two backends that got the same frame — the agent
  reconnected elsewhere while the first was still working — cannot both win. The loser waits for
  the winner to confirm; a claim older than ``CLAIM_TIMEOUT`` (its backend died mid-projection) is
  taken over.
- **project**: the caller routes the frame.
- **confirm**: ``projected_pos = pos``. A projection that raised releases the claim instead, so
  the resend projects it again.

A frame beyond ``projected_pos + 1`` can only mean the agent no longer holds the frames in between
(it sends from its lowest unacked position, in order): they are lost, the gap is logged, and the
watermark moves past it rather than waiting forever.

Frames without ``pos`` (agents without numbering, every probe frame) never reach this module.
"""

import asyncio
import datetime
import enum
import logging
from typing import Any, Dict, Optional

from django.db.models import F, Q
from django.utils import timezone

from facade import messages, models
from facade.probes.ids import is_probe_id

logger = logging.getLogger(__name__)

CLAIM_TIMEOUT_SECONDS = 30.0
"""How long a claim may stay unconfirmed before another backend takes the frame over."""

WAIT_POLL_SECONDS = 0.05


class Position(enum.Enum):
    """What to do with a numbered frame."""

    PROJECT = "project"
    """Claimed: project it, then ``confirm_position`` (or ``release_position`` if it raised)."""
    DUPLICATE = "duplicate"
    """At or below the watermark: already handled. Skip it; ack again."""


def is_numbered(message: Any) -> bool:
    """Whether a FromAgent frame carries a session position (and is not a probe's)."""
    if not (isinstance(message, messages.JournalFields) and message.pos is not None and message.journal_session is not None):
        return False
    return not is_probe_id(getattr(message, "task", None))


def agent_time(message: Any) -> Optional[datetime.datetime]:
    """The frame's ``agent_ts`` (epoch seconds) as an aware datetime, if it carries one."""
    ts = getattr(message, "agent_ts", None)
    if ts is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def position_stamp(message: Any) -> Dict[str, Any]:
    """The ``agent_pos`` / ``agent_ts`` / ``step`` columns a projected row carries — empty without numbering."""
    if not is_numbered(message):
        return {}
    return {"agent_pos": message.pos, "agent_ts": agent_time(message), "step": message.task_step}


class AgentPositionMixin:
    async def _position_session(self, agent_id: int, journal_session: str) -> models.Session:
        session, _ = await models.Session.objects.aget_or_create(agent_id=agent_id, session_id=journal_session)
        return session

    async def claim_position(self, agent_id: int, message: messages.JournalFields) -> Position:
        """Claim ``message.pos`` of its session for projection, or report it a duplicate.

        Waits (polling) while another backend projects the preceding position or this one.
        """
        assert message.pos is not None and message.journal_session is not None
        pos = message.pos
        session = await self._position_session(agent_id, message.journal_session)
        rows = models.Session.objects.filter(pk=session.pk)

        while True:
            current = await rows.values("projected_pos", "claimed_pos", "claimed_at").afirst()
            if current is None:  # the agent (and its sessions) was deleted underneath us
                return Position.DUPLICATE
            projected, claimed, claimed_at = current["projected_pos"], current["claimed_pos"], current["claimed_at"]
            if pos <= projected:
                return Position.DUPLICATE

            now = timezone.now()
            stale = claimed_at is None or (now - claimed_at).total_seconds() > CLAIM_TIMEOUT_SECONDS
            idle = claimed <= projected
            if not idle and not stale:
                await asyncio.sleep(WAIT_POLL_SECONDS)  # another backend is projecting; wait for it
                continue

            if pos > projected + 1:
                logger.warning(
                    "Agent %s session %s: positions %s..%s never arrived (the agent no longer holds them); continuing at %s",
                    agent_id,
                    message.journal_session,
                    projected + 1,
                    pos - 1,
                    pos,
                )
            # Conditional on exactly what we read: a concurrent claim (or confirm) changes one of
            # them, and we lose and re-read.
            won = await rows.filter(projected_pos=projected, claimed_pos=claimed).filter(Q(claimed_at=claimed_at) if claimed_at is not None else Q(claimed_at__isnull=True)).aupdate(
                projected_pos=pos - 1,
                claimed_pos=pos,
                claimed_at=now,
            )
            if won:
                return Position.PROJECT

    async def confirm_position(self, agent_id: int, message: messages.JournalFields) -> None:
        """The claimed frame is projected: move the watermark onto it."""
        await models.Session.objects.filter(agent_id=agent_id, session_id=message.journal_session, claimed_pos=message.pos).aupdate(projected_pos=F("claimed_pos"))

    async def release_position(self, agent_id: int, message: messages.JournalFields) -> None:
        """The claimed frame's projection failed: give the claim back, so the resend projects it."""
        await models.Session.objects.filter(agent_id=agent_id, session_id=message.journal_session, claimed_pos=message.pos).aupdate(claimed_pos=F("projected_pos"), claimed_at=None)

    async def projected_position(self, agent_id: int, journal_session: str) -> int:
        """The session's watermark: what a JOURNAL_ACK may claim."""
        value = await models.Session.objects.filter(agent_id=agent_id, session_id=journal_session).values_list("projected_pos", flat=True).afirst()
        return value or 0


__all__ = ["AgentPositionMixin", "Position", "is_numbered", "agent_time", "position_stamp", "CLAIM_TIMEOUT_SECONDS"]
