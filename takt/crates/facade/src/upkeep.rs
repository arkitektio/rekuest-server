//! The server's upkeep job, on takt's clock (the Python server's `facade/upkeep.py`).
//!
//! One periodic job needs the Python server: provisioning this hub's services (their manifests
//! are registered through its models). The server does not loop; takt asks for it when it is
//! due:
//!
//! | job         | due                                                        |
//! |-------------|------------------------------------------------------------|
//! | `provision` | at start, then every 5 minutes; 30 s after a failed pass   |
//!
//! The request is `POST {server_url}/_rekuest/upkeep/<job>`, signed with the instance key as a
//! service token from rekuest to itself: the mirror of the internal API the server calls here.
//!
//! Any number of replicas run this. When a job is next due is a redis key that expires then,
//! taken by whichever replica asks first, so no replica holds a deadline of its own and a
//! replica dying costs nothing. If redis is unreachable every replica asks: wasteful, still
//! correct, since the server serializes a provisioning pass.

use std::sync::OnceLock;
use std::time::Duration;

use serde_json::Value;

use crate::settings::Settings;
use crate::{redis_keys, service_trust, Context};

/// One upkeep job and its cadence.
#[derive(Debug, Clone, Copy)]
pub struct Job {
    pub name: &'static str,
    /// How long after a pass that did everything the next one is due.
    pub every: Duration,
    /// How long after a pass that failed (or could not be asked for).
    pub retry: Duration,
}

pub const PROVISION: Job = Job {
    name: "provision",
    every: Duration::from_secs(300),
    retry: Duration::from_secs(30),
};

/// How often a replica looks whether a job is due.
const POLL: Duration = Duration::from_secs(10);
/// A provisioning pass fetches every service's manifest.
const TIMEOUT: Duration = Duration::from_secs(120);

fn client() -> &'static reqwest::Client {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .timeout(TIMEOUT)
            .build()
            .expect("an HTTP client")
    })
}

/// What a pass came to.
#[derive(Debug, Clone, PartialEq)]
pub enum Outcome {
    /// Everything was done (or another replica's pass is doing it).
    Done,
    /// The server could not be asked, or said the pass failed.
    Failed(String),
}

/// Ask the server to run `job` once; its JSON answer.
pub async fn call(settings: &Settings, job: &str) -> Result<Value, String> {
    let base = settings
        .server_url
        .as_deref()
        .ok_or("rekuest.server_url is not configured")?;
    let key = settings
        .instance_key
        .as_deref()
        .ok_or("no instance key configured")?;
    let url = format!("{}/_rekuest/upkeep/{job}", base.trim_end_matches('/'));
    let path = reqwest::Url::parse(&url)
        .map_err(|e| format!("{url}: {e}"))?
        .path()
        .to_owned();
    let body = b"{}";
    let me = &settings.rekuest_identifier;
    let authorization = service_trust::sign(key, "POST", &path, body, me, me);
    let response = client()
        .post(&url)
        .header("Content-Type", "application/json")
        .header("Authorization", authorization)
        .body(body.to_vec())
        .send()
        .await
        .map_err(|e| format!("the server is unreachable at {url}: {e}"))?;
    let status = response.status();
    let answer: Value = response.json().await.unwrap_or(Value::Null);
    if !status.is_success() {
        let message = answer.get("error").and_then(Value::as_str).unwrap_or("");
        return Err(format!("the server refused {url} ({status}): {message}"));
    }
    Ok(answer)
}

/// Read the server's answer to `job`.
fn outcome(answer: &Value) -> Outcome {
    if answer.get("ok").and_then(Value::as_bool) == Some(false) {
        let failed = answer
            .get("failed")
            .map(Value::to_string)
            .unwrap_or_default();
        return Outcome::Failed(format!("could not provision {failed}"));
    }
    Outcome::Done
}

fn due_key(settings: &Settings, job: &Job) -> String {
    redis_keys::key(settings, &[&"upkeep", &job.name])
}

/// Take `job` if it is due: the key is absent. Held for `TIMEOUT` so a pass in flight is not
/// asked for twice; [`rest`] then says when it is next due. Any redis problem: take it.
pub async fn take(ctx: &Context, job: &Job) -> bool {
    let mut redis = ctx.redis.clone();
    let taken: redis::RedisResult<Option<String>> = redis::cmd("SET")
        .arg(due_key(&ctx.settings, job))
        .arg(1)
        .arg("NX")
        .arg("PX")
        .arg(TIMEOUT.as_millis() as u64)
        .query_async(&mut redis)
        .await;
    match taken {
        Ok(taken) => taken.is_some(),
        Err(e) => {
            tracing::debug!("upkeep token unavailable; asking anyway: {e}");
            true
        }
    }
}

/// `job` is not due again for `duration`, for any replica.
async fn rest(ctx: &Context, job: &Job, duration: Duration) {
    let mut redis = ctx.redis.clone();
    let set: redis::RedisResult<()> = redis::cmd("SET")
        .arg(due_key(&ctx.settings, job))
        .arg(1)
        .arg("PX")
        .arg((duration.as_millis() as u64).max(1))
        .query_async(&mut redis)
        .await;
    if let Err(e) = set {
        tracing::debug!("could not note when {} is next due: {e}", job.name);
    }
}

/// Run `job` now and note when it is next due; what it came to.
pub async fn run(ctx: &Context, job: &Job) -> Outcome {
    let result = match call(&ctx.settings, job.name).await {
        Ok(answer) => {
            tracing::debug!("upkeep {}: {answer}", job.name);
            outcome(&answer)
        }
        Err(e) => Outcome::Failed(e),
    };
    match &result {
        Outcome::Failed(why) => {
            tracing::warn!(
                "Upkeep {} failed, again in {:?}: {why}",
                job.name,
                job.retry
            );
            rest(ctx, job, job.retry).await;
        }
        Outcome::Done => rest(ctx, job, job.every).await,
    }
    result
}

/// Keep one job: at start whatever the key says (a restart is when a manifest changed), then
/// whenever it is due and this replica is the one to take it.
async fn keep(ctx: Context, job: Job) {
    run(&ctx, &job).await;
    loop {
        tokio::time::sleep(POLL).await;
        if take(&ctx, &job).await {
            run(&ctx, &job).await;
        }
    }
}

/// Keep the job, forever. Never inside the reaper's pass: a slow server must not hold a
/// deadline sweep back.
pub async fn run_forever(ctx: Context) {
    if ctx.settings.server_url.is_none() || ctx.settings.instance_key.is_none() {
        tracing::warn!("Upkeep is off: it needs rekuest.server_url and the instance key");
        return;
    }
    let jitter = uuid::Uuid::new_v4().as_u128() % 500;
    tokio::time::sleep(Duration::from_millis(jitter as u64)).await;
    keep(ctx, PROVISION).await;
}
