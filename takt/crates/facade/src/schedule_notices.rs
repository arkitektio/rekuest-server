//! What the rekuest server says about its schedules, heard on Postgres `NOTIFY`.
//!
//! The server owns the schedule rows. When it writes one it says so on [`CHANNEL`], inside the
//! transaction that writes it (`facade/schedules.py`): Postgres delivers a notice when that
//! transaction commits and never if it rolls back, so takt only ever hears of rows it can read.
//! A notice asks for nothing back; what it changes shows on the rule feed.
//!
//! ```json
//! {"id": "<uuid>", "schedule": 7, "replan": true, "caller": 3}
//! {"id": "<uuid>", "run": 41, "caller": 3}
//! ```
//!
//! The first plans the schedule's next run now rather than on the reaper's next tick; `replan`
//! says its timing, target or switch changed, so the waiting run goes first. The second is a
//! schedule that was deleted: its waiting run, named by the server because the row that led to it
//! is gone. `caller` is who the run is cancelled as (none: the server itself).
//!
//! Every replica hears every notice; the `id` is claimed in redis so one of them acts. A notice
//! sent while no replica listens is lost. For a new or re-enabled schedule that costs one tick:
//! the reaper plans it. A lost `replan` or deleted schedule leaves the waiting run of the old
//! terms to run once.

use std::time::Duration;

use serde::Deserialize;
use sqlx::postgres::PgListener;

use crate::context::Context;
use crate::redis_keys;
use crate::schedules;

/// The channel the server notifies on (`facade/schedules.py`).
pub const CHANNEL: &str = "rekuest_schedule";

/// How long a notice's id stays claimed: long enough for every replica to have heard it.
const CLAIM_SECONDS: u64 = 60;

#[derive(Debug, Deserialize)]
struct Notice {
    id: String,
    #[serde(default)]
    schedule: Option<i64>,
    #[serde(default)]
    run: Option<i64>,
    #[serde(default)]
    replan: bool,
    #[serde(default)]
    caller: Option<i64>,
}

/// Whether this replica acts on the notice: the first to claim its id. Any redis problem: act
/// anyway (planning is safe from any number of replicas; only a `replan` is not).
async fn claim(ctx: &Context, id: &str) -> bool {
    let key = redis_keys::key(&ctx.settings, &[&"schedule-notice", &id]);
    let mut redis = ctx.redis.clone();
    let claimed: redis::RedisResult<Option<String>> = redis::cmd("SET")
        .arg(key)
        .arg(1)
        .arg("NX")
        .arg("EX")
        .arg(CLAIM_SECONDS)
        .query_async(&mut redis)
        .await;
    match claimed {
        Ok(claimed) => claimed.is_some(),
        Err(e) => {
            tracing::debug!("schedule notice claim unavailable; acting anyway: {e}");
            true
        }
    }
}

/// Act on one notice's payload. Nothing is returned to anyone: a refusal is logged.
pub async fn handle(ctx: &Context, payload: &str) {
    let notice: Notice = match serde_json::from_str(payload) {
        Ok(notice) => notice,
        Err(e) => {
            tracing::warn!("Unreadable schedule notice {payload:?}: {e}");
            return;
        }
    };
    if !claim(ctx, &notice.id).await {
        return;
    }
    let outcome = match (notice.schedule, notice.run) {
        (Some(schedule), _) => schedules::plan(ctx, schedule, notice.replan, notice.caller)
            .await
            .map(|_| ()),
        (None, Some(run)) => schedules::cancel_run_if_waiting(ctx, run, notice.caller).await,
        (None, None) => return,
    };
    if let Err(e) = outcome {
        tracing::warn!("Schedule notice {payload}: {e}");
    }
}

/// Listen until the connection fails. Notices are handled one after another, in the order the
/// server sent them.
async fn listen(ctx: &Context) -> Result<(), sqlx::Error> {
    let mut listener = PgListener::connect_with(&ctx.db).await?;
    listener.listen(CHANNEL).await?;
    loop {
        let notification = listener.recv().await?;
        handle(ctx, notification.payload()).await;
    }
}

/// Listen forever (`run_forever`), reconnecting when the connection is lost.
pub async fn run_forever(ctx: Context) {
    loop {
        if let Err(e) = listen(&ctx).await {
            tracing::warn!("Schedule notices: lost the listener; reconnecting: {e}");
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}
