//! `createHigherOrderImplementation`: a wrapper deployed onto the agent of what it wraps,
//! redeployed in place, kept when the agent re-registers, and refused when it cannot work.
//! Needs `AGENTD_TEST_DATABASE_URL` and `AGENTD_TEST_REDIS_URL`; skipped without them.

use std::sync::Arc;

use authentikate::base_models::StaticToken;
use facade::consumers::connections::Connections;
use facade::mutations::higher_order::{create_higher_order_implementation, CreateHigherOrderInput};
use facade::settings::Settings;
use facade::Context;
use serde_json::{json, Value};

async fn context() -> Option<Context> {
    let db_url = std::env::var("AGENTD_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("AGENTD_TEST_REDIS_URL").ok()?;
    let redis_client = redis::Client::open(redis_url).unwrap();
    let settings =
        authentikate::AuthentikateSettings::prepare(&json!({"audience": "rekuest"}), true).unwrap();
    Some(Context {
        db: sqlx::PgPool::connect(&db_url).await.unwrap(),
        redis: redis::aio::ConnectionManager::new(redis_client.clone())
            .await
            .unwrap(),
        redis_client: redis_client.clone(),
        settings: Arc::new(Settings::default()),
        verifier: Arc::new(authentikate::Verifier::new(settings)),
        connections: Connections::default(),
        channel_layer: kante::ChannelLayer::new(
            redis_client,
            kante::ChannelLayerConfig {
                prefix: format!("higher-order-tests-{}", uuid::Uuid::new_v4().simple()),
                ..kante::ChannelLayerConfig::default()
            },
        )
        .await
        .unwrap(),
    })
}

/// A fresh agent of `org` that declares a `run_flow` generator: (agent, its organization).
async fn runner(ctx: &Context, org: &str) -> (i64, i64) {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "higher-order-tests", "org": org,
        "client_id": format!("c-{unique}"), "client_app": format!("a-{unique}"), "client_release": "1",
    }))
    .unwrap();
    let identity = authentikate::expand::expand_token_context(
        &ctx.db,
        &spec.to_token(chrono::Utc::now(), "raw"),
    )
    .await
    .unwrap();
    let agent = facade::registration::ensure_agent(
        &ctx.db,
        identity.client,
        identity.user,
        identity.organization,
    )
    .await
    .unwrap();
    register(ctx, agent, "h1").await;
    (agent, identity.organization)
}

async fn register(ctx: &Context, agent: i64, hash: &str) {
    let payload: rekuest_core::inputs::ImplementAgentInputModel = serde_json::from_value(json!({
        "hash": hash,
        "implementations": [{
            "interface": "run_flow",
            "definition": {"key": "run_flow", "name": "Run flow", "kind": "GENERATOR", "args": [
                {"key": "flow", "kind": "STRING", "nullable": false},
                {"key": "kwargs", "kind": "DICT", "nullable": false, "children": [{"key": "...", "kind": "STRING", "nullable": false}]}
            ]},
        }, {
            "interface": "echo",
            "definition": {"key": "echo", "name": "Echo", "kind": "FUNCTION"},
        }],
    }))
    .unwrap();
    let mut tx = ctx.db.begin().await.unwrap();
    facade::registration::implement_agent(&mut tx, agent, &payload)
        .await
        .unwrap();
    tx.commit().await.unwrap();
}

async fn implementation(
    ctx: &Context,
    agent: i64,
    interface: &str,
) -> Option<(i64, Option<i64>, Value)> {
    sqlx::query_as(
        "SELECT id, higher_order_for_id, higher_order_config FROM facade_implementation WHERE agent_id = $1 AND interface = $2",
    )
    .bind(agent)
    .bind(interface)
    .fetch_optional(&ctx.db)
    .await
    .unwrap()
}

fn wrapper(lower: i64, interface: &str, kind: &str) -> CreateHigherOrderInput {
    serde_json::from_value(json!({
        "lower": lower.to_string(),
        "interface": interface,
        "definition": {"key": format!("flow_{interface}"), "name": "A flow", "kind": kind,
                       "args": [{"key": "x", "kind": "INT", "nullable": false}]},
        "config": {"bound": {"flow": "123"}, "args_key": "kwargs"},
    }))
    .unwrap()
}

#[tokio::test]
async fn a_wrapper_is_deployed_linked_redeployed_and_survives_reregistration() {
    let Some(ctx) = context().await else { return };
    let org = format!("ho-{}", uuid::Uuid::new_v4().simple());
    let (agent, organization) = runner(&ctx, &org).await;
    let (lower, ..) = implementation(&ctx, agent, "run_flow").await.unwrap();

    let created = create_higher_order_implementation(
        &ctx,
        organization,
        wrapper(lower, "flow:123", "GENERATOR"),
    )
    .await
    .unwrap();
    let (id, wraps, config) = implementation(&ctx, agent, "flow:123").await.unwrap();
    assert_eq!((id, wraps), (created.implementation, Some(lower)));
    assert_eq!(
        config,
        json!({"bound": {"flow": "123"}, "args_key": "kwargs"})
    );

    // Deployed again (the flow changed): the same row, the new config.
    let mut again = wrapper(lower, "flow:123", "GENERATOR");
    again.config = Some(json!({"bound": {"flow": "124"}, "args_key": "kwargs"}));
    let redeployed = create_higher_order_implementation(&ctx, organization, again)
        .await
        .unwrap();
    assert_eq!(redeployed.implementation, id);
    assert_eq!(
        implementation(&ctx, agent, "flow:123").await.unwrap().2["bound"]["flow"],
        "124"
    );

    // The agent restarts with a new hash and declares only run_flow: the wrapper stays.
    register(&ctx, agent, "h2").await;
    assert_eq!(implementation(&ctx, agent, "flow:123").await.unwrap().0, id);
}

#[tokio::test]
async fn wrappers_that_cannot_work_are_refused() {
    let Some(ctx) = context().await else { return };
    let (agent, organization) =
        runner(&ctx, &format!("ho-{}", uuid::Uuid::new_v4().simple())).await;
    let (lower, ..) = implementation(&ctx, agent, "run_flow").await.unwrap();
    let refused = |result: Result<_, facade::mutations::Refusal>| result.unwrap_err().to_string();

    let (_, stranger_org) = runner(&ctx, &format!("ho-{}", uuid::Uuid::new_v4().simple())).await;
    assert_eq!(
        refused(
            create_higher_order_implementation(
                &ctx,
                stranger_org,
                wrapper(lower, "flow:1", "GENERATOR")
            )
            .await
        ),
        "Implementation matching query does not exist."
    );
    assert_eq!(
        refused(
            create_higher_order_implementation(
                &ctx,
                organization,
                wrapper(lower, "run_flow", "GENERATOR")
            )
            .await
        ),
        "An implementation cannot wrap itself"
    );
    assert!(refused(
        create_higher_order_implementation(
            &ctx,
            organization,
            wrapper(lower, "flow:2", "FUNCTION")
        )
        .await
    )
    .starts_with("Higher-order kind mismatch"));

    let first = create_higher_order_implementation(
        &ctx,
        organization,
        wrapper(lower, "flow:3", "GENERATOR"),
    )
    .await
    .unwrap();
    assert!(refused(
        create_higher_order_implementation(
            &ctx,
            organization,
            wrapper(first.implementation, "flow:4", "GENERATOR")
        )
        .await
    )
    .starts_with("Nested higher-order"));

    // An interface the agent itself declares is not a wrapper's to take.
    assert_eq!(
        refused(create_higher_order_implementation(&ctx, organization, wrapper(lower, "echo", "GENERATOR")).await),
        format!("Interface echo is already implemented on this agent by something other than a wrapper of implementation {lower}")
    );
}

/// The locks an implementation declares become its required locks (the agent's, by key; a key
/// the agent does not declare is skipped), and Lock/Unlock set and clear who holds one.
#[tokio::test]
async fn required_locks_are_the_agents_and_their_holder_is_tracked() {
    let Some(ctx) = context().await else { return };
    let (agent, _) = runner(&ctx, &format!("locks-{}", uuid::Uuid::new_v4().simple())).await;
    let payload: rekuest_core::inputs::ImplementAgentInputModel = serde_json::from_value(json!({
        "hash": "locks",
        "locks": [{"key": "stage", "definition": {"key": "stage", "description": "the stage"}}],
        "implementations": [{
            "interface": "move",
            "locks": ["stage", "undeclared"],
            "definition": {"key": "move", "name": "Move", "kind": "FUNCTION"},
        }],
    }))
    .unwrap();
    let mut tx = ctx.db.begin().await.unwrap();
    facade::registration::implement_agent(&mut tx, agent, &payload)
        .await
        .unwrap();
    tx.commit().await.unwrap();

    let required: Vec<String> = sqlx::query_scalar(
        "SELECT l.key FROM facade_implementation_required_locks r
           JOIN facade_lock l ON l.id = r.lock_id JOIN facade_implementation i ON i.id = r.implementation_id
          WHERE i.agent_id = $1 AND i.interface = 'move'",
    )
    .bind(agent)
    .fetch_all(&ctx.db)
    .await
    .unwrap();
    assert_eq!(required, vec!["stage"]);

    let (implementation, action): (i64, i64) =
        sqlx::query_as("SELECT id, action_id FROM facade_implementation WHERE agent_id = $1 AND interface = 'move'")
            .bind(agent)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    let task: i64 = sqlx::query_scalar(
        "INSERT INTO facade_task (acted_on, ephemeral, hooks, reference, resumes, capture, is_higher_order_child,
                                  latest_event_kind, latest_instruct_kind, statusmessage, is_done, created_at,
                                  updated_at, revision, step, dispatch_attempts, trigger_depth, action_id, agent_id,
                                  implementation_id)
         VALUES ('{}', false, '[]', $4, 0, false, false, 'STARTED', 'ASSIGN', '', false, now(), now(), 0, false, 1, 0, $2, $1, $3)
         RETURNING id",
    )
    .bind(agent)
    .bind(action)
    .bind(implementation)
    .bind(uuid::Uuid::new_v4().to_string())
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    let holder = || async {
        sqlx::query_scalar::<_, Option<i64>>(
            "SELECT hold_by_id FROM facade_lock WHERE agent_id = $1 AND key = 'stage'",
        )
        .bind(agent)
        .fetch_one(&ctx.db)
        .await
        .unwrap()
    };
    facade::persist::state::on_agent_lock(&ctx, agent, "stage", &task.to_string())
        .await
        .unwrap();
    assert_eq!(holder().await, Some(task));
    facade::persist::state::on_agent_unlock(&ctx, agent, "stage")
        .await
        .unwrap();
    assert_eq!(holder().await, None);
}
