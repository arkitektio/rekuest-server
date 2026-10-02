//! What a workflow's guard asks about a state it depends on (`facade/guards.py`).
//!
//! A workflow that was down (its agent died, it was resumed) may come back to a world that moved.
//! The guard recorded the state's revision when the workflow first entered it; the resumed run
//! asks whether anything changed since. "Anything" excludes the workflow's own calls (its
//! `dispense` changes the plate too, and that is not news); a state set up again (the agent
//! restarted) has changed, whatever its values.

use serde_json::{json, Value};
use sqlx::PgPool;

use rekuest_core::pyjson::repr_str;

use crate::backend::{parse_id, BackendError, BackendResult};

/// A guard's answer: the revision now, whether it changed since, and what changed it.
#[derive(Debug, Clone, PartialEq)]
pub struct Revision {
    pub revision: Value,
    pub changed: Option<bool>,
    pub detail: Option<String>,
}

/// `STATE_REVISION_REQUEST` (`state_revision_sync`).
pub async fn state_revision(
    db: &PgPool,
    agent: i64,
    parent: &str,
    dependency: &str,
    state: &str,
    since: Option<&Value>,
    paths: &[String],
) -> BackendResult<Revision> {
    let parent_id = parse_id(parent)?;
    let row: Option<(i64, Option<Value>, Option<i64>)> = sqlx::query_as(
        "SELECT agent_id, dependencies, implementation_id FROM facade_task WHERE id = $1",
    )
    .bind(parent_id)
    .fetch_optional(db)
    .await?;
    let Some((parent_agent, dependencies, implementation)) = row else {
        return Err(BackendError::Refused(
            "Task matching query does not exist.".into(),
        ));
    };
    if parent_agent != agent {
        return Err(BackendError::Forbidden(
            "A guard is asked by the agent running the workflow.".into(),
        ));
    }

    let (state_id, session) =
        guarded_state(db, dependencies, implementation, dependency, state).await?;
    let revision = json!({"session": session, "global_rev": latest_rev(db, state_id, session.as_deref()).await?});
    let Some(since) = since.filter(|s| !s.is_null()) else {
        return Ok(Revision {
            revision,
            changed: None,
            detail: None,
        });
    };

    let since_session = since.get("session").cloned().unwrap_or(Value::Null);
    if since_session != json!(session) {
        return Ok(Revision {
            revision,
            changed: Some(true),
            detail: Some(format!(
                "{} was set up again: its agent restarted since.",
                repr_str(state)
            )),
        });
    }
    let since_rev = since.get("global_rev").and_then(Value::as_i64).unwrap_or(0);
    let pointers: Vec<String> = paths
        .iter()
        .map(|path| format!("/{}", path.trim_matches('/').replace('.', "/")))
        .collect();
    let tree = call_tree(db, parent_id).await?;
    let change: Option<(String, Option<i64>)> = sqlx::query_as(
        "SELECT p.path, p.task_id FROM facade_patch p JOIN facade_session s ON s.id = p.session_id
          WHERE p.state_id = $1 AND s.session_id = $2 AND p.global_rev > $3
            AND (p.task_id IS NULL OR NOT p.task_id = ANY($4))
            AND (cardinality($5::text[]) = 0
                 OR EXISTS (SELECT 1 FROM unnest($5::text[]) AS w(pointer)
                             WHERE p.path = w.pointer OR starts_with(p.path, w.pointer || '/')))
          ORDER BY p.global_rev, p.id LIMIT 1",
    )
    .bind(state_id)
    .bind(&session)
    .bind(since_rev)
    .bind(&tree)
    .bind(&pointers)
    .fetch_optional(db)
    .await?;
    Ok(match change {
        None => Revision {
            revision,
            changed: Some(false),
            detail: None,
        },
        Some((path, task)) => {
            let by = task.map_or("the agent itself".to_owned(), |task| format!("task {task}"));
            Revision {
                revision,
                changed: Some(true),
                detail: Some(format!(
                    "{} changed at {path} (by {by}) since the workflow last saw it.",
                    repr_str(state)
                )),
            }
        }
    })
}

/// The guarded state and its agent's active session (`_guarded_state`).
async fn guarded_state(
    db: &PgPool,
    dependencies: Option<Value>,
    implementation: Option<i64>,
    dependency: &str,
    slot: &str,
) -> BackendResult<(i64, Option<String>)> {
    let entries = dependencies
        .as_ref()
        .and_then(|d| d.get(dependency))
        .and_then(Value::as_array)
        .filter(|entries| !entries.is_empty())
        .ok_or_else(|| {
            BackendError::Refused(format!(
                "The workflow has no dependency {} to guard.",
                repr_str(dependency)
            ))
        })?;
    let mut agents: Vec<String> = entries
        .iter()
        .filter_map(|entry| entry.get("agent"))
        .filter(|agent| !agent.is_null() && *agent != "")
        .map(|agent| match agent {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        })
        .collect();
    agents.sort();
    agents.dedup();
    if agents.len() != 1 {
        return Err(BackendError::Refused(format!(
            "A guard needs a dependency resolved to one agent; {} was resolved to {}.",
            repr_str(dependency),
            agents.len()
        )));
    }
    let owner: Option<(i64, Option<String>)> =
        sqlx::query_as("SELECT id, active_session_id FROM facade_agent WHERE id = $1")
            .bind(parse_id(&agents[0])?)
            .fetch_optional(db)
            .await?;
    let Some((owner, session)) = owner else {
        return Err(BackendError::Refused(
            "Agent matching query does not exist.".into(),
        ));
    };

    let declared: Option<Value> = sqlx::query_scalar(
        "SELECT state_demands FROM facade_dependency WHERE implementation_id = $1 AND key = $2
          ORDER BY id LIMIT 1",
    )
    .bind(implementation)
    .bind(dependency)
    .fetch_optional(db)
    .await?;
    let identity = declared
        .iter()
        .filter_map(Value::as_array)
        .flatten()
        .find(|demand| demand.get("key").and_then(Value::as_str) == Some(slot))
        .and_then(|demand| demand.get("demand"))
        .and_then(|demand| demand.get("key"))
        .and_then(Value::as_str)
        .filter(|key| !key.is_empty())
        .unwrap_or(slot)
        .to_owned();
    let state: Option<i64> = sqlx::query_scalar(
        "SELECT id FROM (
             (SELECT id, 0 AS rank FROM facade_state WHERE agent_id = $1 AND key = $2 ORDER BY id LIMIT 1)
             UNION ALL
             (SELECT id, 1 AS rank FROM facade_state WHERE agent_id = $1 AND interface = $2 ORDER BY id LIMIT 1)
         ) found ORDER BY rank LIMIT 1",
    )
    .bind(owner)
    .bind(&identity)
    .fetch_optional(db)
    .await?;
    let Some(state) = state else {
        return Err(BackendError::Refused(format!(
            "The agent of {} has no state {} to guard.",
            repr_str(dependency),
            repr_str(&identity)
        )));
    };
    Ok((state, session))
}

/// The highest revision of the state within the session (`_latest_rev`).
async fn latest_rev(db: &PgPool, state: i64, session: Option<&str>) -> BackendResult<i64> {
    let Some(session) = session else {
        return Ok(0);
    };
    let rev: Option<i32> = sqlx::query_scalar(
        "SELECT GREATEST(
             (SELECT max(p.global_rev) FROM facade_patch p JOIN facade_session s ON s.id = p.session_id
               WHERE p.state_id = $1 AND s.session_id = $2),
             (SELECT max(n.global_rev) FROM facade_snapshot n JOIN facade_session s ON s.id = n.session_id
               WHERE n.state_id = $1 AND s.session_id = $2))",
    )
    .bind(state)
    .bind(session)
    .fetch_one(db)
    .await?;
    Ok(i64::from(rev.unwrap_or(0)))
}

/// The workflow and every task it caused, however deep (`_call_tree`).
async fn call_tree(db: &PgPool, root: i64) -> BackendResult<Vec<i64>> {
    Ok(sqlx::query_scalar(
        "WITH RECURSIVE tree(id) AS (
             SELECT $1::bigint
             UNION SELECT t.id FROM facade_task t JOIN tree ON t.parent_id = tree.id
         ) SELECT id FROM tree",
    )
    .bind(root)
    .fetch_all(db)
    .await?)
}
