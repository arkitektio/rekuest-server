"""HookAgents — the agent protocol over HTTP POST.

Covers the signing helper, outbound delivery branching (broadcast → POST for WEBHOOK),
the caller-event callback, and the HTTP intake (HMAC-verified, routed through the shared
dispatcher). httpx is stubbed so no real network calls happen.
"""

import json

import pytest
from asgiref.sync import sync_to_async
from django.test import RequestFactory

from facade import enums, hooks, messages
from facade.consumers.async_consumer import AgentConsumer
from facade.http_intake import hook_intake
from facade.models import Task, TaskEvent

from tests.factories import (
    _build_task,
    _build_webhook_agent,
    build_task,
    build_implementation_for_agent,
    build_webhook_agent,
)


class _FakeResp:
    status_code = 200

    def raise_for_status(self):
        return None


class _Recorder:
    """Captures outbound POSTs in place of httpx."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, content=None, headers=None):
        self.calls.append({"url": url, "content": content, "headers": headers or {}})
        return _FakeResp()


@pytest.fixture
def post_recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(hooks._client, "post", rec)
    return rec


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #
def test_sign_verify_roundtrip_and_tamper():
    body = b'{"hello": 1}'
    sig = hooks.sign("secret", body)
    assert hooks.verify("secret", body, sig) is True
    assert hooks.verify("secret", b'{"hello": 2}', sig) is False  # body tampered
    assert hooks.verify("wrong", body, sig) is False  # wrong secret
    assert hooks.verify("secret", body, None) is False
    assert hooks.verify(None, body, sig) is False


# --------------------------------------------------------------------------- #
# Outbound delivery branch
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
def test_broadcast_to_webhook_posts_signed(post_recorder):
    agent = _build_webhook_agent("hook-out", secret="topsecret", hook_url="https://hook.example/in")
    AgentConsumer.broadcast(str(agent.pk), messages.Cancel(task="ass-1"))

    assert len(post_recorder.calls) == 1
    call = post_recorder.calls[0]
    assert call["url"] == "https://hook.example/in"
    body = call["content"]
    assert json.loads(body)["type"] == messages.ToAgentMessageType.CANCEL.value
    # Signed with the agent's secret — the replay-protected V1 header, plus the legacy one
    # while the compatibility window is open.
    timestamp, digest = hooks.parse_v1(call["headers"][hooks.SIGNATURE_V1_HEADER])
    assert digest == hooks.sign("topsecret", hooks.signed_payload_v1(agent.pk, timestamp, body))
    assert call["headers"][hooks.SIGNATURE_HEADER] == hooks.sign("topsecret", body)


# --------------------------------------------------------------------------- #
# Caller-event callback (signal → POST)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
def test_caller_event_is_posted_to_webhook_caller(post_recorder):
    from facade.models import Caller

    agent = _build_webhook_agent("hook-cb")
    # A task whose caller is this webhook agent's identity.
    caller = Caller.objects.create(client=agent.client, user=agent.user, organization=agent.organization)
    ass = _build_task("hook-cb-ass")
    ass.caller = caller
    ass.save(update_fields=["caller"])

    TaskEvent.objects.create(task=ass, kind=enums.TaskEventKind.PROGRESS, progress=42)

    assert any(json.loads(c["content"]).get("type") == messages.ToAgentMessageType.PROGRESS_EVENT.value for c in post_recorder.calls)


# --------------------------------------------------------------------------- #
# HTTP intake
# --------------------------------------------------------------------------- #
def _header(name: str) -> str:
    return f"HTTP_{name.upper().replace('-', '_')}"


def _signed_request(agent, message, *, body=None, timestamp=None, agent_id=None):
    """A V1-signed intake request. ``agent_id``/``timestamp`` are overridable to forge one."""
    body = message.model_dump_json().encode("utf-8") if body is None else body
    signature = hooks.sign_v1(agent.hook_url_secret, agent.pk if agent_id is None else agent_id, body, timestamp=timestamp)
    return RequestFactory().post(
        f"/agi/http/{agent.pk}",
        data=body,
        content_type="application/json",
        **{_header(hooks.SIGNATURE_V1_HEADER): signature},
    )


def _legacy_signed_request(agent, message):
    """The pre-V1 body-only signature, which ``compat`` still accepts."""
    body = message.model_dump_json().encode("utf-8")
    return RequestFactory().post(
        f"/agi/http/{agent.pk}",
        data=body,
        content_type="application/json",
        **{_header(hooks.SIGNATURE_HEADER): hooks.sign(agent.hook_url_secret, body)},
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestHookIntake:
    async def test_sub_assign_over_http(self, post_recorder):
        # A HookAgent assigns dependent work over HTTP — same rule as the socket: ``parent``
        # is required, since roots come only from the GraphQL assign mutation.
        agent = await build_webhook_agent("hook-in-ca", secret="sek")
        impl = await build_implementation_for_agent(agent.pk, "hook-in-ca")
        parent = await build_task("hook-in-ca-parent")

        msg = messages.AssignRequest(reference="hr-1", implementation=str(impl.pk), parent=str(parent.pk), args={"x": 1})
        response = await hook_intake(_signed_request(agent, msg), str(agent.pk))

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["type"] == messages.ToAgentMessageType.ASSIGN_RESPONSE.value
        assert data["reference"] == "hr-1" and data["created"] is True and data["task"]
        assert await Task.objects.filter(reference="hr-1").acount() == 1

    async def test_bad_signature_is_rejected(self, post_recorder):
        agent = await build_webhook_agent("hook-in-bad", secret="sek")
        body = messages.Completed(task="x").model_dump_json().encode("utf-8")
        request = RequestFactory().post(
            f"/agi/http/{agent.pk}",
            data=body,
            content_type="application/json",
            **{_header(hooks.SIGNATURE_V1_HEADER): "t=1,v1=deadbeef"},
        )
        response = await hook_intake(request, str(agent.pk))
        assert response.status_code == 401

    async def test_done_event_over_http_acks_and_persists(self, post_recorder):
        agent = await build_webhook_agent("hook-in-done", secret="sek")
        ass = await build_task("hook-in-done-ass", agent_pk=agent.pk)

        msg = messages.Completed(task=str(ass.pk), seq=3)
        response = await hook_intake(_signed_request(agent, msg), str(agent.pk))

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["type"] == messages.ToAgentMessageType.EVENT_ACK.value
        assert data["event"] == msg.id
        refreshed = await Task.objects.aget(pk=ass.pk)
        assert refreshed.is_done is True


# --------------------------------------------------------------------------- #
# Connectivity — a webhook agent is selectable despite connected=False
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_action_assign_selects_webhook_agent(post_recorder):
    from facade.backend import controll_backend, agent_is_available
    from facade.caller_context import CallerContext
    from facade import inputs

    agent = await build_webhook_agent("hook-action", secret="sek")
    impl = await build_implementation_for_agent(agent.pk, "hook-action")

    assert agent_is_available(agent) is True  # webhook agent, despite connected=False

    ctx = CallerContext.from_agent(agent)
    action_id = str(impl.action_id)
    task = await sync_to_async(controll_backend.assign)(ctx, inputs.AssignInputModel(action=action_id, args={}))
    assert str(task.agent_id) == str(agent.pk)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestHookIntakeIsReplaySafe:
    """A websocket has a session; an HTTP request has to stand alone. A captured body signed with
    the old body-only scheme was replayable forever, against any backend — and an ordinary retry
    was indistinguishable from an attack, which is what fed the assign double-execution bug."""

    async def test_a_replayed_report_is_acked_but_not_persisted_twice(self, post_recorder, agent_ws_redis):
        agent = await build_webhook_agent("hook-replay-report", secret="sek")
        task = await build_task("hook-replay-report-task", agent_pk=agent.pk)
        msg = messages.Progress(task=str(task.pk), progress=10)
        request = _signed_request(agent, msg)

        first = await hook_intake(request, str(agent.pk))
        replay = await hook_intake(_signed_request(agent, msg, body=request.body, timestamp=hooks.parse_v1(request.headers[hooks.SIGNATURE_V1_HEADER])[0]), str(agent.pk))

        assert first.status_code == 200 and replay.status_code == 200
        # Dropping the duplicate IS the handling: exactly one PROGRESS event exists.
        assert await TaskEvent.objects.filter(task=task, kind=enums.TaskEventKind.PROGRESS).acount() == 1

    async def test_a_replayed_assign_request_still_answers_with_the_same_task(self, post_recorder, agent_ws_redis):
        """It must be routed again, not refused: the sender needs its ``AssignResponse``, and the
        unique constraint on (caller, reference) makes re-routing return the original task."""
        agent = await build_webhook_agent("hook-replay-assign", secret="sek")
        impl = await build_implementation_for_agent(agent.pk, "hook-replay-assign")
        parent = await build_task("hook-replay-assign-parent")
        msg = messages.AssignRequest(reference="hr-replay", implementation=str(impl.pk), parent=str(parent.pk), args={})
        request = _signed_request(agent, msg)
        timestamp = hooks.parse_v1(request.headers[hooks.SIGNATURE_V1_HEADER])[0]

        first = json.loads((await hook_intake(request, str(agent.pk))).content)
        replay = json.loads((await hook_intake(_signed_request(agent, msg, body=request.body, timestamp=timestamp), str(agent.pk))).content)

        assert first["created"] is True and replay["created"] is False
        assert replay["task"] == first["task"]
        assert await Task.objects.filter(reference="hr-replay").acount() == 1

    async def test_a_replayed_control_request_is_refused_not_acked(self, post_recorder, agent_ws_redis):
        """A cancel is an instruction, not a fact: acking one the server did not apply is a lie."""
        agent = await build_webhook_agent("hook-replay-cancel", secret="sek")
        from facade.models import Caller

        caller = await Caller.objects.acreate(client=agent.client, user=agent.user, organization=agent.organization)
        task = await build_task("hook-replay-cancel-task", agent_pk=agent.pk)
        await Task.objects.filter(pk=task.pk).aupdate(caller=caller)

        msg = messages.CancelRequest(task=str(task.pk))
        request = _signed_request(agent, msg)
        timestamp = hooks.parse_v1(request.headers[hooks.SIGNATURE_V1_HEADER])[0]

        first = await hook_intake(request, str(agent.pk))
        replay = await hook_intake(_signed_request(agent, msg, body=request.body, timestamp=timestamp), str(agent.pk))

        assert first.status_code == 200
        assert replay.status_code == 409

    async def test_a_stale_request_is_rejected_even_though_it_is_signed(self, post_recorder, agent_ws_redis, settings):
        """The timestamp is inside the signed payload, so it cannot be edited — it is what bounds
        how long a captured request stays usable."""
        settings.HOOK_MAX_SKEW = 60
        agent = await build_webhook_agent("hook-stale", secret="sek")
        task = await build_task("hook-stale-task", agent_pk=agent.pk)
        import time

        msg = messages.Completed(task=str(task.pk))
        response = await hook_intake(_signed_request(agent, msg, timestamp=int(time.time()) - 3600), str(agent.pk))

        assert response.status_code == 401
        assert (await Task.objects.aget(pk=task.pk)).is_done is False

    async def test_a_request_signed_for_another_agent_is_rejected(self, post_recorder, agent_ws_redis):
        """Two HookAgents can share a secret; binding the agent id into the payload stops a body
        signed for one from being replayed against the other."""
        victim = await build_webhook_agent("hook-victim", secret="shared")
        other = await build_webhook_agent("hook-other", secret="shared")
        task = await build_task("hook-victim-task", agent_pk=victim.pk)

        msg = messages.Completed(task=str(task.pk))
        response = await hook_intake(_signed_request(victim, msg, agent_id=other.pk), str(victim.pk))

        assert response.status_code == 401

    async def test_legacy_signature_is_accepted_in_compat_and_refused_in_strict(self, post_recorder, agent_ws_redis, settings):
        agent = await build_webhook_agent("hook-compat", secret="sek")
        task = await build_task("hook-compat-task", agent_pk=agent.pk)
        msg = messages.Progress(task=str(task.pk), progress=1)

        settings.HOOK_SIGNATURE_MODE = "compat"
        assert (await hook_intake(_legacy_signed_request(agent, msg), str(agent.pk))).status_code == 200

        settings.HOOK_SIGNATURE_MODE = "strict"
        assert (await hook_intake(_legacy_signed_request(agent, msg), str(agent.pk))).status_code == 401

    async def test_strict_mode_sends_only_the_replay_protected_header(self, post_recorder, settings):
        settings.HOOK_SIGNATURE_MODE = "strict"
        agent = await sync_to_async(_build_webhook_agent)("hook-strict-out", secret="sek")
        await sync_to_async(AgentConsumer.broadcast)(str(agent.pk), messages.Cancel(task="t-1"))

        headers = post_recorder.calls[-1]["headers"]
        assert hooks.SIGNATURE_V1_HEADER in headers
        assert hooks.SIGNATURE_HEADER not in headers
