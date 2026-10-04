import datetime
from collections.abc import Awaitable, Callable, Sequence

from asgiref.sync import sync_to_async
from kante.types import Info
from strawberry.dataloader import DataLoader

from facade import models

PKType = int | str


async def load_agents_by_ids(ids: list[PKType]) -> list[models.Agent | None]:
    # 1. Fetch all matching agents in a single query (batching)
    agents_qs = models.Agent.objects.filter(id__in=ids)

    # 2. Map the results into a dictionary by ID.
    # Casting to string ensures we don't get misses if inputs are a mix of int/str
    agent_map = {str(agent.pk): agent async for agent in agents_qs}

    # 3. Return the agents mapped to the exact order of the input `ids`.
    # Any requested ID not found in the DB will return None, keeping the array length consistent.
    return [agent_map.get(str(i)) for i in ids]


async def load_implementations_by_ids(ids: list[PKType]) -> list[models.Implementation | None]:
    # 1. Fetch all matching implementations in a single query (batching)
    impl_qs = models.Implementation.objects.filter(id__in=ids)

    # 2. Map the results into a dictionary by ID.
    # Casting to string ensures we don't get misses if inputs are a mix of int/str
    impl_map = {str(impl.pk): impl async for impl in impl_qs}

    # 3. Return the implementations mapped to the exact order of the input `ids`.
    # Any requested ID not found in the DB will return None, keeping the array length consistent.
    return [impl_map.get(str(i)) for i in ids]


def _loader[K, V](info: Info, name: str, load_fn: Callable[[list[K]], Awaitable[Sequence[V | BaseException]]]) -> DataLoader[K, V]:
    """A DataLoader scoped to this request, kept on kante's per-request ``_loaders`` store.

    These used to be module-level singletons with caching on. A ``DataLoader``'s cache is never
    evicted, so each process pinned the first ``Agent``/``Implementation`` row it ever loaded for
    an id: a later query returned an agent's ``connected``/``name`` as they were at some arbitrary
    past moment, for the life of the process. With several backends that is also *inconsistent* —
    which stale snapshot you get depends on which one the load balancer picked.

    Batching is what these are for, and batching is per request, so nothing is lost. Caching stays
    on for an HTTP context (built fresh per request, so it only dedupes within one query) and off
    for a websocket context, which lives as long as the subscription.
    """
    loaders = info.context._loaders
    loader = loaders.get(name)
    if loader is None:
        loader = DataLoader(load_fn=load_fn, cache=info.context.type == "http")
        loaders[name] = loader
    return loader


async def load_upcoming_slots(asked: list[tuple[models.Schedule, int]]) -> list[list[datetime.datetime] | ValueError]:
    """Every schedule a query asks ``upcoming`` of, in one request to takt."""
    from facade import schedules

    return await sync_to_async(schedules.upcoming)(asked)


def upcoming_loader(info: Info) -> DataLoader[tuple[models.Schedule, int], list[datetime.datetime]]:
    return _loader(info, "facade.upcoming", load_upcoming_slots)


def agent_loader(info: Info) -> DataLoader[PKType, models.Agent | None]:
    return _loader(info, "facade.agents", load_agents_by_ids)


def implementation_loader(info: Info) -> DataLoader[PKType, models.Implementation | None]:
    return _loader(info, "facade.implementations", load_implementations_by_ids)
