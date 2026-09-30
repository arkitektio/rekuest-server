//! Create and control probes (`facade/probes/backend.py`).
//!
//! Shaped like the postman backend but persisting nothing: state goes to the redis hash, the
//! agent receives a normal `ASSIGN` whose `task` is the probe id, on the priority lane. Probes
//! refuse what needs a task row to be sound: higher-order implementations, implementations with
//! dependencies, parents, hooks, capture. They are always provenance roots.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::backend::{
    get_caller_for_context, resolve_direct_target, AssignInput, BackendError, BackendResult,
};
use crate::caller_context::CallerContext;
use crate::context::Context;
use crate::messages::{Assign, ToAgent};
use crate::persist::caller_ops;
use crate::probes::new_probe_id;
use crate::probes::persist::{publish, ProbeEvent};
use crate::probes::store::{self, NewProbe, ProbeState};
use crate::provenance::{mint_token_for_task, MintTask};
use crate::transport;

/// What a probe asks for (`ProbeInputModel`).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ProbeInput {
    #[serde(default)]
    pub action: Option<String>,
    #[serde(default)]
    pub action_hash: Option<String>,
    #[serde(default)]
    pub implementation: Option<String>,
    #[serde(default)]
    pub args: Map<String, Value>,
    #[serde(default)]
    pub reference: Option<String>,
}

fn redis_error(e: redis::RedisError) -> BackendError {
    BackendError::Refused(format!("probe store: {e}"))
}

/// Create and dispatch a probe (`probe`); its stored state, with `id`. `origin` is who fired it:
/// `graphql`, or `agent` (its events then mirror onto the requester's caller topic).
pub async fn probe(
    ctx: &Context,
    principal: &CallerContext,
    input: &ProbeInput,
    origin: &str,
) -> BackendResult<ProbeState> {
    let Some(organization) = principal.organization else {
        return Err(BackendError::Refused(
            "Cannot probe without an organization".into(),
        ));
    };
    let caller = get_caller_for_context(&ctx.db, principal).await?;
    let target = AssignInput {
        action: input.action.clone(),
        action_hash: input.action_hash.clone(),
        implementation: input.implementation.clone(),
        ..AssignInput::default()
    };
    let (action, implementation) = resolve_direct_target(ctx, &target, organization).await?;
    if !action.allow_probe {
        return Err(BackendError::Refused(format!(
            "Action {} does not allow probes — its author must declare allow_probe. Use assign.",
            action.name
        )));
    }
    if implementation.higher_order_for_id.is_some() {
        return Err(BackendError::Refused(
            "Probes cannot target higher-order implementations — their orchestration needs persisted tasks. Use assign.".into(),
        ));
    }
    let has_dependencies: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM facade_dependency WHERE implementation_id = $1)",
    )
    .bind(implementation.id)
    .fetch_one(&ctx.db)
    .await?;
    if has_dependencies {
        return Err(BackendError::Refused(
            "Probes cannot target implementations with dependencies — sub-assignment needs a parent task. Use assign.".into(),
        ));
    }
    if !store::try_acquire_slot(ctx, caller)
        .await
        .map_err(redis_error)?
    {
        return Err(BackendError::Refused(
            "Too many in-flight probes for this caller — cancel or await some, or raise PROBE_MAX_INFLIGHT_PER_CALLER.".into(),
        ));
    }

    let probe = new_probe_id();
    let created = async {
        let mut conn = ctx.db.acquire().await?;
        let token = mint_token_for_task(
            &mut conn,
            &ctx.settings,
            &MintTask {
                id: probe.clone(),
                parent_id: None,
                implementation_id: implementation.id,
                agent_id: implementation.agent_id,
                args: &input.args,
            },
            principal,
        )
        .await?;
        drop(conn);
        let org = principal.organization_slug.clone().unwrap_or_default();
        let state = store::create(
            ctx,
            &probe,
            &NewProbe {
                agent: implementation.agent_id,
                caller,
                user_sub: &principal.user_sub,
                org_slug: &org,
                action: action.id,
                implementation: implementation.id,
                interface: &implementation.interface,
                reference: input.reference.as_deref(),
                origin,
            },
        )
        .await
        .map_err(redis_error)?;
        transport::deliver_to_agent(
            ctx,
            implementation.agent_id,
            ToAgent::Assign(Box::new(Assign {
                id: None,
                interface: implementation.interface.clone(),
                task: probe.clone(),
                root: None,
                parent: None,
                resolution: None,
                step: None,
                probe: true,
                capture: Some(false),
                reference: input.reference.clone(),
                args: input.args.clone(),
                message: None,
                user: principal.user_sub.clone(),
                org,
                action: action.hash.clone(),
                implementation: implementation.id.to_string(),
                token,
                resume: None,
            })),
            true,
        )
        .await
        .map_err(|e| BackendError::Refused(e.to_string()))?;
        Ok::<_, BackendError>(state)
    }
    .await;
    match created {
        Ok(mut state) => {
            state.insert("id".into(), probe);
            Ok(state)
        }
        Err(e) => {
            if let Err(release) = store::release_slot(ctx, caller).await {
                tracing::error!("releasing a probe slot failed: {release}");
            }
            Err(e)
        }
    }
}

/// A probe control: its `-ING` kind, its frame, and the verb its refusal names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProbeControl {
    Cancel,
    Pause,
    Resume,
}

impl ProbeControl {
    fn parts(self, probe: &str) -> (&'static str, ToAgent, &'static str) {
        let task = probe.to_owned();
        match self {
            ProbeControl::Cancel => ("CANCELLING", ToAgent::Cancel { task }, "cancel"),
            ProbeControl::Pause => ("PAUSING", ToAgent::Pause { task }, "pause"),
            ProbeControl::Resume => ("RESUMING", ToAgent::Resume { task, step: false }, "resume"),
        }
    }
}

/// The request phase of a probe control (`_control`). A finished probe is not an error: its
/// state comes back and the caller moves on, since controls race completion all the time.
pub async fn control(
    ctx: &Context,
    principal: &CallerContext,
    probe: &str,
    control: ProbeControl,
) -> BackendResult<ProbeState> {
    let (inging, message, verb) = control.parts(probe);
    let caller = get_caller_for_context(&ctx.db, principal).await?;
    let Some(mut state) = store::get(ctx, probe).await.map_err(redis_error)? else {
        return Err(BackendError::Refused(format!(
            "Unknown or expired probe {probe}"
        )));
    };
    if state.get("caller") != Some(&caller.to_string()) {
        return Err(BackendError::Forbidden(format!(
            "Not authorized to {verb} this probe (not its caller)."
        )));
    }
    if state.get("done").is_some_and(|d| !d.is_empty()) {
        state.insert("id".into(), probe.to_owned());
        return Ok(state);
    }
    if let Some((seq, event_caller, origin)) = store::record_nonterminal(ctx, probe, inging, None)
        .await
        .map_err(redis_error)?
    {
        publish(
            ctx,
            probe,
            inging,
            seq,
            ProbeEvent::default(),
            event_caller.as_deref(),
            &origin,
        )
        .await;
    }
    let agent = state
        .get("agent")
        .and_then(|a| a.parse().ok())
        .unwrap_or_default();
    transport::deliver_to_agent(ctx, agent, message, true)
        .await
        .map_err(|e| BackendError::Refused(e.to_string()))?;
    let mut state = store::get(ctx, probe)
        .await
        .map_err(redis_error)?
        .unwrap_or(state);
    state.insert("id".into(), probe.to_owned());
    Ok(state)
}

/// A probe an agent fires over its socket (`probe_for_agent`): under its own identity, with
/// `origin = "agent"`. Every guard applies unchanged.
pub async fn probe_for_agent(
    ctx: &Context,
    agent: i64,
    input: &ProbeInput,
) -> BackendResult<ProbeState> {
    let caller = caller_ops::get_or_create_caller_id(&ctx.db, agent).await?;
    let principal = caller_ops::caller_context(&ctx.db, agent, caller).await?;
    probe(ctx, &principal, input, "agent").await
}
