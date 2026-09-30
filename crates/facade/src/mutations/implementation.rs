//! Registering one implementation and its action (`facade/mutations/implementation.py`,
//! `_create_implementation` and what it calls).

use std::collections::HashMap;

use rekuest_core::enums::Effects;
use rekuest_core::inputs::{
    AgentDependencyInputModel, ArgPortInputModel, DefinitionInputModel, DescriptorConstraint,
    ImplementationInputModel, ReturnPortInputModel,
};
use serde_json::Value;
use sqlx::PgConnection;

use super::Refusal;
use crate::catalog_validation::{collect_diagnostics, dump_diagnostics, Diagnostic};
use crate::deletion::delete_actions;
use crate::descriptors::compile_descriptors_to_jsonpath;
use crate::protocol::infer_protocols;
use crate::provenance::audience;
use crate::signals::{OnCommit, Signal};
use crate::unique::infer_action_scope;

/// The registering agent, as the writes need it.
#[derive(Debug, Clone, Copy)]
pub struct AgentIdentity {
    pub id: i64,
    pub app: i64,
    pub release: i64,
    pub user: i64,
    pub organization: i64,
}

/// An action row, as far as registration reads it.
#[derive(Debug, Clone, sqlx::FromRow)]
pub struct ActionRow {
    pub id: i64,
    pub key: String,
    pub version: String,
    pub hash: String,
    pub app_id: i64,
    pub args: Value,
    pub returns: Value,
    pub pure: bool,
    pub idempotent: bool,
    pub allow_probe: bool,
    pub is_dev: bool,
    pub kind: String,
    pub port_groups: Value,
    pub arg_count: i32,
    pub return_count: i32,
}

const ACTION_COLUMNS: &str =
    "id, key, version, hash, app_id, args, returns, pure, idempotent, allow_probe, is_dev, kind, port_groups, arg_count, return_count";

/// An implementation row, as far as registration reads it (enough to write it back whole).
#[derive(Debug, Clone, sqlx::FromRow)]
pub struct ImplementationRow {
    pub id: i64,
    pub interface: String,
    pub action_id: i64,
    pub name: String,
    pub policy: Value,
    pub higher_order_config: Value,
    pub higher_order_for_id: Option<i64>,
    pub tracks: Value,
    pub created_at: chrono::DateTime<chrono::Utc>,
}

/// The batch prefetch of `implement_agent`: the agent's app's actions by `(key, version)` and
/// the agent's implementations by interface. Rows created during the batch go in too, so a
/// duplicate within one batch resolves like sequential lookups would.
#[derive(Debug, Default)]
pub struct Prefetch {
    pub actions: HashMap<(String, String), ActionRow>,
    pub implementations: HashMap<String, ImplementationRow>,
}

impl Prefetch {
    pub async fn load(
        conn: &mut PgConnection,
        agent: &AgentIdentity,
        wanted: &[(String, String)],
    ) -> Result<Self, sqlx::Error> {
        let keys: Vec<&str> = wanted.iter().map(|(key, _)| key.as_str()).collect();
        let versions: Vec<&str> = wanted.iter().map(|(_, version)| version.as_str()).collect();
        let actions: Vec<ActionRow> = sqlx::query_as(&format!(
            "SELECT {ACTION_COLUMNS} FROM facade_action
              WHERE app_id = $1 AND organization_id = $2 AND key = ANY($3) AND version = ANY($4)"
        ))
        .bind(agent.app)
        .bind(agent.organization)
        .bind(&keys)
        .bind(&versions)
        .fetch_all(&mut *conn)
        .await?;
        let implementations: Vec<ImplementationRow> = sqlx::query_as(
            "SELECT id, interface, action_id, name, policy, higher_order_config, higher_order_for_id, tracks, created_at
               FROM facade_implementation WHERE agent_id = $1",
        )
        .bind(agent.id)
        .fetch_all(&mut *conn)
        .await?;
        Ok(Self {
            actions: actions
                .into_iter()
                .map(|a| ((a.key.clone(), a.version.clone()), a))
                .collect(),
            implementations: implementations
                .into_iter()
                .map(|i| (i.interface.clone(), i))
                .collect(),
        })
    }
}

fn dump<T: serde::Serialize>(value: &T) -> Value {
    serde_json::to_value(value).expect("declarations serialize")
}

/// The pure/effects and pure/stateful contradictions; the effective idempotence
/// (`_validate_qualifiers`).
fn validate_qualifiers(
    definition: &DefinitionInputModel,
    effects: Effects,
) -> Result<bool, Refusal> {
    if definition.pure && effects == Effects::IRREVERSIBLE {
        return Err(Refusal::Invalid(format!(
            "Action {} is declared pure but its implementation's effects are IRREVERSIBLE — a pure action cannot touch the real world.",
            definition.key
        )));
    }
    if definition.pure && definition.stateful {
        return Err(Refusal::Invalid(format!(
            "Action {} is declared both pure and stateful — a pure action cannot depend on or change state.",
            definition.key
        )));
    }
    Ok(definition.idempotent || definition.pure)
}

/// Find or create the action; whether its definition changed (`_upsert_action`).
async fn upsert_action(
    conn: &mut PgConnection,
    definition: &DefinitionInputModel,
    agent: &AgentIdentity,
    scope: &str,
    idempotent: bool,
    prefetch: &mut Prefetch,
    on_commit: &mut OnCommit,
) -> Result<(ActionRow, bool), Refusal> {
    let hash = definition.unique_hash();
    let lookup = (definition.key.clone(), definition.version.clone());
    let description = definition
        .description
        .clone()
        .filter(|d| !d.is_empty())
        .unwrap_or_else(|| "No description".into());
    let args = dump(&definition.args);
    let returns = dump(&definition.returns);
    let port_groups = dump(&definition.port_groups);

    if let Some(action) = prefetch.actions.get(&lookup).cloned() {
        if action.hash == hash {
            return Ok((action, false));
        }
        if action.app_id != agent.app {
            return Err(Refusal::Invalid(format!(
                "Action with key {} and version {} already exists but has different hash and you are not the owner. Please update the version or key of your action definition.",
                definition.key, definition.version
            )));
        }
        let action: ActionRow = sqlx::query_as(&format!(
            "UPDATE facade_action SET hash = $2, args = $3, returns = $4, port_groups = $5, stateful = $6, scope = $7,
                    kind = $8, description = $9, name = $10
              WHERE id = $1 RETURNING {ACTION_COLUMNS}"
        ))
        .bind(action.id)
        .bind(&hash)
        .bind(&args)
        .bind(&returns)
        .bind(&port_groups)
        .bind(definition.stateful)
        .bind(scope)
        .bind(definition.kind.value())
        .bind(&description)
        .bind(&definition.name)
        .fetch_one(&mut *conn)
        .await?;
        on_commit.push(Signal::ActionSaved {
            id: action.id,
            created: false,
        });
        prefetch.actions.insert(lookup, action.clone());
        return Ok((action, true));
    }

    // get_or_create: another organization member may have inserted it since the prefetch.
    let inserted: Option<ActionRow> = sqlx::query_as(&format!(
        "INSERT INTO facade_action
             (defined_at, embedding_model, key, version, app_id, organization_id, hash, description, args, scope,
              stateful, pure, idempotent, allow_probe, is_dev, kind, port_groups, returns, name, arg_count, return_count)
         VALUES (now(), '', $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, 0, 0)
         ON CONFLICT (organization_id, app_id, key, version) DO NOTHING
         RETURNING {ACTION_COLUMNS}"
    ))
    .bind(&definition.key)
    .bind(&definition.version)
    .bind(agent.app)
    .bind(agent.organization)
    .bind(&hash)
    .bind(&description)
    .bind(&args)
    .bind(scope)
    .bind(definition.stateful)
    .bind(definition.pure)
    .bind(idempotent)
    .bind(definition.allow_probe)
    .bind(definition.is_dev)
    .bind(definition.kind.value())
    .bind(&port_groups)
    .bind(&returns)
    .bind(&definition.name)
    .fetch_optional(&mut *conn)
    .await?;
    let action = match inserted {
        Some(action) => {
            on_commit.push(Signal::ActionSaved { id: action.id, created: true });
            action
        }
        None => {
            sqlx::query_as(&format!(
                "SELECT {ACTION_COLUMNS} FROM facade_action WHERE organization_id = $1 AND app_id = $2 AND key = $3 AND version = $4"
            ))
            .bind(agent.organization)
            .bind(agent.app)
            .bind(&definition.key)
            .bind(&definition.version)
            .fetch_one(&mut *conn)
            .await?
        }
    };
    prefetch.actions.insert(lookup, action.clone());
    Ok((action, true))
}

/// What the relational port rows need of a declared port.
trait RelationalPort {
    fn key(&self) -> &str;
    fn kind(&self) -> &'static str;
    fn identifier(&self) -> Option<&str>;
    fn nullable(&self) -> bool;
    fn dimension(&self) -> Option<&str>;
    fn descriptors(&self) -> Option<&[DescriptorConstraint]>;
    fn children(&self) -> &[Self]
    where
        Self: Sized;
}

macro_rules! relational_port {
    ($model:ty, $descriptors:ident) => {
        impl RelationalPort for $model {
            fn key(&self) -> &str {
                &self.key
            }
            fn kind(&self) -> &'static str {
                self.kind.value()
            }
            fn identifier(&self) -> Option<&str> {
                self.identifier.as_deref()
            }
            fn nullable(&self) -> bool {
                self.nullable
            }
            fn dimension(&self) -> Option<&str> {
                self.dimension.as_deref()
            }
            fn descriptors(&self) -> Option<&[DescriptorConstraint]> {
                self.$descriptors.as_deref()
            }
            fn children(&self) -> &[Self] {
                self.children.as_deref().unwrap_or_default()
            }
        }
    };
}

relational_port!(ArgPortInputModel, requires);
relational_port!(ReturnPortInputModel, provides);

/// Flatten a port tree into rows, one insert per depth level (`_bulk_create_ports_level_by_level`).
async fn create_ports_level_by_level<P: RelationalPort>(
    conn: &mut PgConnection,
    ports: &[P],
    action: i64,
    table: &str,
) -> Result<(), Refusal> {
    // (port, its parent row, its index among siblings, its key path)
    let mut level: Vec<(&P, Option<i64>, i32, String)> = ports
        .iter()
        .enumerate()
        .map(|(index, port)| (port, None, index as i32, port.key().to_owned()))
        .collect();
    while !level.is_empty() {
        let mut compiled = Vec::with_capacity(level.len());
        for (port, ..) in &level {
            compiled.push(
                compile_descriptors_to_jsonpath(port.descriptors()).map_err(Refusal::Invalid)?,
            );
        }
        let ids: Vec<i64> = sqlx::query_scalar(&format!(
            "INSERT INTO {table} (action_id, parent_id, index, key, key_path, kind, identifier, compiled_jsonpath, nullable, dimension)
             SELECT $1, parent_id, index, key, key_path, kind, identifier, compiled_jsonpath, nullable, dimension
               FROM unnest($2::bigint[], $3::int[], $4::varchar[], $5::varchar[], $6::varchar[], $7::varchar[],
                                      $8::text[], $9::bool[], $10::varchar[])
                    WITH ORDINALITY AS rows(parent_id, index, key, key_path, kind, identifier, compiled_jsonpath, nullable, dimension, n)
             ORDER BY n
             RETURNING id"
        ))
        .bind(action)
        .bind(level.iter().map(|(_, parent, ..)| *parent).collect::<Vec<_>>())
        .bind(level.iter().map(|(_, _, index, _)| *index).collect::<Vec<_>>())
        .bind(level.iter().map(|(port, ..)| port.key()).collect::<Vec<_>>())
        .bind(level.iter().map(|(.., path)| path.as_str()).collect::<Vec<_>>())
        .bind(level.iter().map(|(port, ..)| port.kind()).collect::<Vec<_>>())
        .bind(level.iter().map(|(port, ..)| port.identifier()).collect::<Vec<_>>())
        .bind(&compiled)
        .bind(level.iter().map(|(port, ..)| port.nullable()).collect::<Vec<_>>())
        .bind(level.iter().map(|(port, ..)| port.dimension()).collect::<Vec<_>>())
        .fetch_all(&mut *conn)
        .await?;
        level = level
            .iter()
            .zip(ids)
            .flat_map(|((port, _, _, path), id)| {
                port.children()
                    .iter()
                    .enumerate()
                    .map(move |(index, child)| {
                        (
                            child,
                            Some(id),
                            index as i32,
                            format!("{path}.{}", child.key()),
                        )
                    })
            })
            .collect();
    }
    Ok(())
}

/// Replace the action's relational port rows and root counts (`rebuild_relational_ports`).
async fn rebuild_relational_ports(
    conn: &mut PgConnection,
    action: &mut ActionRow,
    definition: &DefinitionInputModel,
) -> Result<(), Refusal> {
    sqlx::query("DELETE FROM facade_argport WHERE action_id = $1")
        .bind(action.id)
        .execute(&mut *conn)
        .await?;
    sqlx::query("DELETE FROM facade_returnport WHERE action_id = $1")
        .bind(action.id)
        .execute(&mut *conn)
        .await?;
    create_ports_level_by_level(conn, &definition.args, action.id, "facade_argport").await?;
    create_ports_level_by_level(conn, &definition.returns, action.id, "facade_returnport").await?;
    action.arg_count = definition.args.len() as i32;
    action.return_count = definition.returns.len() as i32;
    sqlx::query("UPDATE facade_action SET arg_count = $2, return_count = $3 WHERE id = $1")
        .bind(action.id)
        .bind(action.arg_count)
        .bind(action.return_count)
        .execute(&mut *conn)
        .await?;
    Ok(())
}

/// Whether the port rows already reflect the definition (`_relational_state_is_current`).
async fn relational_state_is_current(
    conn: &mut PgConnection,
    action: &ActionRow,
    definition: &DefinitionInputModel,
) -> Result<bool, sqlx::Error> {
    if action.arg_count as usize != definition.args.len()
        || action.return_count as usize != definition.returns.len()
    {
        return Ok(false);
    }
    let (need_args, need_returns) = (!definition.args.is_empty(), !definition.returns.is_empty());
    if !(need_args || need_returns) {
        return Ok(true);
    }
    let (has_args, has_returns): (bool, bool) = sqlx::query_as(
        "SELECT EXISTS (SELECT 1 FROM facade_argport WHERE action_id = $1),
                EXISTS (SELECT 1 FROM facade_returnport WHERE action_id = $1)",
    )
    .bind(action.id)
    .fetch_one(conn)
    .await?;
    Ok((has_args || !need_args) && (has_returns || !need_returns))
}

/// Django's `ManyToManyField.set`: drop the rows not wanted, add the missing ones.
async fn set_m2m(
    conn: &mut PgConnection,
    table: &str,
    owner_column: &str,
    target_column: &str,
    owner: i64,
    targets: &[i64],
) -> Result<(), sqlx::Error> {
    sqlx::query(&format!(
        "DELETE FROM {table} WHERE {owner_column} = $1 AND NOT ({target_column} = ANY($2))"
    ))
    .bind(owner)
    .bind(targets)
    .execute(&mut *conn)
    .await?;
    sqlx::query(&format!(
        "INSERT INTO {table} ({owner_column}, {target_column}) SELECT $1, unnest($2::bigint[])
         ON CONFLICT ({owner_column}, {target_column}) DO NOTHING"
    ))
    .bind(owner)
    .bind(targets)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// The `is_test_for` targets that exist in the organization (`_resolve_test_targets`); ones
/// that match nothing are skipped with a warning (registration order is not guaranteed).
async fn resolve_test_targets(
    conn: &mut PgConnection,
    definition: &DefinitionInputModel,
    agent: &AgentIdentity,
) -> Result<Vec<i64>, sqlx::Error> {
    let mut resolved = vec![];
    let mut unmatched = vec![];
    for target in &definition.is_test_for {
        let matches: Vec<i64> = if let Some(hash) = target.hash.as_deref().filter(|h| !h.is_empty())
        {
            sqlx::query_scalar(
                "SELECT id FROM facade_action WHERE organization_id = $1 AND hash = $2",
            )
            .bind(agent.organization)
            .bind(hash)
            .fetch_all(&mut *conn)
            .await?
        } else {
            sqlx::query_scalar(
                "SELECT a.id FROM facade_action a JOIN authentikate_app app ON app.id = a.app_id
                  WHERE a.organization_id = $1 AND a.key = $2
                    AND (CASE WHEN $3::varchar IS NOT NULL AND $3 <> '' THEN app.identifier = $3 ELSE a.app_id = $4 END)
                    AND ($5::varchar IS NULL OR $5 = '' OR a.version = $5)",
            )
            .bind(agent.organization)
            .bind(&target.key)
            .bind(&target.app)
            .bind(agent.app)
            .bind(&target.version)
            .fetch_all(&mut *conn)
            .await?
        };
        if matches.is_empty() {
            unmatched.push(dump(target));
        }
        resolved.extend(matches);
    }
    if !unmatched.is_empty() {
        tracing::warn!("Action {} declares is_test_for targets that are not registered (skipped): {unmatched:?}", definition.key);
    }
    resolved.sort_unstable();
    resolved.dedup();
    Ok(resolved)
}

/// Upsert the implementation's declared dependencies by `(implementation, key)` (`_sync_dependencies`).
/// Every field the declaration carries is written (`optional` and `description` were once
/// dropped); `assign_policy` keeps its model default, unread.
async fn sync_dependencies(
    conn: &mut PgConnection,
    implementation: i64,
    dependencies: &[AgentDependencyInputModel],
) -> Result<(), sqlx::Error> {
    for dependency in dependencies {
        let action_demands = dump(&dependency.action_dependencies.clone().unwrap_or_default());
        let state_demands = dump(&dependency.state_dependencies.clone().unwrap_or_default());
        let updated = sqlx::query(
            "UPDATE facade_dependency SET action_demands = $3, state_demands = $4, app_filter = $5, version_filter = $6,
                    min_viable_instances = $7, max_viable_instances = $8, prefered_instances = $9, auto_resolvable = $10,
                    optional = $11, description = $12
              WHERE id = (SELECT id FROM facade_dependency WHERE implementation_id = $1 AND key = $2 ORDER BY id LIMIT 1)",
        )
        .bind(implementation)
        .bind(&dependency.key)
        .bind(&action_demands)
        .bind(&state_demands)
        .bind(&dependency.app)
        .bind(&dependency.version)
        .bind(dependency.min_viable_instances)
        .bind(dependency.max_viable_instances)
        .bind(dependency.prefered_instances)
        .bind(dependency.auto_resolvable)
        .bind(dependency.optional)
        .bind(&dependency.description)
        .execute(&mut *conn)
        .await?;
        if updated.rows_affected() == 0 {
            sqlx::query(
                "INSERT INTO facade_dependency
                     (created_at, key, action_demands, state_demands, auto_resolvable, app_filter, version_filter, optional,
                      description, min_viable_instances, max_viable_instances, prefered_instances, assign_policy, implementation_id)
                 VALUES (now(), $2, $3, $4, $10, $5, $6, $11, $12, $7, $8, $9, 'AUTOMATIC', $1)",
            )
            .bind(implementation)
            .bind(&dependency.key)
            .bind(&action_demands)
            .bind(&state_demands)
            .bind(&dependency.app)
            .bind(&dependency.version)
            .bind(dependency.min_viable_instances)
            .bind(dependency.max_viable_instances)
            .bind(dependency.prefered_instances)
            .bind(dependency.auto_resolvable)
            .bind(dependency.optional)
            .bind(&dependency.description)
            .execute(&mut *conn)
            .await?;
        }
    }
    Ok(())
}

/// A registered implementation: its row and the diagnostics stored on it.
#[derive(Debug)]
pub struct Registered {
    pub id: i64,
    pub diagnostics: Vec<Diagnostic>,
}

/// Register one implementation (and its action) for the agent (`_create_implementation`).
pub async fn create_implementation(
    conn: &mut PgConnection,
    input: &ImplementationInputModel,
    agent: &AgentIdentity,
    prefetch: &mut Prefetch,
    on_commit: &mut OnCommit,
) -> Result<Registered, Refusal> {
    let definition = &input.definition;
    let scope = infer_action_scope(definition).value();
    let idempotent = validate_qualifiers(definition, input.effects)?;
    let diagnostics = collect_diagnostics(
        conn,
        definition,
        agent.organization,
        input.optimistics.as_deref(),
    )
    .await?;
    let stored_diagnostics = dump_diagnostics(&diagnostics);

    let (mut action, definition_changed) = upsert_action(
        conn, definition, agent, scope, idempotent, prefetch, on_commit,
    )
    .await?;

    // Qualifiers are not part of the hash: synced on every registration.
    let port_groups = dump(&definition.port_groups);
    if action.pure != definition.pure
        || action.idempotent != idempotent
        || action.allow_probe != definition.allow_probe
        || action.is_dev != definition.is_dev
        || action.kind != definition.kind.value()
        || action.port_groups != port_groups
    {
        sqlx::query("UPDATE facade_action SET pure = $2, idempotent = $3, allow_probe = $4, is_dev = $5, kind = $6, port_groups = $7 WHERE id = $1")
            .bind(action.id)
            .bind(definition.pure)
            .bind(idempotent)
            .bind(definition.allow_probe)
            .bind(definition.is_dev)
            .bind(definition.kind.value())
            .bind(&port_groups)
            .execute(&mut *conn)
            .await?;
        action.pure = definition.pure;
        action.idempotent = idempotent;
        action.allow_probe = definition.allow_probe;
        action.is_dev = definition.is_dev;
        action.kind = definition.kind.value().to_owned();
        action.port_groups = port_groups;
        on_commit.push(Signal::ActionSaved {
            id: action.id,
            created: false,
        });
    }

    if definition_changed || !relational_state_is_current(conn, &action, definition).await? {
        rebuild_relational_ports(conn, &mut action, definition).await?;
        let protocols = infer_protocols(conn, definition, agent.organization).await?;
        set_m2m(
            conn,
            "facade_action_protocols",
            "action_id",
            "protocol_id",
            action.id,
            &protocols,
        )
        .await?;
        let targets = resolve_test_targets(conn, definition, agent).await?;
        set_m2m(
            conn,
            "facade_action_is_test_for",
            "from_action_id",
            "to_action_id",
            action.id,
            &targets,
        )
        .await?;
        let mut collections = vec![];
        for name in &definition.collections {
            sqlx::query(
                "INSERT INTO facade_collection (defined_at, name, description, updated_at, creator_id, organization_id)
                 VALUES (now(), $1, '', now(), $2, $3) ON CONFLICT (name) DO NOTHING",
            )
            .bind(name)
            .bind(agent.user)
            .bind(agent.organization)
            .execute(&mut *conn)
            .await?;
            collections.push(
                sqlx::query_scalar::<_, i64>("SELECT id FROM facade_collection WHERE name = $1")
                    .bind(name)
                    .fetch_one(&mut *conn)
                    .await?,
            );
        }
        set_m2m(
            conn,
            "facade_action_collections",
            "action_id",
            "collection_id",
            action.id,
            &collections,
        )
        .await?;
        on_commit.push(Signal::ActionSaved {
            id: action.id,
            created: false,
        });
        prefetch
            .actions
            .insert((action.key.clone(), action.version.clone()), action.clone());
    }

    // The provenance audience: declared, or derived once from the stored ports.
    let audience = match &input.provenance_audience {
        Some(declared) => Some(dump(declared)),
        None => Some(audience::derive_from_action(
            action.args.as_array().map_or(&[][..], Vec::as_slice),
            action.returns.as_array().map_or(&[][..], Vec::as_slice),
        ))
        .filter(|a| !a.is_empty())
        .map(|a| dump(&a)),
    };
    let params = Value::Object(input.params.clone().unwrap_or_default());

    let existing = prefetch.implementations.get(&input.interface).cloned();
    let (implementation, created) = match existing {
        Some(existing) => {
            let mut recreate = false;
            if existing.action_id != action.id {
                let count: i64 = sqlx::query_scalar(
                    "SELECT count(*) FROM facade_implementation WHERE action_id = $1",
                )
                .bind(existing.action_id)
                .fetch_one(&mut *conn)
                .await?;
                if count == 1 {
                    // Django deletes the old action, which cascades to this very implementation;
                    // the `save()` that follows finds no row to update and inserts it again
                    // under the same id.
                    for deleted in delete_actions(conn, &[existing.action_id]).await? {
                        recreate |= deleted.id == existing.id;
                        on_commit.push(Signal::ImplementationDeleted {
                            id: deleted.id,
                            agent_id: deleted.agent_id,
                        });
                    }
                }
            }
            if recreate {
                sqlx::query(
                    "INSERT INTO facade_implementation
                         (id, interface, name, policy, higher_order_config, params, created_at, updated_at, tracks, diagnostics,
                          needs_token, provenance_audience, effects, execution, code_hash, action_id, agent_id,
                          higher_order_for_id, release_id)
                     VALUES ($1, $2, $3, $4, $5, $6, $7, now(), $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)",
                )
                .bind(existing.id)
                .bind(&existing.interface)
                .bind(&existing.name)
                .bind(&existing.policy)
                .bind(&existing.higher_order_config)
                .bind(&params)
                .bind(existing.created_at)
                .bind(&existing.tracks)
                .bind(&stored_diagnostics)
                .bind(input.needs_token)
                .bind(&audience)
                .bind(input.effects.value())
                .bind(input.execution.value())
                .bind(&input.code_hash)
                .bind(action.id)
                .bind(agent.id)
                // Nulled by the cascade only if it pointed at a row that went with the action.
                .bind(existing.higher_order_for_id)
                .bind(agent.release)
                .execute(&mut *conn)
                .await?;
            } else {
                sqlx::query(
                    "UPDATE facade_implementation SET action_id = $2, params = $3, release_id = $4, needs_token = $5,
                            provenance_audience = $6, effects = $7, execution = $8, code_hash = $9, diagnostics = $10,
                            updated_at = now()
                      WHERE id = $1",
                )
                .bind(existing.id)
                .bind(action.id)
                .bind(&params)
                .bind(agent.release)
                .bind(input.needs_token)
                .bind(&audience)
                .bind(input.effects.value())
                .bind(input.execution.value())
                .bind(&input.code_hash)
                .bind(&stored_diagnostics)
                .execute(&mut *conn)
                .await?;
            }
            prefetch.implementations.insert(
                input.interface.clone(),
                ImplementationRow {
                    action_id: action.id,
                    ..existing.clone()
                },
            );
            (existing.id, recreate)
        }
        None => {
            let row: ImplementationRow = sqlx::query_as(
                "INSERT INTO facade_implementation
                     (interface, name, policy, higher_order_config, params, created_at, updated_at, tracks, diagnostics,
                      needs_token, provenance_audience, effects, execution, code_hash, action_id, agent_id, release_id)
                 VALUES ($1, 'Unnamed', '{}', '{}', $2, now(), now(), '[]', $3, $4, $5, $6, $7, $8, $9, $10, $11)
                 RETURNING id, interface, action_id, name, policy, higher_order_config, higher_order_for_id, tracks, created_at",
            )
            .bind(&input.interface)
            .bind(&params)
            .bind(&stored_diagnostics)
            .bind(input.needs_token)
            .bind(&audience)
            .bind(input.effects.value())
            .bind(input.execution.value())
            .bind(&input.code_hash)
            .bind(action.id)
            .bind(agent.id)
            .bind(agent.release)
            .fetch_one(&mut *conn)
            .await?;
            let id = row.id;
            prefetch
                .implementations
                .insert(input.interface.clone(), row);
            (id, true)
        }
    };
    on_commit.push(Signal::ImplementationSaved {
        id: implementation,
        created,
    });

    sync_dependencies(conn, implementation, &input.dependencies).await?;

    if let Some(manipulates) = input.manipulates.as_ref().filter(|m| !m.is_empty()) {
        let states: Vec<i64> = sqlx::query_scalar(
            "SELECT id FROM facade_state WHERE agent_id = $1 AND interface = ANY($2)",
        )
        .bind(agent.id)
        .bind(manipulates)
        .fetch_all(&mut *conn)
        .await?;
        set_m2m(
            conn,
            "facade_implementation_manipulates",
            "implementation_id",
            "state_id",
            implementation,
            &states,
        )
        .await?;
    }

    if let Some(tracks) = input.tracks.as_ref().filter(|t| !t.is_empty()) {
        sqlx::query(
            "UPDATE facade_implementation SET tracks = $2, updated_at = now() WHERE id = $1",
        )
        .bind(implementation)
        .bind(dump(tracks))
        .execute(&mut *conn)
        .await?;
        on_commit.push(Signal::ImplementationSaved {
            id: implementation,
            created: false,
        });
    }

    Ok(Registered {
        id: implementation,
        diagnostics,
    })
}
