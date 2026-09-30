//! Django's delete collector, for the rows a registration removes.
//!
//! Every foreign key in the schema is `NO ACTION DEFERRABLE INITIALLY DEFERRED`: Django emulates
//! `on_delete` in Python, walking the relations before it deletes. Rust bypasses that, so the
//! walk is written out here, relation by relation, from the models' `on_delete` (CASCADE deletes,
//! SET_NULL nulls, PROTECT refuses). Constraints are deferred, so the order of the statements
//! within the transaction does not matter; only that nothing dangles at commit.
//!
//! Only `Implementation` has a `post_delete` receiver among what is deleted here; its rows are
//! returned so the caller can publish after commit.

use sqlx::PgConnection;

type Ids = Vec<i64>;

/// A deleted implementation, as `implementation_post_del` needs it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DeletedImplementation {
    pub id: i64,
    pub agent_id: i64,
}

/// `models.ProtectedError`: a PROTECT relation still points at a row about to go.
#[derive(Debug, thiserror::Error)]
pub enum DeleteError {
    #[error(transparent)]
    Db(#[from] sqlx::Error),
    #[error("Cannot delete some instances of model 'Implementation' because they are referenced through protected foreign keys: 'ResolvedDependency.implementation'.")]
    Protected,
}

async fn ids(conn: &mut PgConnection, sql: &str, bind: &[i64]) -> Result<Ids, sqlx::Error> {
    sqlx::query_scalar(sql).bind(bind).fetch_all(conn).await
}

async fn exec(conn: &mut PgConnection, sql: &str, bind: &[i64]) -> Result<(), sqlx::Error> {
    sqlx::query(sql).bind(bind).execute(conn).await?;
    Ok(())
}

/// Tasks and everything below them: children and root-descendants (CASCADE), events,
/// instructs and patches (CASCADE); locks held and signals caused are released (SET_NULL).
pub async fn delete_tasks(conn: &mut PgConnection, tasks: &[i64]) -> Result<(), sqlx::Error> {
    if tasks.is_empty() {
        return Ok(());
    }
    let all = ids(
        conn,
        "WITH RECURSIVE doomed(id) AS (
             SELECT unnest($1::bigint[])
             UNION
             SELECT t.id FROM facade_task t JOIN doomed d ON t.parent_id = d.id OR t.root_id = d.id
         ) SELECT id FROM doomed",
        tasks,
    )
    .await?;
    exec(
        conn,
        "UPDATE facade_lock SET hold_by_id = NULL WHERE hold_by_id = ANY($1)",
        &all,
    )
    .await?;
    exec(
        conn,
        "UPDATE facade_signal SET causing_task_id = NULL WHERE causing_task_id = ANY($1)",
        &all,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_taskevent WHERE task_id = ANY($1) OR delegated_to_id = ANY($1)",
        &all,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_taskinstruct WHERE task_id = ANY($1)",
        &all,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_patch WHERE task_id = ANY($1)",
        &all,
    )
    .await?;
    exec(conn, "DELETE FROM facade_task WHERE id = ANY($1)", &all).await
}

/// Resolutions: their tasks and resolved dependencies (CASCADE).
async fn delete_resolutions(
    conn: &mut PgConnection,
    resolutions: &[i64],
) -> Result<(), sqlx::Error> {
    if resolutions.is_empty() {
        return Ok(());
    }
    let tasks = ids(
        conn,
        "SELECT id FROM facade_task WHERE resolution_id = ANY($1)",
        resolutions,
    )
    .await?;
    delete_tasks(conn, &tasks).await?;
    exec(
        conn,
        "DELETE FROM facade_resolveddependency WHERE resolution_id = ANY($1) OR down_stream_resolution_id = ANY($1)",
        resolutions,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_resolution WHERE id = ANY($1)",
        resolutions,
    )
    .await
}

/// Implementations, refused while a resolved dependency outside this cascade still points at one
/// (PROTECT). Returns what was deleted, for `implementation_post_del`.
pub async fn delete_implementations(
    conn: &mut PgConnection,
    implementations: &[i64],
) -> Result<Vec<DeletedImplementation>, DeleteError> {
    if implementations.is_empty() {
        return Ok(vec![]);
    }
    let resolutions = ids(
        conn,
        "SELECT id FROM facade_resolution WHERE implementation_id = ANY($1)",
        implementations,
    )
    .await?;
    let protected: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM facade_resolveddependency
                         WHERE implementation_id = ANY($1)
                           AND NOT (resolution_id = ANY($2)
                                    OR coalesce(down_stream_resolution_id = ANY($2), false)))",
    )
    .bind(implementations)
    .bind(&resolutions)
    .fetch_one(&mut *conn)
    .await?;
    if protected {
        return Err(DeleteError::Protected);
    }
    delete_resolutions(conn, &resolutions).await?;
    exec(
        conn,
        "UPDATE facade_task SET implementation_id = NULL WHERE implementation_id = ANY($1)",
        implementations,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_shortcut WHERE implementation_id = ANY($1)",
        implementations,
    )
    .await?;
    let dependencies = ids(
        conn,
        "SELECT id FROM facade_dependency WHERE implementation_id = ANY($1)",
        implementations,
    )
    .await?;
    exec(
        conn,
        "UPDATE facade_resolveddependency SET dependency_id = NULL WHERE dependency_id = ANY($1)",
        &dependencies,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_dependency WHERE id = ANY($1)",
        &dependencies,
    )
    .await?;
    exec(
        conn,
        "UPDATE facade_implementation SET higher_order_for_id = NULL WHERE higher_order_for_id = ANY($1) AND NOT id = ANY($1)",
        implementations,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_testresult WHERE implementation_id = ANY($1) OR tester_id = ANY($1)",
        implementations,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_implementation_pinned_by WHERE implementation_id = ANY($1)",
        implementations,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_implementation_manipulates WHERE implementation_id = ANY($1)",
        implementations,
    )
    .await?;
    let deleted: Vec<(i64, i64)> = sqlx::query_as(
        "DELETE FROM facade_implementation WHERE id = ANY($1) RETURNING id, agent_id",
    )
    .bind(implementations)
    .fetch_all(&mut *conn)
    .await?;
    Ok(deleted
        .into_iter()
        .map(|(id, agent_id)| DeletedImplementation { id, agent_id })
        .collect())
}

/// Actions: ports, schedules, triggers, tasks, shortcuts, implementations and test cases
/// (CASCADE), and their many-to-many rows.
pub async fn delete_actions(
    conn: &mut PgConnection,
    actions: &[i64],
) -> Result<Vec<DeletedImplementation>, DeleteError> {
    if actions.is_empty() {
        return Ok(vec![]);
    }
    let tasks = ids(
        conn,
        "SELECT id FROM facade_task WHERE action_id = ANY($1)",
        actions,
    )
    .await?;
    delete_tasks(conn, &tasks).await?;
    let implementations = ids(
        conn,
        "SELECT id FROM facade_implementation WHERE action_id = ANY($1)",
        actions,
    )
    .await?;
    let deleted = delete_implementations(conn, &implementations).await?;
    exec(
        conn,
        "DELETE FROM facade_argport WHERE action_id = ANY($1)",
        actions,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_returnport WHERE action_id = ANY($1)",
        actions,
    )
    .await?;
    exec(
        conn,
        "UPDATE facade_task SET schedule_id = NULL WHERE schedule_id IN (SELECT id FROM facade_schedule WHERE action_id = ANY($1))",
        actions,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_schedule WHERE action_id = ANY($1)",
        actions,
    )
    .await?;
    exec(
        conn,
        "UPDATE facade_task SET trigger_id = NULL WHERE trigger_id IN (SELECT id FROM facade_trigger WHERE action_id = ANY($1))",
        actions,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_trigger WHERE action_id = ANY($1)",
        actions,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_shortcut WHERE action_id = ANY($1)",
        actions,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_testresult WHERE case_id IN (SELECT id FROM facade_testcase WHERE action_id = ANY($1) OR tester_id = ANY($1))",
        actions,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_testcase WHERE action_id = ANY($1) OR tester_id = ANY($1)",
        actions,
    )
    .await?;
    exec(conn, "DELETE FROM facade_action_is_test_for WHERE from_action_id = ANY($1) OR to_action_id = ANY($1)", actions).await?;
    exec(
        conn,
        "DELETE FROM facade_action_pinned_by WHERE action_id = ANY($1)",
        actions,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_action_collections WHERE action_id = ANY($1)",
        actions,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_action_protocols WHERE action_id = ANY($1)",
        actions,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_action WHERE id = ANY($1)",
        actions,
    )
    .await?;
    Ok(deleted)
}

/// States: their patches and snapshots (CASCADE) and the implementations' `manipulates` rows.
pub async fn delete_states(conn: &mut PgConnection, states: &[i64]) -> Result<(), sqlx::Error> {
    if states.is_empty() {
        return Ok(());
    }
    exec(
        conn,
        "DELETE FROM facade_patch WHERE state_id = ANY($1)",
        states,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_snapshot WHERE state_id = ANY($1)",
        states,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_implementation_manipulates WHERE state_id = ANY($1)",
        states,
    )
    .await?;
    exec(conn, "DELETE FROM facade_state WHERE id = ANY($1)", states).await
}

/// Blok dependencies: the mappings bound to them keep their row, unbound (SET_NULL).
pub async fn delete_blok_dependencies(
    conn: &mut PgConnection,
    dependencies: &[i64],
) -> Result<(), sqlx::Error> {
    if dependencies.is_empty() {
        return Ok(());
    }
    exec(
        conn,
        "UPDATE facade_blokagentmapping SET dependency_id = NULL WHERE dependency_id = ANY($1)",
        dependencies,
    )
    .await?;
    exec(
        conn,
        "DELETE FROM facade_blokdependency WHERE id = ANY($1)",
        dependencies,
    )
    .await
}
