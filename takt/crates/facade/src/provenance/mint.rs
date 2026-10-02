//! Mint the signed provenance JWT of an assignment (`facade/provenance/mint.py`).
//!
//! One token per assignment, Ed25519. RFC-registered claims keep their names; rekuest's own are
//! three-letter symbols:
//!
//! | claim | meaning |
//! |---|---|
//! | `iss` | the provenance issuer |
//! | `aud` | list of target services (`Implementation.provenance_audience`) |
//! | `sub` | the immediate causer of this hop (the request principal) |
//! | `act.sub`, `act.cid` | the executing agent's user sub and OAuth client id |
//! | `iat`, `exp`, `jti` | issued at, expiry (`TOKEN_TTL_SECONDS`), a fresh uuid4 |
//! | `tsk`, `ptk`, `rtk` | this task, its parent (null for a root), its root (== `tsk` for a root) |
//! | `rcb` | root caused by: the human at the root of the tree |
//! | `ahs`, `aha` | the args hash (`canonical`) and how it was computed |

use serde_json::{json, Map, Value};
use sqlx::PgConnection;

use crate::caller_context::CallerContext;
use crate::provenance::{canonical, keys, principal};
use crate::settings::Settings;

/// Guard against a corrupt parent cycle when walking to the root (`_MAX_LINEAGE_DEPTH`).
const MAX_LINEAGE_DEPTH: usize = 256;

/// What the mint reads of a task: a persisted one, or a probe (always a root).
#[derive(Debug, Clone)]
pub struct MintTask<'a> {
    /// The task's id: its primary key, or a probe id (`p-…`).
    pub id: String,
    pub parent_id: Option<i64>,
    pub implementation_id: i64,
    pub agent_id: i64,
    pub args: &'a Map<String, Value>,
}

#[derive(Debug, thiserror::Error)]
pub enum MintError {
    /// A non-human root under a strict policy, or no key to sign with (`ValueError`,
    /// `ImproperlyConfigured`): the assign is refused.
    #[error("{0}")]
    Refused(String),
    #[error("database: {0}")]
    Database(#[from] sqlx::Error),
}

/// The root of `parent`'s tree, walking `parent_id` (`_resolve_root`).
async fn resolve_root(conn: &mut PgConnection, parent: i64) -> Result<i64, sqlx::Error> {
    let mut current = parent;
    for _ in 0..MAX_LINEAGE_DEPTH {
        let next: Option<i64> =
            sqlx::query_scalar("SELECT parent_id FROM facade_task WHERE id = $1")
                .bind(current)
                .fetch_one(&mut *conn)
                .await?;
        match next {
            Some(next) => current = next,
            None => break,
        }
    }
    Ok(current)
}

/// Mint the token for `task`, or `None` to skip: the implementation opts out (`needs_token`),
/// or the root cannot be confirmed human under a lenient policy (`mint_token_for_task`).
pub async fn mint_token_for_task(
    conn: &mut PgConnection,
    settings: &Settings,
    task: &MintTask<'_>,
    ctx: &CallerContext,
) -> Result<Option<String>, MintError> {
    let (needs_token, audience): (bool, Option<Value>) = sqlx::query_as(
        "SELECT needs_token, provenance_audience FROM facade_implementation WHERE id = $1",
    )
    .bind(task.implementation_id)
    .fetch_one(&mut *conn)
    .await?;
    if !needs_token {
        return Ok(None);
    }
    let (agent_sub, agent_client_id): (String, String) = sqlx::query_as(
        "SELECT u.sub, c.client_id FROM facade_agent a
           JOIN authentikate_user u ON u.id = a.user_id
           JOIN authentikate_client c ON c.id = a.client_id
          WHERE a.id = $1",
    )
    .bind(task.agent_id)
    .fetch_one(&mut *conn)
    .await?;

    let provenance = &settings.provenance;
    let sub = ctx.user_sub.clone();
    let (root, root_caused_by, root_human) = match task.parent_id {
        None => (
            task.id.clone(),
            Some(sub.clone()),
            principal::is_human_by_roles(provenance, &ctx.roles),
        ),
        Some(parent) => {
            let root = resolve_root(conn, parent).await?;
            let caller: Option<(i64, String)> = sqlx::query_as(
                "SELECT c.id, u.sub FROM facade_task t
                   JOIN facade_caller c ON c.id = t.caller_id
                   JOIN authentikate_user u ON u.id = c.user_id
                  WHERE t.id = $1",
            )
            .bind(root)
            .fetch_optional(&mut *conn)
            .await?;
            match caller {
                None => (root.to_string(), None, false),
                Some((caller, caller_sub)) => (
                    root.to_string(),
                    Some(caller_sub),
                    principal::is_human_caller(&mut *conn, provenance, caller).await?,
                ),
            }
        }
    };

    if !root_human {
        let message = format!(
            "Refusing to mint provenance token for task {}: root principal (root_caused_by={}) is not an accountable human.",
            task.id,
            root_caused_by.as_deref().unwrap_or("None")
        );
        if provenance.strict {
            return Err(MintError::Refused(message));
        }
        tracing::warn!("{message}");
        return Ok(None);
    }

    let Some(key) = settings.instance_key.as_deref() else {
        return Err(MintError::Refused(
            "No provenance private key configured (instance.private_key): a static Ed25519 key is required so tokens verify across process restarts and replicas.".into(),
        ));
    };
    let now = chrono::Utc::now().timestamp();
    let audience = match audience {
        Some(Value::Array(services)) => Value::Array(services),
        _ => json!([]),
    };
    let claims = json!({
        "iss": provenance.issuer,
        "aud": audience,
        "sub": sub,
        "act": {"sub": agent_sub, "cid": agent_client_id},
        "iat": now,
        "exp": now + provenance.token_ttl.as_secs() as i64,
        "jti": uuid::Uuid::new_v4().to_string(),
        "tsk": task.id,
        "ptk": task.parent_id.map(|p| p.to_string()),
        "rtk": root,
        "rcb": root_caused_by,
        "ahs": canonical::args_hash(task.args),
        "aha": canonical::algorithm(),
    });
    let header = json!({"alg": keys::ALGORITHM, "kid": key.kid(), "typ": "JWT"});
    Ok(Some(key.sign_jwt(&header, &claims)))
}
