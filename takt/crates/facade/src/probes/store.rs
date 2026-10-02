//! Redis-held probe state (`facade/probes/store.py`).
//!
//! One hash per probe plus two indexes, on the redis the agent queues use:
//!
//! ```text
//! {prefix}:probe:{id}                 HASH  agent, caller, user, org, action, impl, iface, ref,
//!                                            kind, seq, origin, created, done, last_returns, err
//! {prefix}:probe-agent:{agent}        SET   live probe ids (fail fast when the agent dies)
//! {prefix}:probe-inflight:{caller}    STR   in-flight counter (per-caller backpressure)
//! ```
//!
//! The hash is also the concurrency primitive `select_for_update` is for tasks: the terminal
//! transition is claimed with `HSETNX done`, one winner across every process. `seq` is a
//! per-probe `HINCRBY`, the order and dedup key a task event's id is. Everything expires: the
//! TTL while live (refreshed on every write), the linger once terminal. Expiry is the collector.

use std::collections::HashMap;

use redis::AsyncCommands;
use serde_json::Value;

use crate::context::Context;
use crate::redis_keys;

pub type ProbeState = HashMap<String, String>;

fn call_key(ctx: &Context, probe: &str) -> String {
    redis_keys::key(&ctx.settings, &[&"probe", &probe])
}

fn agent_index_key(ctx: &Context, agent: &str) -> String {
    redis_keys::key(&ctx.settings, &[&"probe-agent", &agent])
}

fn inflight_key(ctx: &Context, caller: &str) -> String {
    redis_keys::key(&ctx.settings, &[&"probe-inflight", &caller])
}

fn ttl(ctx: &Context) -> i64 {
    ctx.settings.probe_ttl.as_secs() as i64
}

/// Take one in-flight slot for the caller, or refuse at the cap (`try_acquire_slot`). The
/// counter expires with the TTL, so a crashed handler cannot wedge a caller's budget for good.
pub async fn try_acquire_slot(ctx: &Context, caller: i64) -> redis::RedisResult<bool> {
    let mut redis = ctx.redis.clone();
    let key = inflight_key(ctx, &caller.to_string());
    let (count, _): (i64, i64) = redis::pipe()
        .atomic()
        .incr(&key, 1)
        .expire(&key, ttl(ctx))
        .query_async(&mut redis)
        .await?;
    if count > ctx.settings.probe_max_inflight {
        let _: i64 = redis.decr(&key, 1).await?;
        return Ok(false);
    }
    Ok(true)
}

/// Give the slot back when a create failed after acquiring it (`release_slot_sync`).
pub async fn release_slot(ctx: &Context, caller: i64) -> redis::RedisResult<()> {
    let mut redis = ctx.redis.clone();
    let _: i64 = redis
        .decr(inflight_key(ctx, &caller.to_string()), 1)
        .await?;
    Ok(())
}

/// What a probe is created with (`create`'s arguments).
#[derive(Debug, Clone)]
pub struct NewProbe<'a> {
    pub agent: i64,
    pub caller: i64,
    pub user_sub: &'a str,
    pub org_slug: &'a str,
    pub action: i64,
    pub implementation: i64,
    pub interface: &'a str,
    pub reference: Option<&'a str>,
    /// Who fired it: `graphql`, or `agent` (its events mirror onto the requester's caller topic).
    pub origin: &'a str,
}

/// Write the probe's hash and index it under its agent (`create`).
pub async fn create(
    ctx: &Context,
    probe: &str,
    new: &NewProbe<'_>,
) -> redis::RedisResult<ProbeState> {
    let state: ProbeState = [
        ("agent", new.agent.to_string()),
        ("caller", new.caller.to_string()),
        ("user", new.user_sub.to_owned()),
        ("org", new.org_slug.to_owned()),
        ("action", new.action.to_string()),
        ("impl", new.implementation.to_string()),
        ("iface", new.interface.to_owned()),
        ("ref", new.reference.unwrap_or_default().to_owned()),
        ("kind", "QUEUED".to_owned()),
        ("seq", "0".to_owned()),
        ("origin", new.origin.to_owned()),
        (
            "created",
            chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false),
        ),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_owned(), v))
    .collect();
    let key = call_key(ctx, probe);
    let fields: Vec<(&String, &String)> = state.iter().collect();
    let mut redis = ctx.redis.clone();
    let _: () = redis::pipe()
        .atomic()
        .hset_multiple(&key, &fields)
        .ignore()
        .expire(&key, ttl(ctx))
        .ignore()
        .sadd(agent_index_key(ctx, &new.agent.to_string()), probe)
        .ignore()
        .query_async(&mut redis)
        .await?;
    Ok(state)
}

/// The probe's state, or `None` once it expired (`get`).
pub async fn get(ctx: &Context, probe: &str) -> redis::RedisResult<Option<ProbeState>> {
    let mut redis = ctx.redis.clone();
    let state: ProbeState = redis.hgetall(call_key(ctx, probe)).await?;
    Ok((!state.is_empty()).then_some(state))
}

/// Record a non-terminal event (`record_nonterminal`): `(seq, caller, origin)`, or `None` for an
/// unknown, expired or already finished probe (the event is dropped: nobody is listening).
pub async fn record_nonterminal(
    ctx: &Context,
    probe: &str,
    kind: &str,
    returns: Option<&Value>,
) -> redis::RedisResult<Option<(i64, Option<String>, String)>> {
    let mut redis = ctx.redis.clone();
    let key = call_key(ctx, probe);
    // EXISTS first, so an expired probe is not resurrected as a stub by HINCRBY.
    let done: Option<String> = redis.hget(&key, "done").await?;
    if done.as_deref().is_some_and(|d| !d.is_empty()) {
        return Ok(None);
    }
    if done.is_none() && !redis.exists::<_, bool>(&key).await? {
        return Ok(None);
    }
    let mut pipe = redis::pipe();
    pipe.atomic()
        .hincr(&key, "seq", 1)
        .hset(&key, "kind", kind)
        .ignore();
    if let Some(returns) = returns {
        pipe.hset(&key, "last_returns", returns.to_string())
            .ignore();
    }
    pipe.expire(&key, ttl(ctx))
        .ignore()
        .cmd("HMGET")
        .arg(&key)
        .arg("caller")
        .arg("origin");
    let (seq, fields): (i64, Vec<Option<String>>) = pipe.query_async(&mut redis).await?;
    let caller = fields.first().cloned().flatten();
    let origin = fields
        .get(1)
        .cloned()
        .flatten()
        .unwrap_or_else(|| "graphql".into());
    Ok(Some((seq, caller, origin)))
}

/// Claim the terminal transition (`claim_terminal`): `(seq, state)` for the one winner, `None`
/// for a resent terminal report and for an unknown or expired probe.
pub async fn claim_terminal(
    ctx: &Context,
    probe: &str,
    kind: &str,
    error: Option<&str>,
) -> redis::RedisResult<Option<(i64, ProbeState)>> {
    let mut redis = ctx.redis.clone();
    let key = call_key(ctx, probe);
    if !redis.exists::<_, bool>(&key).await? {
        return Ok(None);
    }
    if !redis.hset_nx::<_, _, _, bool>(&key, "done", kind).await? {
        return Ok(None);
    }
    let mut pipe = redis::pipe();
    pipe.atomic()
        .hincr(&key, "seq", 1)
        .hset(&key, "kind", kind)
        .ignore();
    if let Some(error) = error {
        pipe.hset(&key, "err", error).ignore();
    }
    pipe.expire(&key, ctx.settings.probe_linger.as_secs() as i64)
        .ignore()
        .hgetall(&key);
    let (seq, state): (i64, ProbeState) = pipe.query_async(&mut redis).await?;
    let _: i64 = redis
        .srem(
            agent_index_key(
                ctx,
                state.get("agent").map(String::as_str).unwrap_or_default(),
            ),
            probe,
        )
        .await?;
    if let Some(caller) = state.get("caller").filter(|c| !c.is_empty()) {
        let _: i64 = redis.decr(inflight_key(ctx, caller), 1).await?;
    }
    Ok(Some((seq, state)))
}

/// The live probes of an agent, sorted (`live_calls_for_agent`).
pub async fn live_calls_for_agent(ctx: &Context, agent: i64) -> redis::RedisResult<Vec<String>> {
    let mut redis = ctx.redis.clone();
    let mut probes: Vec<String> = redis
        .smembers(agent_index_key(ctx, &agent.to_string()))
        .await?;
    probes.sort();
    Ok(probes)
}

/// Remove exactly these ids from the agent's index (`forget_agent_calls`), never the whole set:
/// a reconnected agent may have registered new probes in it meanwhile.
pub async fn forget_agent_calls(
    ctx: &Context,
    agent: i64,
    probes: &[String],
) -> redis::RedisResult<()> {
    if probes.is_empty() {
        return Ok(());
    }
    let mut redis = ctx.redis.clone();
    let _: i64 = redis
        .srem(agent_index_key(ctx, &agent.to_string()), probes)
        .await?;
    Ok(())
}
