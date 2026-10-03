from kante.channel import build_channel
from .channel_events import StateUpdateEvent, TaskEventCreatedEvent, ImplementationEvent, AgentEvent, ProbeEventBroadcast, ChildTaskEvent, PatchEvent, RuleFeedEvent, SignalFeedEvent


# takt publishes on these channels by name (``takt/crates/facade/src/channels.rs``): every
# name is spelled out here, so renaming a payload class cannot silently change one.
agent_updated_channel = build_channel(AgentEvent, "agent_updated_broadcast")

task_event_channel = build_channel(TaskEventCreatedEvent, "TaskEventCreatedEvent")

# Same payload model, but explicitly distinct names: unnamed same-model channels share a
# message type in kante, so a future group-name overlap would silently cross-feed them.
child_task_channel = build_channel(ChildTaskEvent, "child_task_feed")

agent_task_channel = build_channel(ChildTaskEvent, "agent_task_feed")


new_implementation_channel = build_channel(ImplementationEvent, "ImplementationEvent")


patch_channel = build_channel(PatchEvent, "PatchEvent")

state_update_channel = build_channel(StateUpdateEvent, "StateUpdateEvent")

probe_event_channel = build_channel(ProbeEventBroadcast, "probe_event_broadcast")

# Automation. Signals are takt's rows (it publishes); rules are written by both sides.
signal_channel = build_channel(SignalFeedEvent, "signal_feed")

rule_channel = build_channel(RuleFeedEvent, "rule_feed")
