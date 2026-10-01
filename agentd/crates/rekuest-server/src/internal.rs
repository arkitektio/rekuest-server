//! The internal API: the Python server's GraphQL mutations, handed to agentd.
//!
//! Every route is `POST {force_script_name}/internal/<op>` with a JSON body, and answers JSON.
//! Nothing here is public API: only the rekuest server beside agentd may call it.
//!
//! # Authentication
//!
//! A service token, exactly as `rekuest_service.trust.sign` makes one:
//!
//! ```text
//! Authorization: RekuestService <jwt>
//! header  {"alg": "Ed25519", "kid": <RFC 7638 thumbprint of instance.private_key>, "typ": "rekuest-service+jwt"}
//! claims  {"iss": <rekuest.identifier>, "aud": <rekuest.identifier>, "iat", "exp" (iat + 60),
//!          "jti", "htm": "POST", "htu": <the full request path, prefix included>,
//!          "bh": <base64url sha256 of the body, unpadded>}
//! ```
//!
//! The rekuest server and agentd read the same `config.yaml`, so they hold the same instance key:
//! in Python, `trust.sign("POST", path, body, issuer=rekuest_identifier(),
//! audience=rekuest_identifier())`. A token is accepted once (its `jti` is claimed in redis) and
//! only within ±30 s of its window. Refusals: `401 {"error"}` (no or bad token), `409 {"error"}`
//! (a replayed token).
//!
//! # The principal
//!
//! The GraphQL request's identity, which the backend reads (`CallerContext`), as primary keys:
//!
//! ```json
//! {"user": 1, "client": 2, "organization": 3, "roles": ["admin"]}
//! ```
//!
//! Ids may be numbers or numeric strings. `roles` are the membership's (`request.membership.roles`).
//!
//! # Routes
//!
//! | route | body | answer |
//! |---|---|---|
//! | `assign` | `{"principal", "input": AssignInput, "schedule"?, "signal"?, "trigger"?, "trigger_depth"?}` | `{"task", "reference", "created"}` |
//! | `cancel`, `interrupt`, `pause` | `{"principal"?, "task"}` | `{"task"}` |
//! | `resume` | `{"principal"?, "task", "step"?}` | `{"task"}` |
//! | `bounce`, `kick`, `unblock` | `{"principal", "agent"}` | `{"agent"}` |
//! | `block` | `{"principal", "agent", "reason"?}` | `{"agent"}` |
//! | `collect` | `{"principal", "drawers": [id, …]}` | `{"drawers"}` |
//! | `probe` | `{"principal", "input": ProbeInput}` | the probe's state, with `id` |
//! | `probe/cancel`, `probe/pause`, `probe/resume` | `{"principal", "probe"}` | the probe's state, with `id` |
//! | `agent/ensure` | `{"principal", "name"?, "description"?, "kind"?, "hook_url"?, "hook_url_secret"?, "clear_drawers"?}` (a present null clears) | `{"agent"}` |
//! | `agent/implement` | `{"principal", "input": ImplementAgentInput}` | `{"agent", "diagnostics"}` |
//! | `agent/delete` | `{"principal", "agent"}` (kicks it, deletes it with everything below it) | `{"agent"}` |
//! | `implementation/delete` | `{"principal", "implementation"}` | `{"implementation"}` |
//! | `drawer/shelve` | `{"principal", "identifier", "resource_id", "label"?, "description"?}` | `{"drawer"}` |
//! | `drawer/unshelve` | `{"principal", "id"}` (a resource id, else a drawer id) | `{"drawer"}` |
//! | `higher-order/create` | `{"principal", "input": {"lower", "interface", "definition", "config"?, "dependencies"?}}` | `{"implementation", "diagnostics"}` |
//!
//! `AssignInput` is `AssignInputModel`'s JSON (`action`, `action_hash`, `implementation`,
//! `agent` + `interface`, `dependency` + `method`, `args`, `reference`, `parent`, `parent_step`,
//! `call_key`, `hooks`, `capture`, `step`, `resolution`, `dependencies`, `not_before`);
//! `ProbeInput` is `ProbeInputModel`'s (`action`, `action_hash`, `implementation`, `args`,
//! `reference`). Ids in answers are strings. A control without a principal is an internal,
//! trusted one (`caller=None`); with one, the task must be in the principal's organization.
//! Refused ops answer `400 {"error"}` (the Python `ValueError`) or `403 {"error"}` (its
//! `PermissionError`), with the Python server's messages.

use axum::{
    body::Bytes,
    extract::{OriginalUri, State},
    http::{HeaderMap, Method, StatusCode},
    response::{IntoResponse, Response},
    routing::post,
    Json, Router,
};
use facade::backend::{self, AssignInput, AssignOrigin, BackendError};
use facade::caller_context::CallerContext;
use facade::probes::backend::{self as probe_backend, ProbeControl, ProbeInput};
use facade::service_trust;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::urls::Shared;

/// The internal routes, to be nested under the configuration's prefix.
pub fn routes() -> Router<Shared> {
    Router::new()
        .route("/internal/assign", post(assign))
        .route("/internal/cancel", post(cancel))
        .route("/internal/interrupt", post(interrupt))
        .route("/internal/pause", post(pause))
        .route("/internal/resume", post(resume))
        .route("/internal/bounce", post(bounce))
        .route("/internal/kick", post(kick))
        .route("/internal/block", post(block))
        .route("/internal/unblock", post(unblock))
        .route("/internal/collect", post(collect))
        .route("/internal/probe", post(probe))
        .route("/internal/probe/cancel", post(probe_cancel))
        .route("/internal/probe/pause", post(probe_pause))
        .route("/internal/probe/resume", post(probe_resume))
        .route("/internal/higher-order/create", post(create_higher_order))
        .route("/internal/agent/ensure", post(ensure_agent))
        .route("/internal/agent/implement", post(implement_agent))
        .route("/internal/agent/delete", post(delete_agent))
        .route("/internal/implementation/delete", post(delete_implementation))
        .route("/internal/drawer/shelve", post(shelve))
        .route("/internal/drawer/unshelve", post(unshelve))
}

/// A refusal, as JSON.
struct Refusal(StatusCode, String);

impl IntoResponse for Refusal {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error": self.1}))).into_response()
    }
}

impl From<BackendError> for Refusal {
    fn from(e: BackendError) -> Self {
        match e {
            BackendError::Refused(message) => Refusal(StatusCode::BAD_REQUEST, message),
            BackendError::Forbidden(message) => Refusal(StatusCode::FORBIDDEN, message),
            BackendError::Database(e) => {
                tracing::error!("internal API: {e}");
                Refusal(StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
            }
        }
    }
}

impl From<facade::mutations::Refusal> for Refusal {
    fn from(e: facade::mutations::Refusal) -> Self {
        match e {
            facade::mutations::Refusal::Invalid(message) => {
                Refusal(StatusCode::BAD_REQUEST, message)
            }
            facade::mutations::Refusal::Db(e) => BackendError::Database(e).into(),
        }
    }
}

type Answer = Result<Json<Value>, Refusal>;

/// An id as a number or a numeric string.
#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
enum Id {
    Number(i64),
    Text(String),
}

impl Id {
    fn get(&self) -> Result<i64, Refusal> {
        match self {
            Id::Number(id) => Ok(*id),
            Id::Text(id) => backend::parse_id(id).map_err(Refusal::from),
        }
    }

    fn text(&self) -> String {
        match self {
            Id::Number(id) => id.to_string(),
            Id::Text(id) => id.clone(),
        }
    }
}

#[derive(Debug, Deserialize)]
struct Principal {
    user: Option<Id>,
    client: Option<Id>,
    organization: Option<Id>,
    #[serde(default)]
    roles: Vec<String>,
}

impl Principal {
    fn organization(&self) -> Result<i64, Refusal> {
        match &self.organization {
            Some(organization) => organization.get(),
            None => Err(Refusal(
                StatusCode::BAD_REQUEST,
                "The principal has no organization".into(),
            )),
        }
    }

    /// `(client, user, organization)`: whose agent an agent route acts on.
    fn identity(&self) -> Result<(i64, i64, i64), Refusal> {
        let (Some(user), Some(client)) = (&self.user, &self.client) else {
            return Err(Refusal(
                StatusCode::BAD_REQUEST,
                "The principal needs a user and a client".into(),
            ));
        };
        Ok((client.get()?, user.get()?, self.organization()?))
    }

    async fn context(&self, state: &Shared) -> Result<CallerContext, Refusal> {
        let (Some(user), Some(client)) = (&self.user, &self.client) else {
            return Err(Refusal(
                StatusCode::BAD_REQUEST,
                "The principal needs a user and a client".into(),
            ));
        };
        let organization = self.organization.as_ref().map(Id::get).transpose()?;
        CallerContext::load(
            &state.facade.db,
            user.get()?,
            client.get()?,
            organization,
            self.roles.clone(),
        )
        .await
        .map_err(|e| match e {
            sqlx::Error::RowNotFound => Refusal(
                StatusCode::BAD_REQUEST,
                "The principal names no known user and client".into(),
            ),
            e => BackendError::Database(e).into(),
        })
    }
}

/// Verify the service token and parse the body.
async fn authorize<T: for<'de> Deserialize<'de>>(
    state: &Shared,
    method: &Method,
    uri: &axum::http::Uri,
    headers: &HeaderMap,
    body: &Bytes,
) -> Result<T, Refusal> {
    let facade = &state.facade;
    let Some(key) = facade.settings.instance_key.as_deref() else {
        return Err(Refusal(
            StatusCode::UNAUTHORIZED,
            "No instance key configured".into(),
        ));
    };
    let authorization = headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok());
    let verified = service_trust::verify(
        key,
        method.as_str(),
        uri.path(),
        body,
        authorization,
        &facade.settings.rekuest_identifier,
    )
    .map_err(|e| Refusal(StatusCode::UNAUTHORIZED, e.0))?;
    match service_trust::claim(facade, &verified).await {
        Ok(true) => {}
        Ok(false) => {
            return Err(Refusal(
                StatusCode::CONFLICT,
                "Replayed service token".into(),
            ))
        }
        // Fail closed: without the guard, knocking redis over would allow replays.
        Err(e) => {
            tracing::error!("internal API replay guard unavailable: {e}");
            return Err(Refusal(
                StatusCode::SERVICE_UNAVAILABLE,
                "Replay guard unavailable".into(),
            ));
        }
    }
    serde_json::from_slice(body).map_err(|e| Refusal(StatusCode::BAD_REQUEST, e.to_string()))
}

/// The handler signature every route shares.
macro_rules! internal {
    ($name:ident, $body:ty, |$state:ident, $request:ident| $run:expr) => {
        async fn $name(
            State($state): State<Shared>,
            method: Method,
            OriginalUri(uri): OriginalUri,
            headers: HeaderMap,
            body: Bytes,
        ) -> Answer {
            let $request: $body = authorize(&$state, &method, &uri, &headers, &body).await?;
            $run
        }
    };
}

#[derive(Debug, Deserialize)]
struct AssignRequest {
    principal: Principal,
    input: AssignInput,
    #[serde(default)]
    schedule: Option<Id>,
    #[serde(default)]
    signal: Option<Id>,
    #[serde(default)]
    trigger: Option<Id>,
    #[serde(default)]
    trigger_depth: i16,
}

internal!(assign, AssignRequest, |state, request| {
    let principal = request.principal.context(&state).await?;
    let origin = AssignOrigin {
        schedule: request.schedule.as_ref().map(Id::get).transpose()?,
        signal: request.signal.as_ref().map(Id::get).transpose()?,
        trigger: request.trigger.as_ref().map(Id::get).transpose()?,
        trigger_depth: request.trigger_depth,
    };
    let assigned =
        backend::assign_with_status(&state.facade, &principal, &request.input, origin).await?;
    Ok(Json(json!({
        "task": assigned.task.to_string(),
        "reference": assigned.reference,
        "created": assigned.created,
    })))
});

#[derive(Debug, Deserialize)]
struct ControlRequest {
    #[serde(default)]
    principal: Option<Principal>,
    task: Id,
    #[serde(default)]
    step: bool,
}

/// The caller of a control: the principal's (`get_caller_for_context`), or none (trusted).
async fn control(state: &Shared, request: &ControlRequest, control: backend::Control) -> Answer {
    let caller = match &request.principal {
        Some(principal) => {
            let principal = principal.context(state).await?;
            Some(backend::get_caller_for_context(&state.facade.db, &principal).await?)
        }
        None => None,
    };
    let task =
        backend::request_control(&state.facade, &request.task.text(), control, caller).await?;
    Ok(Json(json!({"task": task.to_string()})))
}

internal!(cancel, ControlRequest, |state, request| control(
    &state,
    &request,
    backend::Control::Cancel
)
.await);
internal!(interrupt, ControlRequest, |state, request| control(
    &state,
    &request,
    backend::Control::Interrupt
)
.await);
internal!(pause, ControlRequest, |state, request| control(
    &state,
    &request,
    backend::Control::Pause
)
.await);
internal!(resume, ControlRequest, |state, request| {
    let step = request.step;
    control(&state, &request, backend::Control::Resume { step }).await
});

#[derive(Debug, Deserialize)]
struct AgentRequest {
    principal: Principal,
    agent: Id,
    #[serde(default)]
    reason: Option<String>,
}

internal!(bounce, AgentRequest, |state, request| {
    let organization = request.principal.organization()?;
    let agent = backend::bounce(&state.facade, organization, &request.agent.text()).await?;
    Ok(Json(json!({"agent": agent.to_string()})))
});
internal!(kick, AgentRequest, |state, request| {
    let organization = request.principal.organization()?;
    let agent = backend::kick(&state.facade, organization, &request.agent.text()).await?;
    Ok(Json(json!({"agent": agent.to_string()})))
});
internal!(block, AgentRequest, |state, request| {
    let organization = request.principal.organization()?;
    let agent = backend::block(
        &state.facade,
        organization,
        &request.agent.text(),
        request.reason.clone(),
    )
    .await?;
    Ok(Json(json!({"agent": agent.to_string()})))
});
internal!(unblock, AgentRequest, |state, request| {
    let organization = request.principal.organization()?;
    let agent = backend::unblock(&state.facade, organization, &request.agent.text()).await?;
    Ok(Json(json!({"agent": agent.to_string()})))
});

internal!(delete_agent, AgentRequest, |state, request| {
    let organization = request.principal.organization()?;
    let agent =
        facade::removal::delete_agent(&state.facade, organization, &request.agent.text()).await?;
    Ok(Json(json!({"agent": agent.to_string()})))
});

#[derive(Debug, Deserialize)]
struct ImplementationRequest {
    principal: Principal,
    implementation: Id,
}

internal!(delete_implementation, ImplementationRequest, |state, request| {
    let organization = request.principal.organization()?;
    let implementation = facade::removal::delete_implementation(
        &state.facade,
        organization,
        &request.implementation.text(),
    )
    .await?;
    Ok(Json(json!({"implementation": implementation.to_string()})))
});

#[derive(Debug, Deserialize)]
struct CollectRequest {
    principal: Principal,
    drawers: Vec<Id>,
}

internal!(collect, CollectRequest, |state, request| {
    let organization = request.principal.organization()?;
    let drawers: Vec<String> = request.drawers.iter().map(Id::text).collect();
    let drawers = backend::collect(&state.facade, organization, &drawers).await?;
    Ok(Json(json!({"drawers": drawers})))
});

#[derive(Debug, Deserialize)]
struct ProbeRequest {
    principal: Principal,
    input: ProbeInput,
}

internal!(probe, ProbeRequest, |state, request| {
    let principal = request.principal.context(&state).await?;
    let probe = probe_backend::probe(&state.facade, &principal, &request.input, "graphql").await?;
    Ok(Json(json!(probe)))
});

#[derive(Debug, Deserialize)]
struct ProbeControlRequest {
    principal: Principal,
    probe: String,
}

async fn probe_control(state: &Shared, request: &ProbeControlRequest, op: ProbeControl) -> Answer {
    let principal = request.principal.context(state).await?;
    let probe = probe_backend::control(&state.facade, &principal, &request.probe, op).await?;
    Ok(Json(json!(probe)))
}

internal!(probe_cancel, ProbeControlRequest, |state, request| {
    probe_control(&state, &request, ProbeControl::Cancel).await
});
internal!(probe_pause, ProbeControlRequest, |state, request| {
    probe_control(&state, &request, ProbeControl::Pause).await
});
internal!(probe_resume, ProbeControlRequest, |state, request| {
    probe_control(&state, &request, ProbeControl::Resume).await
});

#[derive(Debug, Deserialize)]
struct CreateHigherOrderRequest {
    principal: Principal,
    input: facade::mutations::higher_order::CreateHigherOrderInput,
}

internal!(
    create_higher_order,
    CreateHigherOrderRequest,
    |state, request| {
        let organization = request.principal.organization()?;
        let created = facade::mutations::higher_order::create_higher_order_implementation(
            &state.facade,
            organization,
            request.input,
        )
        .await?;
        Ok(Json(json!({
            "implementation": created.implementation.to_string(),
            "diagnostics": created.diagnostics,
        })))
    }
);

#[derive(Debug, Deserialize)]
struct EnsureAgentRequest {
    principal: Principal,
    #[serde(flatten)]
    input: facade::mutations::agent::EnsureAgentInput,
}

internal!(ensure_agent, EnsureAgentRequest, |state, request| {
    let (client, user, organization) = request.principal.identity()?;
    let agent =
        facade::mutations::agent::ensure(&state.facade, client, user, organization, &request.input)
            .await?;
    Ok(Json(json!({"agent": agent.to_string()})))
});

#[derive(Debug, Deserialize)]
struct ImplementAgentRequest {
    principal: Principal,
    input: rekuest_core::inputs::ImplementAgentInputModel,
}

internal!(implement_agent, ImplementAgentRequest, |state, request| {
    let (client, user, organization) = request.principal.identity()?;
    let mut input = request.input;
    input
        .validate()
        .map_err(|e| Refusal(StatusCode::BAD_REQUEST, e.0))?;
    let (agent, diagnostics) =
        facade::mutations::agent::implement(&state.facade, client, user, organization, &input)
            .await?;
    Ok(Json(
        json!({"agent": agent.to_string(), "diagnostics": diagnostics}),
    ))
});

#[derive(Debug, Deserialize)]
struct ShelveRequest {
    principal: Principal,
    identifier: String,
    resource_id: String,
    #[serde(default)]
    label: Option<String>,
    #[serde(default)]
    description: Option<String>,
}

internal!(shelve, ShelveRequest, |state, request| {
    let (client, user, organization) = request.principal.identity()?;
    let db = &state.facade.db;
    let agent = facade::registration::ensure_agent(db, client, user, organization)
        .await
        .map_err(BackendError::Database)?;
    let drawer = facade::registration::shelve(
        db,
        agent,
        &request.identifier,
        &request.resource_id,
        request.label.as_deref(),
        request.description.as_deref(),
        false,
    )
    .await
    .map_err(BackendError::Database)?;
    Ok(Json(json!({"drawer": drawer.to_string()})))
});

#[derive(Debug, Deserialize)]
struct UnshelveRequest {
    principal: Principal,
    id: String,
}

internal!(unshelve, UnshelveRequest, |state, request| {
    let (client, user, organization) = request.principal.identity()?;
    let db = &state.facade.db;
    let agent = facade::registration::ensure_agent(db, client, user, organization)
        .await
        .map_err(BackendError::Database)?;
    facade::registration::unshelve(db, agent, &request.id, true)
        .await
        .map_err(|e| match e {
            facade::registration::UnshelveError::Database(e) => {
                Refusal::from(BackendError::Database(e))
            }
            refused => Refusal(StatusCode::BAD_REQUEST, refused.to_string()),
        })?;
    Ok(Json(json!({"drawer": request.id})))
});
