"""Server-side filters, orders and counts that keep a paginated list right past its first page.

Each answers a question a client used to answer by loading a capped list and filtering it: which
agents are online, how many there are, the actions of one app or collection together, a blok by
its exact name, a person's tasks.
"""

import datetime

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Client, Organization, Release, User
from django.db import connection
from django.utils import timezone
from kante.context import HttpContext

from facade import enums, models
from facade.schema import schema
from tests.factories import TEST_TOKEN
from tests.graphql.test_cross_tenant_isolation import (
    OTHER_TOKEN,
    TenantContext,
    tenant_context,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


async def _contexts() -> tuple[TenantContext, TenantContext]:
    return await sync_to_async(
        lambda: (tenant_context(TEST_TOKEN), tenant_context(OTHER_TOKEN))
    )()


async def _run(query: str, context: HttpContext, **variables: object) -> dict:
    result = await schema.execute(
        query, variable_values=variables or None, context_value=context
    )
    assert result.errors is None, result.errors
    assert result.data is not None
    return result.data


def _agent(organization: Organization, prefix: str, **fields: object) -> models.Agent:
    """An agent of ``organization`` with its own client, user and app."""
    user = User.objects.create(
        username=f"{prefix}-user", password="x", sub=f"{prefix}-sub"
    )
    client = Client.objects.create(client_id=f"{prefix}-client")
    app = App.objects.create(identifier=f"{prefix}-app")
    release = Release.objects.create(app=app, version="1.0.0")
    return models.Agent.objects.create(
        app=app,
        release=release,
        user=user,
        client=client,
        organization=organization,
        hash=f"{prefix}-hash",
        name=prefix,
        **fields,
    )


agent = sync_to_async(_agent)


class TestAgents:
    async def test_online_agents_are_filtered_and_counted_on_the_server(self) -> None:
        (context, *_, organization), (other, *_, other_organization) = await _contexts()
        fresh = timezone.now()
        stale = fresh - datetime.timedelta(hours=1)
        online = await agent(organization, "lf-online", connected=True, last_seen=fresh)
        stuck = await agent(
            organization, "lf-stuck", connected=True, last_seen=stale
        )  # crashed, never disconnected
        hook = await agent(organization, "lf-hook", kind=enums.AgentKind.WEBHOOK.value)
        await agent(other_organization, "lf-theirs", connected=True, last_seen=fresh)

        listed = "query($filters: AgentFilter) { agents(filters: $filters) { id } }"

        async def ids(**filters: object) -> set[str]:
            return {
                row["id"]
                for row in (await _run(listed, context, filters=filters))["agents"]
            }

        assert str(online.pk) in await ids(connected=True)
        assert {str(stuck.pk), str(hook.pk)}.isdisjoint(await ids(connected=True))
        assert {str(stuck.pk), str(hook.pk)} <= await ids(connected=False)
        assert str(online.pk) not in await ids(connected=False)
        # `available` still counts the webhook agent, `connected` never does.
        assert str(hook.pk) in await ids(available=True)

        assert await ids(kind="WEBHOOK", search="lf-") == {str(hook.pk)}
        assert await ids(kind="WEBSOCKET", search="lf-") == {
            str(online.pk),
            str(stuck.pk),
        }

        stats = """query {
            all: agentStats(filters: {search: "lf-"}) { count }
            online: agentStats(filters: {search: "lf-", connected: true}) { count }
        }"""
        assert await _run(stats, context) == {
            "all": {"count": 3},
            "online": {"count": 1},
        }
        assert (await _run(stats, other))["all"]["count"] == 1  # their own, not ours

    async def test_agents_are_ordered_by_when_they_registered(self) -> None:
        (context, *_, organization), _ = await _contexts()
        first = await agent(organization, "lf-first")
        second = await agent(organization, "lf-second")
        query = 'query($ordering: [AgentOrder!]!) { agents(filters: {search: "lf-"}, ordering: $ordering) { id installedAt } }'

        newest_first = (await _run(query, context, ordering=[{"installedAt": "DESC"}]))[
            "agents"
        ]
        assert [row["id"] for row in newest_first] == [str(second.pk), str(first.pk)]
        oldest_first = (await _run(query, context, ordering=[{"installedAt": "ASC"}]))[
            "agents"
        ]
        assert [row["id"] for row in oldest_first] == [str(first.pk), str(second.pk)]


def _action(
    organization: Organization,
    app: App,
    key: str,
    kind: str = "FUNCTION",
    collections: tuple[str, ...] = (),
    creator: User | None = None,
) -> models.Action:
    action = models.Action.objects.create(
        app=app,
        key=key,
        version="1.0.0",
        name=key,
        hash=f"{app.identifier}-{key}",
        organization=organization,
        kind=kind,
    )
    for name in collections:
        collection, _ = models.Collection.objects.get_or_create(
            name=name,
            organization=organization,
            defaults={"creator": creator, "description": ""},
        )
        action.collections.add(collection)
    return action


class TestActionOrder:
    async def test_actions_of_one_group_stay_together_across_pages(self) -> None:
        (context, user, _client, organization), _ = await _contexts()

        def seed() -> dict[str, models.Action]:
            zeta = App.objects.create(identifier="lf-zeta")
            alpha = App.objects.create(identifier="lf-alpha")
            return {
                "z1": _action(
                    organization,
                    zeta,
                    "lf-z1",
                    collections=("lf-segmentation", "lf-io"),
                    creator=user,
                ),
                "a1": _action(
                    organization,
                    alpha,
                    "lf-a1",
                    kind="GENERATOR",
                    collections=("lf-segmentation",),
                    creator=user,
                ),
                "z2": _action(organization, zeta, "lf-z2"),
                "a2": _action(
                    organization,
                    alpha,
                    "lf-a2",
                    collections=("lf-tracking",),
                    creator=user,
                ),
            }

        actions = await sync_to_async(seed)()
        name = {str(action.pk): key for key, action in actions.items()}
        query = 'query($ordering: [ActionOrder!]!) { actions(filters: {search: "lf-"}, ordering: $ordering) { id } }'

        async def order(*ordering: dict[str, str]) -> list[str]:
            return [
                name[row["id"]]
                for row in (await _run(query, context, ordering=list(ordering)))[
                    "actions"
                ]
                if row["id"] in name
            ]

        by_app = await order({"appIdentifier": "ASC"}, {"definedAt": "ASC"})
        assert by_app == ["a1", "a2", "z1", "z2"]

        # Filed under its first collection by name ("lf-io" before "lf-segmentation"); none last.
        assert await order({"collection": "ASC"}, {"definedAt": "ASC"}) == [
            "z1",
            "a1",
            "a2",
            "z2",
        ]
        assert (await order({"collection": "DESC"}, {"definedAt": "ASC"}))[-1] == "z2"

        assert (await order({"kind": "ASC"}, {"definedAt": "ASC"}))[
            -1
        ] == "a1"  # FUNCTION < GENERATOR

    async def test_actions_are_grouped_by_availability_across_pages(self) -> None:
        (context, user, _client, organization), (other, *_) = await _contexts()

        def seed() -> dict[str, models.Action]:
            app = App.objects.create(identifier="lf-avail")
            actions = {
                key: _action(organization, app, f"lf-av-{key}")
                for key in ("none", "offline", "recent", "runnable")
            }
            now = timezone.now()
            agents = {
                "runnable": _agent(
                    organization, "lf-av-hook", kind=enums.AgentKind.WEBHOOK.value
                ),
                "recent": _agent(
                    organization,
                    "lf-av-recent",
                    connected=False,
                    last_seen=now - datetime.timedelta(hours=2),
                ),
                "offline": _agent(
                    organization,
                    "lf-av-gone",
                    connected=False,
                    last_seen=now - datetime.timedelta(days=3),
                ),
            }
            for key, holder in agents.items():
                models.Implementation.objects.create(
                    agent=holder, action=actions[key], interface=f"lf-av-{key}"
                )
            # An agent seen long ago besides the recent one does not make "recent" offline.
            models.Implementation.objects.create(
                agent=agents["offline"],
                action=actions["recent"],
                interface="lf-av-recent-2",
            )
            with (
                connection.cursor() as cursor
            ):  # takt's drain, which recomputes `assignable` on a hub
                cursor.execute("SELECT facade_refresh_live_implementations(500)")
            return actions

        actions = await sync_to_async(seed)()
        name = {str(action.pk): key for key, action in actions.items()}
        query = 'query($ordering: [ActionOrder!]!, $only: [ActionAvailability!]) { actions(filters: {search: "lf-av-", availability: $only}, ordering: $ordering, pagination: {limit: 2}) { id availability } }'

        async def page(
            ordering: list[dict[str, str]],
            only: list[str] | None = None,
            offset: int = 0,
        ) -> list[tuple[str, str]]:
            q = query.replace("limit: 2", f"limit: 2, offset: {offset}")
            rows = (await _run(q, context, ordering=ordering, only=only))["actions"]
            return [(name[row["id"]], row["availability"]) for row in rows]

        # Two pages of two: the groups come in order, so a page edge never splits one out of place.
        ordering = [{"availability": "ASC"}, {"definedAt": "ASC"}]
        assert await page(ordering) + await page(ordering, offset=2) == [
            ("runnable", "RUNNABLE"),
            ("recent", "RECENT"),
            ("offline", "OFFLINE"),
            ("none", "NONE"),
        ]
        assert [key for key, _ in await page([{"availability": "DESC"}])] == [
            "none",
            "offline",
        ]
        # A whole group, whatever page it starts on.
        assert await page(ordering, only=["OFFLINE", "NONE"]) == [
            ("offline", "OFFLINE"),
            ("none", "NONE"),
        ]
        # Another organization's agents and actions are not ours to rank.
        assert (await _run('{ actions(filters: {search: "lf-av-"}) { id } }', other))[
            "actions"
        ] == []


class TestBloks:
    async def test_a_blok_is_found_by_its_exact_name_and_bloks_by_what_they_say(
        self,
    ) -> None:
        (context, user, _client, organization), _ = await _contexts()

        def seed() -> tuple[models.Blok, models.Blok, models.MaterializedBlok]:
            module = models.Module.objects.get_or_create(
                name="default", organization=organization
            )[0]
            plot = models.Blok.objects.create(
                organization=organization,
                creator=user,
                module=module,
                name="lf-plot",
                description="draws a histogram",
                context="PANEL",
            )
            longer = models.Blok.objects.create(
                organization=organization,
                creator=user,
                module=module,
                name="lf-plot-3d",
                description="a surface",
                context="SCENE",
            )
            materialized = models.MaterializedBlok.objects.create(
                blok=plot, name="lf-plot", description=""
            )
            models.MaterializedBlok.objects.create(
                blok=longer, name="lf-plot-3d", description=""
            )
            return plot, longer, materialized

        plot, longer, materialized = await sync_to_async(seed)()

        exact = 'query { materializedBloks(filters: {blokName: "lf-plot"}) { id } }'
        assert [
            row["id"] for row in (await _run(exact, context))["materializedBloks"]
        ] == [str(materialized.pk)]

        bloks = "query($filters: BlokFilter) { bloks(filters: $filters) { id } }"

        async def ids(**filters: object) -> set[str]:
            return {
                row["id"]
                for row in (await _run(bloks, context, filters=filters))["bloks"]
            }

        assert await ids(search="HISTOGRAM") == {str(plot.pk)}
        assert await ids(search="lf-plot", contexts=["SCENE"]) == {str(longer.pk)}


class TestTasksByMember:
    async def test_a_persons_tasks_are_found_by_their_sub_whatever_client_they_used(
        self,
    ) -> None:
        (context, *_, organization), _ = await _contexts()
        runner = await agent(
            organization, "lf-runner", kind=enums.AgentKind.WEBHOOK.value
        )

        def seed() -> tuple[models.Task, models.Task]:
            def task_of(prefix: str) -> models.Task:
                # A person of the organization, calling through an app of their own.
                caller = models.Caller.objects.create(
                    user=User.objects.create(
                        username=f"{prefix}-user", password="x", sub=f"{prefix}-sub"
                    ),
                    client=Client.objects.create(client_id=f"{prefix}-client"),
                    organization=organization,
                )
                action = _action(
                    organization,
                    App.objects.create(identifier=f"{prefix}-app"),
                    f"{prefix}-key",
                )
                return models.Task.objects.create(
                    caller=caller,
                    action=action,
                    agent=runner,
                    latest_event_kind=enums.TaskEventKind.STARTED,
                    latest_instruct_kind=enums.TaskInstructChoices.ASSIGN,
                )

            return task_of("lf-member"), task_of("lf-stranger")

        mine, _ = await sync_to_async(seed)()
        query = 'query { tasks(filters: {user: "lf-member-sub"}) { id } }'
        assert [row["id"] for row in (await _run(query, context))["tasks"]] == [
            str(mine.pk)
        ]
