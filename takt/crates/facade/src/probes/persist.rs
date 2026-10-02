//! Agent events of probes: the redis twin of the task persistence (`facade/probes/persist.py`).
//!
//! The router sends every frame whose task is a probe id here. Nothing touches SQL: transitions
//! land in the probe's hash (`store`) and the event is fanned out payload-carrying on
//! `probe_event_broadcast`. Terminal dedup is `HSETNX done`: agents resend terminal reports until
//! acked, and exactly one claim wins.

use serde_json::Value;

use crate::channel_events::ProbeEventBroadcast;
use crate::channels;
use crate::context::Context;
use crate::probes::store;

/// The topic of one probe's own stream (`probe_events_topic`).
pub fn probe_events_topic(probe: &str) -> String {
    format!("probe_events_{probe}")
}

/// Where one probe event goes (`probe_topics`): its own stream, and for a probe an agent fired,
/// the requester's `task_caller_{caller}` group too, which its socket joined at registration.
pub fn probe_topics(probe: &str, caller: Option<&str>, origin: &str) -> Vec<String> {
    let mut topics = vec![probe_events_topic(probe)];
    if let Some(caller) = caller.filter(|c| origin == "agent" && !c.is_empty()) {
        topics.push(format!("task_caller_{caller}"));
    }
    topics
}

/// The event's fields besides its probe, kind and seq.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ProbeEvent {
    pub message: Option<String>,
    pub progress: Option<i64>,
    pub returns: Option<Value>,
    pub level: Option<String>,
}

/// Broadcast one probe event (`_publish`). Best effort, like every feed.
pub async fn publish(
    ctx: &Context,
    probe: &str,
    kind: &str,
    seq: i64,
    event: ProbeEvent,
    caller: Option<&str>,
    origin: &str,
) {
    let payload = ProbeEventBroadcast {
        probe: probe.to_owned(),
        kind: kind.to_owned(),
        seq,
        message: event.message,
        level: event.level,
        progress: event.progress,
        returns: event.returns,
        created_at: chrono::Utc::now(),
    };
    let topics = probe_topics(probe, caller, origin);
    if let Err(e) =
        kante::channel::broadcast(&ctx.channel_layer, channels::PROBE_EVENT, &payload, &topics)
            .await
    {
        tracing::error!(probe, "probe event broadcast failed: {e}");
    }
}

/// A non-terminal report (`_nonterminal`): recorded and published; dropped for an unknown,
/// expired or finished probe.
pub async fn nonterminal(
    ctx: &Context,
    probe: &str,
    kind: &str,
    event: ProbeEvent,
) -> redis::RedisResult<()> {
    let Some((seq, caller, origin)) =
        store::record_nonterminal(ctx, probe, kind, event.returns.as_ref()).await?
    else {
        return Ok(());
    };
    publish(ctx, probe, kind, seq, event, caller.as_deref(), &origin).await;
    Ok(())
}

/// A terminal report (`_terminal`): only the claim's winner publishes.
pub async fn terminal(
    ctx: &Context,
    probe: &str,
    kind: &str,
    error: Option<&str>,
) -> redis::RedisResult<()> {
    let Some((seq, state)) = store::claim_terminal(ctx, probe, kind, error).await? else {
        return Ok(());
    };
    publish(
        ctx,
        probe,
        kind,
        seq,
        ProbeEvent {
            message: error.map(str::to_owned),
            ..ProbeEvent::default()
        },
        state.get("caller").map(String::as_str),
        state.get("origin").map_or("graphql", String::as_str),
    )
    .await;
    Ok(())
}

/// CRITICAL every live probe of a dead agent, at once (`fail_all_for_agent`): hover-grade work is
/// worthless without its executor, so no grace window applies. Winners only; returns how many.
pub async fn fail_all_for_agent(ctx: &Context, agent: i64) -> redis::RedisResult<usize> {
    let probes = store::live_calls_for_agent(ctx, agent).await?;
    let mut failed = 0;
    for probe in &probes {
        let Some((seq, state)) =
            store::claim_terminal(ctx, probe, "CRITICAL", Some("Agent disconnected")).await?
        else {
            continue;
        };
        publish(
            ctx,
            probe,
            "CRITICAL",
            seq,
            ProbeEvent {
                message: Some("Agent disconnected".into()),
                ..ProbeEvent::default()
            },
            state.get("caller").map(String::as_str),
            state.get("origin").map_or("graphql", String::as_str),
        )
        .await;
        failed += 1;
    }
    // Only the ids looked at: a reconnected agent's new probes stay indexed.
    store::forget_agent_calls(ctx, agent, &probes).await?;
    Ok(failed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn agent_probes_mirror_onto_the_callers_topic() {
        assert_eq!(
            probe_topics("p-1", Some("7"), "graphql"),
            vec!["probe_events_p-1"]
        );
        assert_eq!(
            probe_topics("p-1", Some("7"), "agent"),
            vec!["probe_events_p-1", "task_caller_7"]
        );
    }
}
