"""Every redis key this service owns, under one configurable namespace.

Redis is shared infrastructure: the channel layer, the agent queues, probe state and the
reaper's tick token all live on one instance, next to every other arkitekt service — and,
in some setups, next to a second rekuest deployment. Bare keys such as ``42_my_queue`` are
one integer away from another deployment's agent 42, which would then be handed this
deployment's Assigns. ``REDIS_KEY_PREFIX`` (``redis.key_prefix``, default ``rekuest``)
scopes them; a deployment sharing its redis picks a distinct one (e.g. ``next:rekuest``).

A leaf module on purpose (imports only Django settings), so the queue, the probe store and
the reaper can all use it without import cycles.
"""

from django.conf import settings


def prefix() -> str:
    return str(getattr(settings, "REDIS_KEY_PREFIX", "rekuest") or "rekuest")


def key(*parts: object) -> str:
    """``{prefix}:{part}:{part}…`` — the only way first-party code should name a redis key."""
    return ":".join([prefix(), *(str(part) for part in parts)])
