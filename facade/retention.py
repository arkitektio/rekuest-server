"""Retention sweep for terminal task trees.

Tasks were previously kept forever — there was no GC at all. When
``TASK_RETENTION_SECONDS`` is set (> 0), this sweep deletes terminal ROOT tasks whose
``finished_at`` is past the horizon; the ``parent``/``root`` self-FK CASCADEs take the
whole tree with them, along with its TaskEvents, TaskInstructs and Patches
(``Lock.hold_by`` SET_NULLs). A done root can still have a live descendant (a cancel
goes to the mother only), so roots with any not-done member are skipped — sound only
because ``root_id`` is set on every assign-created child (and backfilled by migration).

Retention is an explicit operator opt-in (default 0 = disabled): deleting past runs also
removes them from replay discovery (``reusable_task_for``). 2592000 (30 days) is a
sensible production value.

Ephemeral trees (the runs of a schedule with ``ephemeral_runs`` — services' housekeeping sweeps,
thousands a day) have their own, shorter horizon, ``EPHEMERAL_TASK_RETENTION_SECONDS``, which is
on by default: they are never replay candidates, and without it they would pile up forever.

Triggered by the reaper loop (``facade.reaper``): one batch per slow tick, so a
backlog drains over successive ticks rather than in one long transaction.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db.models import Exists, OuterRef
from django.utils import timezone

from facade import models

logger = logging.getLogger(__name__)


def retention_seconds() -> int:
    """The retention horizon (seconds); 0 disables the sweep."""
    return int(getattr(settings, "TASK_RETENTION_SECONDS", 0))


def ephemeral_retention_seconds() -> int:
    """The ephemeral-task horizon (seconds); 0 disables that half of the sweep."""
    return int(getattr(settings, "EPHEMERAL_TASK_RETENTION_SECONDS", 0))


def sweep_terminal_tasks(batch_size: int = 500, max_batches: Optional[int] = None) -> int:
    """Delete terminal root task trees past their retention horizon. Returns roots deleted.

    Batched so a huge backlog never runs one giant transaction; idempotent and
    multi-worker-safe (a concurrently deleted pk simply matches nothing).
    """
    deleted = 0
    _sweep_signals(batch_size)
    ephemeral = ephemeral_retention_seconds()
    if ephemeral > 0:
        deleted += _sweep(ephemeral, batch_size, max_batches, ephemeral=True)
    retention = retention_seconds()
    if retention > 0:
        deleted += _sweep(retention, batch_size, max_batches)
    return deleted


def _sweep_signals(batch_size: int) -> int:
    """Drop one batch of processed signals past ``SIGNAL_RETENTION_SECONDS`` (0 keeps them)."""
    retention = int(getattr(settings, "SIGNAL_RETENTION_SECONDS", 0))
    if retention <= 0:
        return 0
    cutoff = timezone.now() - timedelta(seconds=retention)
    ids = list(models.Signal.objects.filter(processed_at__lt=cutoff).values_list("pk", flat=True)[:batch_size])
    if ids:
        models.Signal.objects.filter(pk__in=ids).delete()
    return len(ids)


def _sweep(retention: int, batch_size: int, max_batches: Optional[int], **only) -> int:
    cutoff = timezone.now() - timedelta(seconds=retention)
    live_descendants = models.Task.objects.filter(root_id=OuterRef("pk"), is_done=False)

    deleted_roots = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        roots = list(
            models.Task.objects.filter(is_done=True, root__isnull=True, finished_at__lt=cutoff, **only)
            .exclude(Exists(live_descendants))
            .values_list("pk", flat=True)[:batch_size]
        )
        if not roots:
            break
        models.Task.objects.filter(pk__in=roots).delete()
        deleted_roots += len(roots)
        batches += 1

    if deleted_roots:
        logger.info("Retention sweep deleted %s terminal %stask tree(s) past %ss.", deleted_roots, "ephemeral " if only else "", retention)
    return deleted_roots
