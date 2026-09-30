//! An agent's identity and its declaration (`facade/registration.py`).

use sqlx::PgPool;

/// The agent for `(client, user, organization)`, created with its memory shelve if it did not
/// exist (`ensure_agent`). A new agent is named after its client and belongs to the client's
/// release (and its app); an existing agent is not touched.
pub async fn ensure_agent(
    db: &PgPool,
    client: i64,
    user: i64,
    organization: i64,
) -> Result<i64, sqlx::Error> {
    let (agent, name): (i64, String) = sqlx::query_as(
        "WITH client AS (
             SELECT c.client_id, c.release_id, r.app_id
               FROM authentikate_client c JOIN authentikate_release r ON r.id = c.release_id
              WHERE c.id = $1
         )
         INSERT INTO facade_agent
             (installed_at, hash, name, health_check_interval, \"unique\", lease_epoch, on_instance,
              kind, latest_event, connected, blocked, app_id, release_id, client_id, user_id,
              organization_id)
         SELECT now(), '', client.client_id, 300, gen_random_uuid()::text, 0, 'all',
                'WEBSOCKET', 'DISCONNECT', false, false, client.app_id, client.release_id, $1, $2, $3
           FROM client
         ON CONFLICT (client_id, user_id, organization_id) DO UPDATE SET name = facade_agent.name
         RETURNING id, name",
    )
    .bind(client)
    .bind(user)
    .bind(organization)
    .fetch_one(db)
    .await?;

    sqlx::query(
        "INSERT INTO facade_memoryshelve (name, description, created_at, updated_at, agent_id, creator_id, organization_id)
         VALUES ($1, '', now(), now(), $2, $3, $4)
         ON CONFLICT (agent_id) DO NOTHING",
    )
    .bind(format!("{name} memory shelve"))
    .bind(agent)
    .bind(user)
    .bind(organization)
    .execute(db)
    .await?;
    Ok(agent)
}

/// Forget every drawer on the agent's shelve: a process that just started holds nothing.
pub async fn clear_drawers(
    executor: impl sqlx::PgExecutor<'_>,
    agent: i64,
) -> Result<u64, sqlx::Error> {
    Ok(sqlx::query(
        "DELETE FROM facade_memorydrawer d USING facade_memoryshelve s
          WHERE d.shelve_id = s.id AND s.agent_id = $1",
    )
    .bind(agent)
    .execute(executor)
    .await?
    .rows_affected())
}

/// Record that `agent` holds `resource_id` (an `identifier`) in memory (`shelve`): upsert the
/// drawer on its shelve, keyed by `resource_id`. `agent_minted` (a numbered SHELVE) marks a
/// drawer the agent references by that id; a later plain upsert never unsets it. The drawer's id.
pub async fn shelve(
    db: &PgPool,
    agent: i64,
    identifier: &str,
    resource_id: &str,
    label: Option<&str>,
    description: Option<&str>,
    agent_minted: bool,
) -> Result<i64, sqlx::Error> {
    let shelve: i64 = sqlx::query_scalar(
        "INSERT INTO facade_memoryshelve (name, description, created_at, updated_at, agent_id, creator_id, organization_id)
         SELECT a.name || ' memory shelve', '', now(), now(), a.id, a.user_id, a.organization_id
           FROM facade_agent a WHERE a.id = $1
         ON CONFLICT (agent_id) DO UPDATE SET agent_id = EXCLUDED.agent_id
         RETURNING id",
    )
    .bind(agent)
    .fetch_one(db)
    .await?;
    sqlx::query_scalar(
        "INSERT INTO facade_memorydrawer (shelve_id, resource_id, identifier, label, description, agent_minted)
         VALUES ($1, $2, $3, $4, $5, $6)
         ON CONFLICT (shelve_id, resource_id) WHERE resource_id IS NOT NULL
         DO UPDATE SET identifier = EXCLUDED.identifier, label = EXCLUDED.label, description = EXCLUDED.description,
                       agent_minted = facade_memorydrawer.agent_minted OR EXCLUDED.agent_minted
         RETURNING id",
    )
    .bind(shelve)
    .bind(resource_id)
    .bind(identifier)
    .bind(label)
    .bind(description)
    .bind(agent_minted)
    .fetch_one(db)
    .await
}

/// Why an unshelve was refused.
#[derive(Debug, thiserror::Error)]
pub enum UnshelveError {
    #[error("Unknown drawer {0:?}")]
    Unknown(String),
    #[error("This drawer does not belong to this agent.")]
    Foreign,
    #[error("database: {0}")]
    Database(#[from] sqlx::Error),
}

/// Drop the drawer `drawer` from `agent`'s shelve (`unshelve`). With `by_resource_id` (a
/// numbered UNSHELVE) it is first looked up as a resource id on the agent's own shelve, then
/// as a primary key.
pub async fn unshelve(
    db: &PgPool,
    agent: i64,
    drawer: &str,
    by_resource_id: bool,
) -> Result<(), UnshelveError> {
    if by_resource_id {
        let own = sqlx::query(
            "DELETE FROM facade_memorydrawer d USING facade_memoryshelve s
              WHERE d.shelve_id = s.id AND s.agent_id = $1 AND d.id = (
                    SELECT d2.id FROM facade_memorydrawer d2 JOIN facade_memoryshelve s2 ON s2.id = d2.shelve_id
                     WHERE s2.agent_id = $1 AND d2.resource_id = $2 ORDER BY d2.id LIMIT 1)",
        )
        .bind(agent)
        .bind(drawer)
        .execute(db)
        .await?
        .rows_affected();
        if own > 0 {
            return Ok(());
        }
    }
    let Some(id) = drawer
        .parse::<i64>()
        .ok()
        .filter(|_| drawer.chars().all(|c| c.is_ascii_digit()))
    else {
        return Err(UnshelveError::Unknown(drawer.to_owned()));
    };
    let owner: Option<i64> = sqlx::query_scalar(
        "SELECT s.agent_id FROM facade_memorydrawer d JOIN facade_memoryshelve s ON s.id = d.shelve_id WHERE d.id = $1",
    )
    .bind(id)
    .fetch_optional(db)
    .await?;
    match owner {
        None => Err(UnshelveError::Unknown(drawer.to_owned())),
        Some(owner) if owner != agent => Err(UnshelveError::Foreign),
        Some(_) => {
            sqlx::query("DELETE FROM facade_memorydrawer WHERE id = $1")
                .bind(id)
                .execute(db)
                .await?;
            Ok(())
        }
    }
}

use rekuest_core::inputs::{
    BlokImplementationInputModel, ImplementAgentInputModel, StateImplementationInputModel,
};
use sqlx::PgConnection;

use crate::catalog_validation::{
    dump_diagnostics, validate_manifest_against_catalog, Catalog, Diagnostic,
};
use crate::deletion::{delete_implementations, delete_states};
use crate::mutations::blok::sync_blok_dependencies;
use crate::mutations::implementation::{create_implementation, AgentIdentity, Prefetch};
use crate::mutations::Refusal;
use crate::registration_lock::lock_organization;
use crate::signals::{OnCommit, Signal};
use crate::unique::hash_state_definition;

/// What a registration did: the diagnostics to hand back on `INIT`, the signals to publish
/// once it committed, and the hash the agent now holds.
#[derive(Debug)]
pub struct Implemented {
    pub hash: String,
    pub diagnostics: Vec<Diagnostic>,
    pub on_commit: OnCommit,
}

/// Upsert one of the agent's states: `key` defaults to the interface, `app_identifier` to the
/// agent's app (`_register_state`).
async fn register_state(
    conn: &mut PgConnection,
    agent: &AgentIdentity,
    app_identifier: &str,
    state: &StateImplementationInputModel,
) -> Result<i64, sqlx::Error> {
    let definition: i64 = sqlx::query_scalar(
        "INSERT INTO facade_statedefinition (name, hash, ports, description, organization_id)
         VALUES ($1, $2, $3, 'A state definition', $4)
         ON CONFLICT (organization_id, hash) DO UPDATE SET name = excluded.name, ports = excluded.ports,
                                                           description = excluded.description
         RETURNING id",
    )
    .bind(&state.definition.name)
    .bind(hash_state_definition(&state.definition))
    .bind(serde_json::to_value(&state.definition.ports).expect("ports serialize"))
    .bind(agent.organization)
    .fetch_one(&mut *conn)
    .await?;
    sqlx::query_scalar(
        "INSERT INTO facade_state (interface, key, app_identifier, value, created_at, updated_at, retention_policy, agent_id, definition_id)
         VALUES ($1, $2, $3, '{}', now(), now(), 'KEEP_ALL', $4, $5)
         ON CONFLICT (interface, agent_id) DO UPDATE SET definition_id = excluded.definition_id, key = excluded.key,
                                                        app_identifier = excluded.app_identifier, updated_at = now()
         RETURNING id",
    )
    .bind(&state.interface)
    .bind(state.key.as_deref().filter(|k| !k.is_empty()).unwrap_or(&state.interface))
    .bind(state.app.as_deref().filter(|a| !a.is_empty()).unwrap_or(app_identifier))
    .bind(agent.id)
    .bind(definition)
    .fetch_one(conn)
    .await
}

/// One declared blok: its catalog, manifest check, row, auto-materialization bound to the
/// declaring agent, and a mapping per dependency. Its diagnostics.
async fn register_blok(
    conn: &mut PgConnection,
    agent: &AgentIdentity,
    blok: &BlokImplementationInputModel,
) -> Result<Vec<Diagnostic>, Refusal> {
    let catalog_name = blok
        .catalog
        .as_deref()
        .filter(|c| !c.is_empty())
        .unwrap_or("default");
    let catalog = Catalog::get_or_create(conn, catalog_name, agent.organization).await?;
    let diagnostics = validate_manifest_against_catalog(&catalog, Some(&blok.components))?;

    let (id, name, description): (i64, String, Option<String>) = sqlx::query_as(
        "INSERT INTO facade_blok (name, description, components, demo_state, diagnostics, creator_id, organization_id, catalog_id)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
         ON CONFLICT (organization_id, name) DO UPDATE SET
             description = excluded.description, components = excluded.components, demo_state = excluded.demo_state,
             diagnostics = excluded.diagnostics, creator_id = excluded.creator_id, catalog_id = excluded.catalog_id
         RETURNING id, name, description",
    )
    .bind(&blok.key)
    .bind(&blok.description)
    .bind(serde_json::to_value(&blok.components).expect("components serialize"))
    .bind(serde_json::Value::Object(blok.demo_state.clone().unwrap_or_default()))
    .bind(dump_diagnostics(&diagnostics))
    .bind(agent.user)
    .bind(agent.organization)
    .bind(catalog.id)
    .fetch_one(&mut *conn)
    .await?;

    let materialized: Option<i64> = sqlx::query_scalar(
        "UPDATE facade_materializedblok SET name = $3, description = $4, updated_at = now()
          WHERE id = (SELECT id FROM facade_materializedblok WHERE blok_id = $1 AND declared_by_id = $2 ORDER BY id LIMIT 1)
         RETURNING id",
    )
    .bind(id)
    .bind(agent.id)
    .bind(&name)
    .bind(description.as_deref().unwrap_or(""))
    .fetch_optional(&mut *conn)
    .await?;
    let materialized = match materialized {
        Some(materialized) => materialized,
        None => {
            sqlx::query_scalar(
                "INSERT INTO facade_materializedblok (name, description, created_at, updated_at, blok_id, declared_by_id)
                 VALUES ($3, $4, now(), now(), $1, $2) RETURNING id",
            )
            .bind(id)
            .bind(agent.id)
            .bind(&name)
            .bind(description.as_deref().unwrap_or(""))
            .fetch_one(&mut *conn)
            .await?
        }
    };

    for (dependency, key) in sync_blok_dependencies(conn, id, &blok.dependencies, true).await? {
        sqlx::query(
            "INSERT INTO facade_blokagentmapping (key, created_at, updated_at, agent_id, dependency_id, materialized_blok_id)
             VALUES ($1, now(), now(), $2, $3, $4)
             ON CONFLICT (materialized_blok_id, key) DO UPDATE SET dependency_id = excluded.dependency_id,
                                                                  agent_id = excluded.agent_id, updated_at = now()",
        )
        .bind(&key)
        .bind(agent.id)
        .bind(dependency)
        .bind(materialized)
        .execute(&mut *conn)
        .await?;
    }
    Ok(diagnostics)
}

/// Reconcile an agent's declared implementations, states, locks and bloks in one transaction
/// (`implement_agent`): either the whole declared set lands or none of it. The caller commits,
/// then publishes [`Implemented::on_commit`].
pub async fn implement_agent(
    conn: &mut PgConnection,
    agent: i64,
    payload: &ImplementAgentInputModel,
) -> Result<Implemented, Refusal> {
    let (organization, user, client_id, release, app, app_identifier): (
        i64,
        i64,
        String,
        i64,
        i64,
        String,
    ) = sqlx::query_as(
        "SELECT a.organization_id, a.user_id, c.client_id, c.release_id, r.app_id, app.identifier
           FROM facade_agent a
           JOIN authentikate_client c ON c.id = a.client_id
           JOIN authentikate_release r ON r.id = c.release_id
           JOIN authentikate_app app ON app.id = r.app_id
          WHERE a.id = $1",
    )
    .bind(agent)
    .fetch_one(&mut *conn)
    .await?;

    // Before any row is read or written: the org-shared rows are written interleaved with the
    // agent's own, so concurrent registrations of one fleet would deadlock without it.
    lock_organization(conn, organization).await?;

    let identity = AgentIdentity {
        id: agent,
        app,
        release,
        user,
        organization,
    };
    let mut on_commit = OnCommit::default();
    let hash = payload
        .hash
        .clone()
        .filter(|h| !h.is_empty())
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    // A register without a description keeps the agent's; `name` resets to the client id, as
    // in Python (which also undoes an `updateAgent` rename).
    sqlx::query(
        "UPDATE facade_agent SET name = $2, app_id = $3, release_id = $4, hash = $5, description = coalesce($6, description)
          WHERE id = $1",
    )
    .bind(agent)
    .bind(payload.name.as_deref().filter(|n| !n.is_empty()).unwrap_or(&client_id))
    .bind(app)
    .bind(release)
    .bind(&hash)
    .bind(&payload.description)
    .execute(&mut *conn)
    .await?;
    on_commit.push(Signal::AgentSaved {
        id: agent,
        created: false,
    });

    for lock in payload.locks.iter().flatten() {
        sqlx::query(
            "INSERT INTO facade_lock (created_at, key, description, updated_at, agent_id) VALUES (now(), $2, $3, now(), $1)
             ON CONFLICT (agent_id, key) DO UPDATE SET description = excluded.description, updated_at = now()",
        )
        .bind(agent)
        .bind(&lock.key)
        .bind(&lock.definition.description)
        .execute(&mut *conn)
        .await?;
    }

    let mut declared: Vec<_> = payload.implementations.iter().flatten().collect();
    declared.sort_by(|a, b| {
        (&a.definition.key, &a.definition.version).cmp(&(&b.definition.key, &b.definition.version))
    });
    let mut diagnostics = vec![];
    let mut created_implementations = vec![];
    if !declared.is_empty() {
        let wanted: Vec<(String, String)> = declared
            .iter()
            .map(|i| (i.definition.key.clone(), i.definition.version.clone()))
            .collect();
        let mut prefetch = Prefetch::load(conn, &identity, &wanted).await?;
        for implementation in declared {
            let registered = create_implementation(
                conn,
                implementation,
                &identity,
                &mut prefetch,
                &mut on_commit,
            )
            .await?;
            created_implementations.push(registered.id);
            diagnostics.extend(registered.diagnostics);
        }
    }

    let mut created_states = vec![];
    for state in payload.states.iter().flatten() {
        let id = register_state(conn, &identity, &app_identifier, state).await?;
        created_states.push(id);
        on_commit.push(Signal::StateSaved { id });
    }

    // Reap what the agent no longer declares; implementations still running tasks are kept, and
    // so are the wrappers deployed onto it (`createHigherOrderImplementation`), which it never
    // declares.
    let stale_states: Vec<i64> = sqlx::query_scalar(
        "SELECT id FROM facade_state WHERE agent_id = $1 AND NOT (id = ANY($2))",
    )
    .bind(agent)
    .bind(&created_states)
    .fetch_all(&mut *conn)
    .await?;
    delete_states(conn, &stale_states).await?;
    let stale: Vec<(i64, bool)> = sqlx::query_as(
        "SELECT i.id, EXISTS (SELECT 1 FROM facade_task t WHERE t.implementation_id = i.id AND NOT t.is_done)
           FROM facade_implementation i
          WHERE i.agent_id = $1 AND NOT (i.id = ANY($2)) AND i.higher_order_for_id IS NULL ORDER BY i.id",
    )
    .bind(agent)
    .bind(&created_implementations)
    .fetch_all(&mut *conn)
    .await?;
    let live = stale.iter().filter(|(_, live)| *live).count();
    if live > 0 {
        tracing::warn!(
            "Keeping {live} undeclared implementation(s) for agent {agent}: still running tasks."
        );
    }
    let reaped: Vec<i64> = stale
        .iter()
        .filter(|(_, live)| !live)
        .map(|(id, _)| *id)
        .collect();
    for deleted in delete_implementations(conn, &reaped).await? {
        on_commit.push(Signal::ImplementationDeleted {
            id: deleted.id,
            agent_id: deleted.agent_id,
        });
    }

    for blok in payload.bloks.iter().flatten() {
        diagnostics.extend(register_blok(conn, &identity, blok).await?);
    }

    Ok(Implemented {
        hash,
        diagnostics,
        on_commit,
    })
}
