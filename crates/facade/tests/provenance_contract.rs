//! The provenance token, against the Python mint (`facade/provenance/mint.py`).
//!
//! agentd assigns a root task and a child through the real backend and takes the tokens off the
//! agent's queue. The Python mint is handed the same inputs (the rows agentd read, as the stubs
//! its duck-typed `task` accepts) and mints its own. Python verifies agentd's signature with
//! joserfc, and both tokens must decode to the same header and the same claims but for `iat`,
//! `exp` and `jti`, `ahs` included, over args that exercise the canonical encoding.
//!
//! Needs `AGENTD_TEST_DATABASE_URL`, `AGENTD_TEST_REDIS_URL`, the Python server's source
//! (`AGENTD_TEST_REKUEST_SOURCE`, default the lab mount) and a Python with its dependencies
//! (`AGENTD_TEST_PYTHON`, default the workflows worktree's venv). Nothing is written to the source.

use std::io::Write;
use std::process::{Command, Stdio};
use std::sync::Arc;

use authentikate::base_models::StaticToken;
use facade::backend::{self, AssignInput, AssignOrigin};
use facade::caller_context::CallerContext;
use facade::consumers::connections::Connections;
use facade::provenance::keys::InstanceKey;
use facade::settings::Settings;
use facade::Context;
use redis::AsyncCommands;
use serde_json::{json, Map, Value};

const PEM: &str = "-----BEGIN PRIVATE KEY-----\n\
    MC4CAQAwBQYDK2VwBCIEILK+rl9gVEjfKGiye+mLLjfEUGIdoP0WPC8lMZS3NYK2\n\
    -----END PRIVATE KEY-----\n";

const PYTHON_MINT: &str = r#"
import json, sys, types
sys.dont_write_bytecode = True
data = json.load(sys.stdin)
sys.path.insert(0, data["source"])
from django.conf import settings
from joserfc import jwt
from joserfc.jwk import OKPKey

key = OKPKey.import_key(data["pem"])
settings.configure(PROVENANCE={
    "ISSUER": data["issuer"], "KID": key.thumbprint(), "PRIVATE_KEY": data["pem"], "PUBLIC_KEY": None,
    "TOKEN_TTL_SECONDS": data["ttl"], "HUMAN_ROLES": [], "STRICT": False,
})
from facade.caller_context import CallerContext
from facade.provenance import mint

NS = types.SimpleNamespace
stubs = {}
for t in data["tasks"]:
    stubs[t["pk"]] = NS(
        pk=t["pk"], parent_id=t["parent_id"], args=t["args"],
        implementation=NS(needs_token=t["needs_token"], provenance_audience=t["audience"]),
        agent=NS(user=NS(sub=t["agent_sub"]), client=NS(client_id=t["agent_client_id"])),
        caller=NS(user_id=1, user=NS(sub=t["caller_sub"]), organization_id=1) if t["caller_sub"] else None,
    )
for stub in stubs.values():
    stub.parent = stubs.get(stub.parent_id)
ctx = CallerContext(user=NS(sub=data["sub"]), client=None, organization=None, roles=[])

def decoded(token):
    token = jwt.decode(token, key, algorithms=["Ed25519"])
    return {"header": token.header, "claims": token.claims}

out = []
for t in data["tasks"]:
    out.append({"python": decoded(mint.mint_token_for_task(stubs[t["pk"]], ctx)), "rust": decoded(t["token"])})
print(json.dumps(out))
"#;

async fn context() -> Option<Context> {
    let db_url = std::env::var("AGENTD_TEST_DATABASE_URL").ok()?;
    let redis_url = std::env::var("AGENTD_TEST_REDIS_URL").ok()?;
    let db = sqlx::PgPool::connect(&db_url).await.unwrap();
    let redis_client = redis::Client::open(redis_url).unwrap();
    let redis = redis::aio::ConnectionManager::new(redis_client.clone())
        .await
        .unwrap();
    let auth =
        authentikate::AuthentikateSettings::prepare(&json!({"audience": "rekuest"}), true).unwrap();
    let channel_layer =
        kante::ChannelLayer::new(redis_client.clone(), kante::ChannelLayerConfig::default())
            .await
            .unwrap();
    Some(Context {
        db,
        redis,
        redis_client,
        settings: Arc::new(Settings {
            instance_key: Some(Arc::new(InstanceKey::from_pem(PEM).unwrap())),
            ..Settings::default()
        }),
        verifier: Arc::new(authentikate::Verifier::new(auth)),
        connections: Connections::default(),
        channel_layer,
    })
}

fn python() -> Option<(String, String)> {
    let python = std::env::var("AGENTD_TEST_PYTHON").unwrap_or_else(|_| {
        "/home/jhnnsrs/Code/worktrees/rekuest-server-workflows/.venv/bin/python".into()
    });
    let source = std::env::var("AGENTD_TEST_REKUEST_SOURCE")
        .unwrap_or_else(|_| "/home/jhnnsrs/Code/deployments/next/mounts/rekuest".into());
    (std::path::Path::new(&python).exists() && std::path::Path::new(&source).exists())
        .then_some((python, source))
}

#[tokio::test]
async fn the_rust_token_is_the_python_token() {
    let (Some(ctx), Some((python, source))) = (context().await, python()) else {
        return;
    };
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "contract", "org": format!("o-{unique}"),
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
    sqlx::query("UPDATE facade_agent SET connected = true, last_seen = now() WHERE id = $1")
        .bind(agent)
        .execute(&ctx.db)
        .await
        .unwrap();
    // No ports: any args go, so they can exercise the canonical form.
    let implementation: i64 = sqlx::query_scalar(
        "WITH a AS (
             INSERT INTO facade_action (defined_at, embedding_model, key, version, pure, idempotent, allow_probe,
                                        stateful, kind, port_groups, name, description, scope, is_dev, hash, args,
                                        returns, arg_count, return_count, app_id, organization_id)
             SELECT now(), '', 'anything', '1', false, false, false, false, 'FUNCTION', '[]', 'Anything', '',
                    'GLOBAL', false, $2, '[]', '[]', 0, 0, app_id, organization_id FROM facade_agent WHERE id = $1
             RETURNING id)
         INSERT INTO facade_implementation (interface, name, policy, higher_order_config, params, created_at,
                                            updated_at, tracks, diagnostics, needs_token, provenance_audience,
                                            effects, execution, action_id, agent_id, release_id)
         SELECT 'anything', 'anything', '{}', '{}', '{}', now(), now(), '[]', '[]', true, '[\"mikro\", \"kabinet\"]',
                'UNKNOWN', 'PLAIN', a.id, ag.id, ag.release_id FROM a, facade_agent ag WHERE ag.id = $1
         RETURNING id",
    )
    .bind(agent)
    .bind(format!("hash-{unique}"))
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    let principal = CallerContext::from_agent(&ctx.db, agent, vec![])
        .await
        .unwrap();

    let args = |text: &str| serde_json::from_str::<Map<String, Value>>(text).unwrap();
    let root_args =
        args(r#"{"b": [1, 2.5, null, 1e-7, 1e22], "a": "é\n \u007f\"", "n": {"z": 1, "y": "x"}}"#);
    let child_args = args(r#"{"emoji": "🧪", "neg": -0.0, "big": 12345678901234567, "t": true}"#);
    let root = backend::assign_with_status(
        &ctx,
        &principal,
        &AssignInput {
            implementation: Some(implementation.to_string()),
            args: root_args.clone(),
            ..AssignInput::default()
        },
        AssignOrigin::default(),
    )
    .await
    .unwrap()
    .task;
    let child = backend::assign_with_status(
        &ctx,
        &principal,
        &AssignInput {
            implementation: Some(implementation.to_string()),
            args: child_args.clone(),
            parent: Some(root.to_string()),
            ..AssignInput::default()
        },
        AssignOrigin::default(),
    )
    .await
    .unwrap()
    .task;

    let mut redis = ctx.redis.clone();
    let frames: Vec<String> = redis
        .lrange(format!("rekuest:agent:{agent}:queue"), 0, -1)
        .await
        .unwrap();
    let token_of = |task: i64| {
        frames
            .iter()
            .map(|f| serde_json::from_str::<Value>(f).unwrap())
            .find(|f| f["task"].as_str() == Some(task.to_string().as_str()))
            .and_then(|f| f["token"].as_str().map(str::to_owned))
            .expect("the task's ASSIGN carries a token")
    };
    let (agent_sub, agent_client_id): (String, String) = sqlx::query_as(
        "SELECT u.sub, c.client_id FROM facade_agent a JOIN authentikate_user u ON u.id = a.user_id
           JOIN authentikate_client c ON c.id = a.client_id WHERE a.id = $1",
    )
    .bind(agent)
    .fetch_one(&ctx.db)
    .await
    .unwrap();
    let stub = |pk: i64, parent: Option<i64>, args: &Map<String, Value>| {
        json!({
            "pk": pk, "parent_id": parent, "args": args, "needs_token": true,
            "audience": ["mikro", "kabinet"], "agent_sub": agent_sub, "agent_client_id": agent_client_id,
            "caller_sub": principal.user_sub, "token": token_of(pk),
        })
    };
    let input = json!({
        "source": source, "pem": PEM, "issuer": ctx.settings.provenance.issuer,
        "ttl": ctx.settings.provenance.token_ttl.as_secs(), "sub": principal.user_sub,
        "tasks": [stub(root, None, &root_args), stub(child, Some(root), &child_args)],
    });

    let mut process = Command::new(&python)
        .args(["-B", "-c", PYTHON_MINT])
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("python runs");
    process
        .stdin
        .take()
        .unwrap()
        .write_all(input.to_string().as_bytes())
        .unwrap();
    let output = process.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let minted: Vec<Value> = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(minted.len(), 2);

    for (tokens, (task, parent)) in minted.iter().zip([(root, None), (child, Some(root))]) {
        let (python, rust) = (&tokens["python"], &tokens["rust"]);
        assert_eq!(python["header"], rust["header"]);
        let strip = |claims: &Value| {
            let mut claims = claims.as_object().unwrap().clone();
            let window = claims["exp"].as_i64().unwrap() - claims["iat"].as_i64().unwrap();
            for volatile in ["iat", "exp", "jti"] {
                claims.remove(volatile);
            }
            (Value::Object(claims), window)
        };
        let (python_claims, python_window) = strip(&python["claims"]);
        let (rust_claims, rust_window) = strip(&rust["claims"]);
        assert_eq!(python_claims, rust_claims, "task {task}");
        assert_eq!(python_window, rust_window);
        assert_eq!(rust_claims["tsk"], task.to_string());
        assert_eq!(rust_claims["rtk"], root.to_string());
        assert_eq!(rust_claims["ptk"], json!(parent.map(|p| p.to_string())));
        assert_eq!(rust_claims["rcb"], json!(principal.user_sub));
    }
}
