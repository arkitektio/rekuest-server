from django.db import models
from django.db.models.functions import Now


class CallerSession(models.Model):
    """One app window's session on takt (``/session``), the durable part of it.

    What a session watches and how far it got lives in redis (takt's registry); this row is
    only what must outlive redis: who it is, and when it was last heard from. The dead man
    switch reads ``last_seen`` (takt's reaper), so a redis restart cancels nothing.
    """

    caller_id: int

    id = models.CharField(
        primary_key=True, max_length=64, help_text="The session id takt gave the window"
    )
    caller = models.ForeignKey(
        "Caller",
        on_delete=models.CASCADE,
        related_name="sessions",
        help_text="Who the session is (the token's client, user and organization)",
    )
    client_info = models.JSONField(
        default=dict,
        db_default={},
        help_text="What the window said about itself at HELLO (app, version, window label)",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    last_seen = models.DateTimeField(
        db_default=Now(),
        help_text="When takt last heard from the window (written on a coarse beat)",
    )
    ended_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the session ended (its grace ran out). NULL while it may still resume.",
    )

    def __str__(self) -> str:
        return f"Session {self.id} of {self.caller_id}"


class SweepBeat(models.Model):
    """When a takt reaper last ran its sweeps: proof that takt was serving.

    A dead man switch counts a session's absence only while takt was up, so a deploy or an
    outage longer than a switch's seconds cancels nothing (takt's ``reaper.rs``).
    """

    name = models.CharField(primary_key=True, max_length=100)
    at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return f"{self.name} at {self.at}"
