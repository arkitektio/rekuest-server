//! Creating a higher-order implementation: a wrapper deployed onto the agent of the
//! implementation it wraps (`createHigherOrderImplementation`).
//!
//! The caller (a UI, fluss) supplies the wrapper's typed definition and the projection config;
//! the server stays agnostic of what is wrapped. The wrapper is registered exactly as a declared
//! implementation is (the action upsert by hash, its port rows, diagnostics) on the lower's
//! agent, then linked to the lower, in one transaction. Re-registering the agent keeps it: the
//! reap of undeclared implementations skips wrappers.

use rekuest_core::inputs::{
    AgentDependencyInputModel, DefinitionInputModel, ImplementationInputModel,
};
use serde::Deserialize;
use serde_json::{json, Value};

use super::implementation::{create_implementation, AgentIdentity, Prefetch};
use super::Refusal;
use crate::catalog_validation::Diagnostic;
use crate::context::Context;
use crate::higher_order::{validate_dependency_coverage, validate_higher_order_pairing};
use crate::registration_lock::lock_organization;
use crate::signals::{OnCommit, Signal};

/// What a caller sends (`CreateHigherOrderImplementationInput`).
#[derive(Debug, Clone, Deserialize)]
pub struct CreateHigherOrderInput {
    /// The implementation to wrap; its agent hosts the wrapper.
    pub lower: String,
    /// The wrapper's interface, unique on that agent (e.g. `flow:123`).
    pub interface: String,
    pub definition: DefinitionInputModel,
    /// `bound`, `arg_map`, `args_key`, `dependency_map`, `return_map` (see [`crate::higher_order`]).
    #[serde(default)]
    pub config: Option<Value>,
    /// The dependencies the wrapper declares, for a `dependency_map` sourcing `from: caller`.
    #[serde(default)]
    pub dependencies: Vec<AgentDependencyInputModel>,
}

/// The wrapper created (or updated in place), and its diagnostics.
#[derive(Debug)]
pub struct Created {
    pub implementation: i64,
    pub diagnostics: Vec<Diagnostic>,
}

#[derive(sqlx::FromRow)]
struct Lower {
    id: i64,
    agent_id: i64,
    interface: String,
    higher_order_for_id: Option<i64>,
    kind: String,
    organization_id: i64,
    app_id: i64,
    release_id: i64,
    user_id: i64,
}

/// Create (or redeploy) a wrapper of `input.lower` for a caller of `organization`.
pub async fn create_higher_order_implementation(
    ctx: &Context,
    organization: i64,
    input: CreateHigherOrderInput,
) -> Result<Created, Refusal> {
    let config = input.config.clone().unwrap_or_else(|| json!({}));
    if !config.is_object() {
        return Err(Refusal::Invalid(
            "The higher-order config must be an object".into(),
        ));
    }
    let lower_id: i64 = input
        .lower
        .parse()
        .map_err(|_| Refusal::Invalid(format!("Invalid implementation id {:?}", input.lower)))?;
    let mut implementation: ImplementationInputModel = serde_json::from_value(json!({
        "definition": input.definition,
        "interface": input.interface,
        "dependencies": input.dependencies,
    }))
    .map_err(|e| Refusal::Invalid(e.to_string()))?;
    implementation
        .validate()
        .map_err(|e| Refusal::Invalid(e.0))?;

    let lower: Option<Lower> = sqlx::query_as(
        "SELECT i.id, i.agent_id, i.interface, i.higher_order_for_id, a.kind,
                ag.organization_id, ag.app_id, ag.release_id, ag.user_id
           FROM facade_implementation i
           JOIN facade_action a ON a.id = i.action_id
           JOIN facade_agent ag ON ag.id = i.agent_id
          WHERE i.id = $1",
    )
    .bind(lower_id)
    .fetch_optional(&ctx.db)
    .await?;
    let Some(lower) = lower.filter(|l| l.organization_id == organization) else {
        // Another organization's implementation is indistinguishable from a missing one.
        return Err(Refusal::Invalid(
            "Implementation matching query does not exist.".into(),
        ));
    };
    if lower.interface == implementation.interface {
        return Err(Refusal::Invalid(
            "An implementation cannot wrap itself".into(),
        ));
    }
    validate_higher_order_pairing(
        implementation.definition.kind.value(),
        &lower.kind,
        lower.higher_order_for_id.is_some(),
    )?;
    let lower_dependencies: Vec<String> = sqlx::query_scalar(
        "SELECT key FROM facade_dependency WHERE implementation_id = $1 ORDER BY key",
    )
    .bind(lower.id)
    .fetch_all(&ctx.db)
    .await?;
    let declared: Vec<String> = implementation
        .dependencies
        .iter()
        .map(|d| d.key.clone())
        .collect();
    validate_dependency_coverage(&config, &lower_dependencies, &declared)?;

    let mut tx = ctx.db.begin().await?;
    lock_organization(&mut tx, lower.organization_id).await?;
    // The interface may be taken only by an earlier wrapper of the same lower: a redeploy.
    let occupant: Option<Option<i64>> = sqlx::query_scalar(
        "SELECT higher_order_for_id FROM facade_implementation WHERE agent_id = $1 AND interface = $2",
    )
    .bind(lower.agent_id)
    .bind(&implementation.interface)
    .fetch_optional(&mut *tx)
    .await?;
    if matches!(occupant, Some(occupant) if occupant != Some(lower.id)) {
        return Err(Refusal::Invalid(format!(
            "Interface {} is already implemented on this agent by something other than a wrapper of implementation {}",
            implementation.interface, lower.id
        )));
    }
    let identity = AgentIdentity {
        id: lower.agent_id,
        app: lower.app_id,
        release: lower.release_id,
        user: lower.user_id,
        organization: lower.organization_id,
    };
    let wanted = [(
        implementation.definition.key.clone(),
        implementation.definition.version.clone(),
    )];
    let mut prefetch = Prefetch::load(&mut tx, &identity, &wanted).await?;
    let mut on_commit = OnCommit::default();
    let registered = create_implementation(
        &mut tx,
        &implementation,
        &identity,
        &mut prefetch,
        &mut on_commit,
    )
    .await?;
    sqlx::query(
        "UPDATE facade_implementation SET higher_order_for_id = $2, higher_order_config = $3, updated_at = now() WHERE id = $1",
    )
    .bind(registered.id)
    .bind(lower.id)
    .bind(&config)
    .execute(&mut *tx)
    .await?;
    on_commit.push(Signal::ImplementationSaved {
        id: registered.id,
        created: false,
    });
    tx.commit().await?;
    on_commit.publish(ctx).await;
    Ok(Created {
        implementation: registered.id,
        diagnostics: registered.diagnostics,
    })
}
