//! `ensureAgent` through agentd: a new agent's name, the transport fields (a present null
//! clears), drawers forgotten on request, and an agent turning WEBHOOK abandoning its socket
//! queue. Needs `AGENTD_TEST_DATABASE_URL` and `AGENTD_TEST_REDIS_URL`; skipped without them.

use std::sync::Arc;

use authentikate::base_models::StaticToken;
use facade::consumers::agent_queue::queue_key;
use facade::consumers::connections::Connections;
use facade::mutations::agent::{ensure, EnsureAgentInput};
use facade::settings::Settings;
use facade::Context;
use serde_json::json;

async fn context() -> Option<Context> {
    let db_url = std::env::var("AGENTD_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("AGENTD_TEST_REDIS_URL").ok()?;
    let redis_client = redis::Client::open(redis_url).unwrap();
    let auth =
        authentikate::AuthentikateSettings::prepare(&json!({"audience": "rekuest"}), true).unwrap();
    Some(Context {
        db: sqlx::PgPool::connect(&db_url).await.unwrap(),
        redis: redis::aio::ConnectionManager::new(redis_client.clone())
            .await
            .unwrap(),
        redis_client: redis_client.clone(),
        settings: Arc::new(Settings {
            redis_key_prefix: format!("agent-tests-{}", uuid::Uuid::new_v4().simple()),
            ..Settings::default()
        }),
        verifier: Arc::new(authentikate::Verifier::new(auth)),
        connections: Connections::default(),
        channel_layer: kante::ChannelLayer::new(redis_client, kante::ChannelLayerConfig::default())
            .await
            .unwrap(),
    })
}

/// A fresh identity: (client, user, organization).
async fn identity(ctx: &Context) -> (i64, i64, i64) {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "agent-tests", "org": format!("o-{unique}"),
        "client_id": format!("c-{unique}"), "client_app": format!("a-{unique}"), "client_release": "1",
    }))
    .unwrap();
    let id = authentikate::expand::expand_token_context(
        &ctx.db,
        &spec.to_token(chrono::Utc::now(), "raw"),
    )
    .await
    .unwrap();
    (id.client, id.user, id.organization)
}

fn input(value: serde_json::Value) -> EnsureAgentInput {
    serde_json::from_value(value).unwrap()
}

#[tokio::test]
async fn an_agent_is_ensured_configured_and_turned_into_a_hook() {
    let Some(ctx) = context().await else { return };
    let (client, user, org) = identity(&ctx).await;

    let agent = ensure(
        &ctx,
        client,
        user,
        org,
        &input(json!({"name": "first", "hook_url_secret": "s3cret"})),
    )
    .await
    .unwrap();
    let again = ensure(&ctx, client, user, org, &input(json!({"name": "renamed"})))
        .await
        .unwrap();
    assert_eq!(agent, again);
    let (name, secret): (String, Option<String>) =
        sqlx::query_as("SELECT name, hook_url_secret FROM facade_agent WHERE id = $1")
            .bind(agent)
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(
        (name.as_str(), secret.as_deref()),
        ("first", Some("s3cret")),
        "the name only names a new agent"
    );

    // Work queued for its socket, one task not picked up yet.
    let mut redis = ctx.redis.clone();
    let _: i64 = redis::cmd("RPUSH")
        .arg(queue_key(&ctx.settings, agent))
        .arg("{}")
        .query_async(&mut redis)
        .await
        .unwrap();
    facade::registration::shelve(&ctx.db, agent, "@x/y", "r1", None, None, false)
        .await
        .unwrap();

    ensure(
        &ctx,
        client,
        user,
        org,
        &input(json!({"kind": "WEBHOOK", "hook_url": "http://hook", "hook_url_secret": null, "description": "a hook", "clear_drawers": true})),
    )
    .await
    .unwrap();
    let (kind, url, secret, description): (String, Option<String>, Option<String>, Option<String>) =
        sqlx::query_as(
            "SELECT kind, hook_url, hook_url_secret, description FROM facade_agent WHERE id = $1",
        )
        .bind(agent)
        .fetch_one(&ctx.db)
        .await
        .unwrap();
    assert_eq!(
        (
            kind.as_str(),
            url.as_deref(),
            secret,
            description.as_deref()
        ),
        ("WEBHOOK", Some("http://hook"), None, Some("a hook"))
    );
    let queued: i64 = redis::cmd("LLEN")
        .arg(queue_key(&ctx.settings, agent))
        .query_async(&mut redis)
        .await
        .unwrap();
    assert_eq!(queued, 0, "no socket will drain it any more");
    let drawers: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM facade_memorydrawer d JOIN facade_memoryshelve s ON s.id = d.shelve_id WHERE s.agent_id = $1",
    )
    .bind(agent)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    assert_eq!(drawers, 0);

    assert!(ensure(
        &ctx,
        client,
        user,
        org,
        &input(json!({"kind": "CARRIER_PIGEON"}))
    )
    .await
    .is_err());
}
