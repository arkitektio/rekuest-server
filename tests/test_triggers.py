"""Triggers, from this server's side: the rows its GraphQL creates, checked when written.

Receiving signals, matching them to triggers and firing them are takt's and tested there
(``takt/crates/facade/tests/scheduling.rs``, ``signal_intake.rs``). Real postgres.
"""


import pytest

from tests import registered

from facade import enums, models, triggers
from facade.service_agents import _identity

IDENTIFIER = "@mikro/arraydataset"
CHANNELS = "@mikro/n_channels"


@pytest.fixture
def mikro_service(settings):
    settings.SERVICE_AGENTS = [{"service": "mikro", "hook_url": "http://127.0.0.1:9/_rekuest/hook"}]
    return settings


def _org(slug: str):
    from authentikate.models import Organization

    return Organization.objects.get_or_create(slug=slug)[0]


def _target(prefix: str, org, *, needs_token: bool = False) -> models.Implementation:
    """An agent in ``org`` implementing an action whose ``image`` port takes a multi-channel dataset."""
    from facade.mutations.agent import ImplementAgentInputModel
    from rekuest_core.enums import ActionKind
    from rekuest_core.inputs.models import DefinitionInputModel, ImplementationInputModel

    user, client = _identity(prefix, org)
    definition = DefinitionInputModel(
        key=f"{prefix}-thumbnail",
        name="Thumbnail",
        kind=ActionKind.FUNCTION,
        args=[
            {"key": "image", "kind": "STRUCTURE", "identifier": IDENTIFIER, "requires": [{"key": CHANNELS, "operator": "GTE", "value": 2}]},
            {"key": "size", "kind": "INT", "nullable": True},
        ],
    )
    agent, _ = registered.implement_agent(client, user, org, ImplementAgentInputModel(name=prefix, implementations=[ImplementationInputModel(interface="thumbnail", definition=definition, needs_token=needs_token)]))
    models.Agent.objects.filter(pk=agent.pk).update(kind=enums.AgentKind.WEBHOOK.value, hook_url="http://127.0.0.1:9/hook", hook_url_secret="x")
    return models.Implementation.objects.select_related("action", "agent").get(agent=agent, interface="thumbnail")


def _trigger(impl: models.Implementation, *, conditions=None, owner_prefix: str = "owner") -> models.Trigger:
    user, client = _identity(f"{owner_prefix}-{impl.pk}", impl.agent.organization)
    caller, _ = models.Caller.objects.get_or_create(client=client, user=user, organization=impl.agent.organization)
    stored, compiled = triggers.compile_conditions(conditions or [])
    return models.Trigger.objects.create(
        name="thumbnail new images", caller=caller, kind="CREATED", identifier=IDENTIFIER, conditions=stored, compiled_jsonpath=compiled,
        action=impl.action, agent=impl.agent, interface=impl.interface, port="image", args={"size": 128},
    )


CREATE_TRIGGER = """
    mutation($input: CreateTriggerInput!) { createTrigger(input: $input) { id port kind identifier conditions } }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestTriggerGraphQL:
    async def test_create_validates_and_is_scoped(self, authenticated_context):
        from asgiref.sync import sync_to_async

        from facade.schema import schema
        from tests.factories import TEST_TOKEN
        from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context

        context_a, context_b = await sync_to_async(lambda: (tenant_context(TEST_TOKEN)[0], tenant_context(OTHER_TOKEN)[0]))()
        impl = await sync_to_async(_target)("gql-trigger", context_a.request.organization)
        await models.SignalDeclaration.objects.acreate(agent=impl.agent, identifier=IDENTIFIER, kind="CREATED", descriptor_keys=[CHANNELS])

        def create(port, conditions=None, kind="CREATED", identifier=IDENTIFIER):
            inp = {"name": "t", "kind": kind, "identifier": identifier, "action": str(impl.action_id), "port": port, "args": {"size": 64}}
            if conditions is not None:
                inp["conditions"] = conditions
            return schema.execute(CREATE_TRIGGER, variable_values={"input": inp}, context_value=context_a)

        undeclared_kind = await create("image", kind="DELETED")
        assert undeclared_kind.errors and "No service emits DELETED" in str(undeclared_kind.errors[0])
        undeclared_identifier = await create("image", identifier="@mikro/nothing")
        assert undeclared_identifier.errors
        unknown_key = await create("image", [{"key": "@mikro/n_chanels", "operator": "GTE", "value": 1}])
        assert unknown_key.errors and "@mikro/n_chanels" in str(unknown_key.errors[0])

        wrong_port = await create("size")
        assert wrong_port.errors and "not a @mikro/arraydataset structure" in str(wrong_port.errors[0])
        bad_condition = await create("image", [{"key": CHANNELS, "operator": "IN", "value": 3}])
        assert bad_condition.errors

        created = await create("image", [{"key": CHANNELS, "operator": "GTE", "value": 1}])
        assert created.errors is None, created.errors
        seen_by_a = await schema.execute("query { triggers { id } }", context_value=context_a)
        seen_by_b = await schema.execute("query { triggers { id } }", context_value=context_b)
        assert [t["id"] for t in seen_by_a.data["triggers"]] == [created.data["createTrigger"]["id"]]
        assert seen_by_b.data["triggers"] == []

        # Declarations are hub-wide: every tenant sees what the services emit.
        listed = await schema.execute("query { signalDeclarations { identifier kind descriptorKeys service } }", context_value=context_b)
        assert listed.errors is None, listed.errors
        assert listed.data["signalDeclarations"] == [{"identifier": IDENTIFIER, "kind": "CREATED", "descriptorKeys": [CHANNELS], "service": impl.agent.name}]
