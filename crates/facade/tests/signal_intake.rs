//! `POST /agi/signal/{service}`: a hub service's signal is stored once, a provenance token of
//! this rekuest makes it caused by its task (only within the object's organization), and a
//! request not signed by that service is refused. Needs `AGENTD_TEST_DATABASE_URL` and
//! `AGENTD_TEST_REDIS_URL`; skipped without them.

use std::sync::Arc;

use authentikate::base_models::StaticToken;
use axum::http::HeaderMap;
use facade::consumers::connections::Connections;
use facade::provenance::keys::InstanceKey;
use facade::service_trust::{self, TrustBundle};
use facade::settings::{ServiceAgent, Settings};
use facade::signal_intake::signal_intake;
use facade::Context;
use serde_json::{json, Value};

const PEM: &str = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEILK+rl9gVEjfKGiye+mLLjfEUGIdoP0WPC8lMZS3NYK2\n-----END PRIVATE KEY-----\n";
const PATH: &str = "/agi/signal/bank";

async fn context() -> Option<(Context, Arc<InstanceKey>)> {
    let db_url = std::env::var("AGENTD_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("AGENTD_TEST_REDIS_URL").ok()?;
    let key = Arc::new(InstanceKey::from_pem(PEM).unwrap());
    // The test signs as the bank with the same key; the bundle lists it under the bank.
    let mut jwk = key.public_jwk();
    jwk["service"] = json!("live.arkitekt.bank");
    let settings = Settings {
        instance_key: Some(key.clone()),
        service_agents: vec![ServiceAgent {
            service: "bank".into(),
            identifier: None,
        }],
        trust_bundle: Arc::new(TrustBundle::new(None, Some(&json!({"keys": [jwk]})))),
        redis_key_prefix: format!("signal-tests-{}", uuid::Uuid::new_v4().simple()),
        ..Settings::default()
    };
    let redis_client = redis::Client::open(redis_url).unwrap();
    let auth =
        authentikate::AuthentikateSettings::prepare(&json!({"audience": "rekuest"}), true).unwrap();
    let ctx = Context {
        db: sqlx::PgPool::connect(&db_url).await.unwrap(),
        redis: redis::aio::ConnectionManager::new(redis_client.clone())
            .await
            .unwrap(),
        redis_client: redis_client.clone(),
        settings: Arc::new(settings),
        verifier: Arc::new(authentikate::Verifier::new(auth)),
        connections: Connections::default(),
        channel_layer: kante::ChannelLayer::new(redis_client, kante::ChannelLayerConfig::default())
            .await
            .unwrap(),
    };
    Some((ctx, key))
}

/// An organization (by slug) with an agent and one task in it: the task's id.
async fn task_in(ctx: &Context, org: &str) -> i64 {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "signal-tests", "org": org,
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
    let action: i64 = sqlx::query_scalar(
        "INSERT INTO facade_action (defined_at, embedding_model, key, version, pure, idempotent, allow_probe, stateful,
                                    kind, port_groups, name, description, scope, is_dev, hash, args, returns,
                                    arg_count, return_count, app_id, organization_id)
         SELECT now(), '', $2, '1', false, false, false, false, 'FUNCTION', '[]', 'act', '', 'GLOBAL', false, $2,
                '[]', '[]', 0, 0, a.app_id, a.organization_id FROM facade_agent a WHERE a.id = $1 RETURNING id",
    )
    .bind(agent)
    .bind(format!("act-{unique}"))
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    sqlx::query_scalar(
        "INSERT INTO facade_task (acted_on, ephemeral, hooks, reference, resumes, capture, is_higher_order_child,
                                  latest_event_kind, latest_instruct_kind, statusmessage, is_done, created_at,
                                  updated_at, revision, step, dispatch_attempts, trigger_depth, action_id, agent_id)
         VALUES ('{}', false, '[]', $3, 0, false, false, 'QUEUED', 'ASSIGN', '', false, now(), now(), 0, false, 0, 0, $2, $1)
         RETURNING id",
    )
    .bind(agent)
    .bind(action)
    .bind(unique)
    .fetch_one(&ctx.db)
    .await
    .unwrap()
}

fn provenance(key: &InstanceKey, task: i64) -> String {
    let now = chrono::Utc::now().timestamp();
    key.sign_jwt(
        &json!({"alg": "Ed25519", "kid": key.kid(), "typ": "JWT"}),
        &json!({"iss": "rekuest", "iat": now, "exp": now + 60, "tsk": task.to_string()}),
    )
}

/// POST `body` signed as `issuer`.
async fn post(
    ctx: &Context,
    key: &InstanceKey,
    service: &str,
    issuer: &str,
    body: &Value,
) -> (u16, Value) {
    let raw = serde_json::to_vec(body).unwrap();
    let mut headers = HeaderMap::new();
    let authorization = service_trust::sign(
        key,
        "POST",
        &format!("/agi/signal/{service}"),
        &raw,
        issuer,
        "live.arkitekt.rekuest",
    );
    headers.insert("Authorization", authorization.parse().unwrap());
    signal_intake(
        ctx,
        service,
        &format!("/agi/signal/{service}"),
        &headers,
        &raw,
    )
    .await
}

fn signal(org: &str, id: &str, provenance: Option<String>) -> Value {
    json!({"id": id, "kind": "CREATED", "identifier": "@bank/account", "object": "17", "organization": org,
           "descriptors": {"currency": "EUR"}, "provenance": provenance})
}

#[tokio::test]
async fn a_signal_is_stored_once_and_caused_only_within_its_organization() {
    let Some((ctx, key)) = context().await else {
        return;
    };
    let org = format!("sig-{}", uuid::Uuid::new_v4().simple());
    let task = task_in(&ctx, &org).await;
    let bank = "live.arkitekt.bank";

    let (status, first) = post(
        &ctx,
        &key,
        "bank",
        bank,
        &signal(&org, "s1", Some(provenance(&key, task))),
    )
    .await;
    assert_eq!(status, 202, "{first}");
    assert_eq!(
        (first["created"].clone(), first["caused_by"].clone()),
        (json!(true), json!(task.to_string()))
    );
    let (descriptors, kind): (Value, String) =
        sqlx::query_as("SELECT descriptors, kind FROM facade_signal WHERE id = $1")
            .bind(first["signal"].as_str().unwrap().parse::<i64>().unwrap())
            .fetch_one(&ctx.db)
            .await
            .unwrap();
    assert_eq!(
        (descriptors, kind.as_str()),
        (json!({"currency": "EUR"}), "CREATED")
    );

    // The same signal, freshly signed: not stored twice.
    let (_, again) = post(&ctx, &key, "bank", bank, &signal(&org, "s1", None)).await;
    assert_eq!(
        (again["created"].clone(), again["signal"].clone()),
        (json!(false), first["signal"].clone())
    );

    // A task of another organization gives no cause.
    let foreign = task_in(&ctx, &format!("sig-{}", uuid::Uuid::new_v4().simple())).await;
    let (_, stranger) = post(
        &ctx,
        &key,
        "bank",
        bank,
        &signal(&org, "s2", Some(provenance(&key, foreign))),
    )
    .await;
    assert_eq!(stranger["caused_by"], Value::Null);
    // Nor does a token that is not ours.
    let (_, forged) = post(
        &ctx,
        &key,
        "bank",
        bank,
        &signal(&org, "s3", Some("not.a.token".into())),
    )
    .await;
    assert_eq!(
        (forged["created"].clone(), forged["caused_by"].clone()),
        (json!(true), Value::Null)
    );
}

#[tokio::test]
async fn what_is_not_the_services_is_refused() {
    let Some((ctx, key)) = context().await else {
        return;
    };
    let org = format!("sig-{}", uuid::Uuid::new_v4().simple());

    assert_eq!(
        post(
            &ctx,
            &key,
            "bank",
            "live.arkitekt.mikro",
            &signal(&org, "x", None)
        )
        .await
        .0,
        401,
        "signed as another service"
    );
    assert_eq!(
        post(
            &ctx,
            &key,
            "nope",
            "live.arkitekt.bank",
            &signal(&org, "x", None)
        )
        .await
        .0,
        404
    );
    let (status, dropped) = post(
        &ctx,
        &key,
        "bank",
        "live.arkitekt.bank",
        &signal(&format!("{org}-unknown"), "x", None),
    )
    .await;
    assert_eq!(
        (status, dropped),
        (202, json!({"dropped": "unknown organization"}))
    );
    let mut bad = signal(&org, "y", None);
    bad["kind"] = json!("EXPLODED");
    assert_eq!(
        post(&ctx, &key, "bank", "live.arkitekt.bank", &bad).await.0,
        400
    );

    // A replayed request (the same token) is acknowledged as a duplicate, not stored again.
    let raw = serde_json::to_vec(&signal(&org, "z", None)).unwrap();
    let mut headers = HeaderMap::new();
    headers.insert(
        "Authorization",
        service_trust::sign(
            &key,
            "POST",
            PATH,
            &raw,
            "live.arkitekt.bank",
            "live.arkitekt.rekuest",
        )
        .parse()
        .unwrap(),
    );
    assert_eq!(
        signal_intake(&ctx, "bank", PATH, &headers, &raw).await.0,
        202
    );
    assert_eq!(
        signal_intake(&ctx, "bank", PATH, &headers, &raw).await,
        (202, json!({"duplicate": true}))
    );
}
