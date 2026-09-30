//! Registration, against what the Python server's own `implement_agent` wrote for the same
//! declarations.
//!
//! A fresh agent is registered step by step through a sequence of declarations: the apps of
//! `rekuest-server-core`'s fixture, each replacing the last (every reap path), then synthetic
//! steps for bloks, locks, states, descriptors, catalogs and an interface moving to another
//! action. After every step its rows, normalized, and whether it was accepted or refused must be
//! what the Python server produced: `fixtures/registration_parity.json`, recorded from it before
//! its registration was deleted (`PARITY_RECORD=1` records again, against a Python server that
//! still has `facade.registration` running as the test stack's container).
//!
//! Where agentd deliberately writes more than Python did (a dependency's `optional` and
//! `description`), those columns are masked. Needs `AGENTD_TEST_DATABASE_URL`; skipped without.

use authentikate::base_models::StaticToken;
use serde_json::{json, Value};
use sqlx::PgPool;

const STACK_CONTAINER: &str = "agentd-testdb-rekuest-1";

const IMPLEMENT: &str = r#"
import json, sys
from facade import registration
from facade.mutations.agent import ImplementAgentInputModel
from facade.persist.registration import AgentRegistrationMixin
request = json.loads(sys.stdin.read())
agent = AgentRegistrationMixin._agent_with_identity_sync(request["agent"])
try:
    agent, diagnostics = registration.implement_agent(agent.client, agent.user, agent.organization, ImplementAgentInputModel(**request["payload"]))
    out = {"ok": True, "diagnostics": [d.model_dump(mode="json") for d in diagnostics]}
except Exception as e:
    out = {"ok": False, "error": str(e), "type": type(e).__name__}
print("RESULT " + json.dumps(out))
"#;

/// What a registration answered: the diagnostics, or the refusal's first line.
#[derive(Debug, PartialEq)]
enum Outcome {
    Accepted(Value),
    Refused(String),
}

impl Outcome {
    fn to_json(&self) -> Value {
        match self {
            Outcome::Accepted(diagnostics) => json!({"accepted": diagnostics}),
            Outcome::Refused(message) => json!({"refused": message}),
        }
    }

    fn from_json(value: &Value) -> Self {
        match value.get("refused").and_then(Value::as_str) {
            Some(message) => Outcome::Refused(message.to_owned()),
            None => Outcome::Accepted(value["accepted"].clone()),
        }
    }
}

/// Drop the columns agentd writes and the Python server never did (a dependency's `optional`
/// and `description`), from every implementation's dependencies.
fn mask(mut rows: Value) -> Value {
    for implementation in rows["implementations"].as_array_mut().into_iter().flatten() {
        for dependency in implementation["dependencies"]
            .as_array_mut()
            .into_iter()
            .flatten()
        {
            if let Some(dependency) = dependency.as_object_mut() {
                dependency.remove("optional");
                dependency.remove("description");
            }
        }
    }
    rows
}

async fn python_implement(agent: i64, payload: &Value) -> Outcome {
    use tokio::io::AsyncWriteExt;
    let mut child = tokio::process::Command::new("docker")
        .args([
            "exec",
            "-i",
            STACK_CONTAINER,
            "python",
            "manage.py",
            "shell",
            "-c",
            IMPLEMENT,
        ])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("docker runs");
    let request = json!({"agent": agent, "payload": payload}).to_string();
    let mut stdin = child.stdin.take().unwrap();
    stdin.write_all(request.as_bytes()).await.unwrap();
    drop(stdin);
    let output = child.wait_with_output().await.unwrap();
    let stdout = String::from_utf8_lossy(&output.stdout);
    let Some(line) = stdout.lines().find_map(|line| line.strip_prefix("RESULT ")) else {
        panic!(
            "python printed no result:\n{stdout}\n{}",
            String::from_utf8_lossy(&output.stderr)
        );
    };
    let result: Value = serde_json::from_str(line).unwrap();
    if result["ok"] == json!(true) {
        Outcome::Accepted(result["diagnostics"].clone())
    } else {
        Outcome::Refused(first_line(result["error"].as_str().unwrap()))
    }
}

fn first_line(message: &str) -> String {
    message.lines().next().unwrap_or_default().to_owned()
}

async fn rust_implement(db: &PgPool, agent: i64, payload: &Value) -> Outcome {
    let mut model: rekuest_core::inputs::ImplementAgentInputModel =
        match serde_json::from_value(payload.clone()) {
            Ok(model) => model,
            Err(e) => return Outcome::Refused(format!("shape: {e}")),
        };
    if let Err(e) = model.validate() {
        return Outcome::Refused(first_line(&e.0));
    }
    let mut tx = db.begin().await.unwrap();
    match facade::registration::implement_agent(&mut tx, agent, &model).await {
        Ok(implemented) => {
            tx.commit().await.unwrap();
            Outcome::Accepted(serde_json::to_value(&implemented.diagnostics).unwrap())
        }
        Err(e) => Outcome::Refused(first_line(&e.to_string())),
    }
}

/// An agent of `org` for a fresh client and app, through the real expansion.
async fn agent(db: &PgPool, org: &str, side: &str) -> i64 {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{side}-{unique}"), "iss": "registration-parity", "org": org,
        "client_id": format!("c-{side}-{unique}"), "client_app": format!("a-{side}-{unique}"), "client_release": "1",
    }))
    .unwrap();
    let token = spec.to_token(chrono::Utc::now(), "raw");
    let identity = authentikate::expand::expand_token_context(db, &token)
        .await
        .unwrap();
    facade::registration::ensure_agent(db, identity.client, identity.user, identity.organization)
        .await
        .unwrap()
}

/// Everything a registration writes for `agent`, without ids, timestamps or the identity that
/// differs between the two agents by construction (app, client, user).
const SNAPSHOT: &str = r#"
WITH ag AS (SELECT * FROM facade_agent WHERE id = $1),
ports AS (
    SELECT 'arg' AS side, p.action_id, p.key_path, p.index, p.key, p.kind, p.identifier, p.dimension,
           p.compiled_jsonpath, p.nullable, parent.key_path AS parent
      FROM facade_argport p LEFT JOIN facade_argport parent ON parent.id = p.parent_id
    UNION ALL
    SELECT 'return', p.action_id, p.key_path, p.index, p.key, p.kind, p.identifier, p.dimension,
           p.compiled_jsonpath, p.nullable, parent.key_path
      FROM facade_returnport p LEFT JOIN facade_returnport parent ON parent.id = p.parent_id
)
SELECT jsonb_build_object(
  'agent', (SELECT jsonb_build_object('hash', hash, 'description', description,
                                      'name_is_client', name = (SELECT client_id FROM authentikate_client WHERE id = ag.client_id),
                                      'name', name) FROM ag),
  'implementations', coalesce((SELECT jsonb_agg(jsonb_build_object(
      'interface', i.interface, 'higher_order_config', i.higher_order_config,
      'higher_order', i.higher_order_for_id IS NOT NULL, 'params', i.params, 'tracks', i.tracks,
      'diagnostics', i.diagnostics, 'needs_token', i.needs_token, 'provenance_audience', i.provenance_audience,
      'effects', i.effects, 'execution', i.execution, 'code_hash', i.code_hash,
      'action', (SELECT jsonb_build_object(
          'key', a.key, 'version', a.version, 'hash', a.hash, 'name', a.name, 'description', a.description,
          'scope', a.scope, 'pure', a.pure, 'idempotent', a.idempotent, 'allow_probe', a.allow_probe,
          'stateful', a.stateful, 'is_dev', a.is_dev, 'kind', a.kind, 'port_groups', a.port_groups,
          'args', a.args, 'returns', a.returns, 'arg_count', a.arg_count, 'return_count', a.return_count,
          'same_org', a.organization_id = (SELECT organization_id FROM ag),
          'same_app', a.app_id = (SELECT app_id FROM ag),
          'protocols', (SELECT coalesce(jsonb_agg(pr.name ORDER BY pr.name), '[]') FROM facade_action_protocols ap
                          JOIN facade_protocol pr ON pr.id = ap.protocol_id WHERE ap.action_id = a.id),
          'collections', (SELECT coalesce(jsonb_agg(c.name ORDER BY c.name), '[]') FROM facade_action_collections ac
                            JOIN facade_collection c ON c.id = ac.collection_id WHERE ac.action_id = a.id),
          'is_test_for', (SELECT coalesce(jsonb_agg(t.key || '@' || t.version ORDER BY t.key, t.version), '[]')
                            FROM facade_action_is_test_for x JOIN facade_action t ON t.id = x.to_action_id
                           WHERE x.from_action_id = a.id),
          'ports', (SELECT coalesce(jsonb_agg(to_jsonb(p) - 'action_id' ORDER BY p.side, p.key_path), '[]')
                      FROM ports p WHERE p.action_id = a.id))
        FROM facade_action a WHERE a.id = i.action_id),
      'dependencies', (SELECT coalesce(jsonb_agg(to_jsonb(d) - 'id' - 'created_at' - 'implementation_id' ORDER BY d.key), '[]')
                         FROM facade_dependency d WHERE d.implementation_id = i.id),
      'manipulates', (SELECT coalesce(jsonb_agg(s.interface ORDER BY s.interface), '[]') FROM facade_implementation_manipulates m
                        JOIN facade_state s ON s.id = m.state_id WHERE m.implementation_id = i.id)
    ) ORDER BY i.interface) FROM facade_implementation i WHERE i.agent_id = $1), '[]'),
  'states', coalesce((SELECT jsonb_agg(jsonb_build_object(
      'interface', s.interface, 'key', s.key,
      'app_identifier', CASE WHEN s.app_identifier = (SELECT identifier FROM authentikate_app WHERE id = (SELECT app_id FROM ag))
                             THEN '<agent app>' ELSE s.app_identifier END,
      'definition', (SELECT to_jsonb(sd) - 'id' - 'organization_id' FROM facade_statedefinition sd WHERE sd.id = s.definition_id)
    ) ORDER BY s.interface) FROM facade_state s WHERE s.agent_id = $1), '[]'),
  'locks', coalesce((SELECT jsonb_agg(jsonb_build_object('key', l.key, 'description', l.description, 'held', l.hold_by_id IS NOT NULL)
                       ORDER BY l.key) FROM facade_lock l WHERE l.agent_id = $1), '[]'),
  'bloks', coalesce((SELECT jsonb_agg(jsonb_build_object(
      'name', replace(m.name, $2, '<side>'), 'description', m.description,
      'blok', (SELECT jsonb_build_object(
          'name', replace(b.name, $2, '<side>'), 'description', b.description, 'components', b.components,
          'demo_state', b.demo_state, 'diagnostics', b.diagnostics,
          'catalog', (SELECT name FROM facade_uicatalog WHERE id = b.catalog_id),
          'dependencies', (SELECT coalesce(jsonb_agg(to_jsonb(d) - 'id' - 'created_at' - 'blok_id' ORDER BY d.key), '[]')
                             FROM facade_blokdependency d WHERE d.blok_id = b.id))
        FROM facade_blok b WHERE b.id = m.blok_id),
      'mappings', (SELECT coalesce(jsonb_agg(jsonb_build_object('key', bm.key,
                       'dependency', (SELECT key FROM facade_blokdependency WHERE id = bm.dependency_id),
                       'agent_is_declarer', bm.agent_id = $1) ORDER BY bm.key), '[]')
                     FROM facade_blokagentmapping bm WHERE bm.materialized_blok_id = m.id)
    ) ORDER BY m.name) FROM facade_materializedblok m WHERE m.declared_by_id = $1), '[]')
)
"#;

async fn snapshot(db: &PgPool, agent: i64, side: &str) -> Value {
    let mut snapshot: Value = sqlx::query_scalar(SNAPSHOT)
        .bind(agent)
        .bind(side)
        .fetch_one(db)
        .await
        .unwrap();
    // The agent's name is its client id unless declared: compare whether it is, not the id.
    if snapshot["agent"]["name_is_client"] == json!(true) {
        snapshot["agent"]["name"] = json!("<client id>");
    }
    snapshot
}

/// The synthetic declarations, `{side}` standing for the agent's side where names are org-wide
/// (bloks are unique per organization, and both agents share one).
fn synthetic_steps() -> Vec<(&'static str, Value)> {
    let image = |requires: Value| json!({"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": false, "requires": requires});
    let definition = |key: &str, args: Value, returns: Value| {
        json!({"key": key, "name": key, "kind": "FUNCTION", "args": args, "returns": returns,
               "collections": ["parity-collection", "parity-other"], "catalogs": ["base@1", "base@2", "not-registered"]})
    };
    let segment = json!({
        "interface": "segment",
        "definition": definition("segment",
            json!([image(json!([{"key": "axes", "operator": "IN", "value": ["c", "t"]},
                                 {"key": "$.@mikro/n", "operator": "GTE", "value": 2},
                                 {"key": "x", "operator": "EXISTS", "value": true}])),
                   {"key": "sizes", "kind": "LIST", "nullable": false, "children": [{"key": "...", "kind": "INT", "nullable": false}]},
                   {"key": "threshold", "kind": "FLOAT", "nullable": true,
                    "effects": [{"kind": "HIDE", "call": {"operation": "made_up_operation", "arguments": []}}]}]),
            json!([{"key": "labels", "kind": "STRUCTURE", "identifier": "@mikro/labels", "nullable": false,
                    "provides": [{"key": "dtype", "operator": "EQUALS", "value": "uint16"}]}])),
        "dependencies": [{"key": "stage", "app": "stage-app", "version": "2", "auto_resolvable": true,
                          "min_viable_instances": 1, "action_dependencies": [{"key": "move"}]}],
        "tracks": [{"state_key": "position", "value_key": "x"}],
        "manipulates": ["position"],
        "params": {"gpu": true},
        "effects": "REPEATABLE",
        "code_hash": "abc",
    });
    let segment_test = json!({
        "interface": "segment_test",
        "definition": {"key": "segment_test", "name": "Segment test", "kind": "FUNCTION",
                       "is_test_for": [{"key": "segment"}, {"key": "missing"}], "args": [], "returns": []},
        "provenance_audience": ["declared"],
    });
    let state = json!({"interface": "position", "definition": {"name": "Position", "ports": [
        {"key": "x", "kind": "FLOAT", "nullable": false}, {"key": "y", "kind": "FLOAT", "nullable": false}]}});
    let blok = |deps: Value| {
        json!({"key": "panel-{side}", "description": "A panel", "components": [
            {"id": "root", "component": "Stack", "props": [
                {"key": "onClick", "util_call": {"operation": "made_up_ui_operation", "arguments": []}}],
             "children": [{"id": "leaf", "component": "Text", "props": [{"key": "text", "static_value": "hi"}]}]}],
            "dependencies": deps, "demo_state": {"x": 1}})
    };
    let stage_dep = json!({"key": "stage", "app": "stage-app", "optional": true, "assign_policy": "ROUND_ROBIN"});
    let camera_dep = json!({"key": "camera", "description": "the camera"});

    // An interface moving to another action: the old action keeps no implementation and goes.
    let mut moved = segment.clone();
    moved["definition"]["key"] = json!("segment_v2");
    // The same action, redefined: the update path and a port rebuild.
    let mut changed = segment.clone();
    changed["definition"]["args"][1]["children"][0]["kind"] = json!("FLOAT");
    changed["definition"]["description"] = json!("changed");

    vec![
        (
            "full",
            json!({"name": "parity", "description": "an agent", "hash": "h1",
                        "implementations": [segment, segment_test], "states": [state],
                        "locks": [{"key": "stage-lock", "definition": {"key": "stage-lock", "description": "moves"}}],
                        "bloks": [blok(json!([stage_dep, camera_dep]))]}),
        ),
        (
            "same again",
            json!({"name": "parity", "hash": "h2",
                              "implementations": [segment_test, changed.clone()], "states": [state],
                              "locks": [{"key": "stage-lock", "definition": {"key": "stage-lock", "description": "moves more"}}],
                              "bloks": [blok(json!([stage_dep]))]}),
        ),
        (
            "moved",
            json!({"hash": "h3", "implementations": [moved], "states": [],
                         "bloks": [blok(json!([camera_dep]))]}),
        ),
        // The model allows EXISTS without a value; compiling it refuses, on both servers.
        (
            "exists without a value",
            json!({"hash": "h35", "implementations": [{
            "interface": "bad",
            "definition": definition("bad", json!([image(json!([{"key": "x", "operator": "EXISTS"}]))]), json!([]))}]}),
        ),
        (
            "pure and irreversible",
            json!({"hash": "h4", "implementations": [{
            "interface": "bad", "effects": "IRREVERSIBLE",
            "definition": {"key": "bad", "name": "bad", "kind": "FUNCTION", "pure": true}}]}),
        ),
        (
            "bad descriptor",
            json!({"hash": "h5", "implementations": [{
            "interface": "bad",
            "definition": definition("bad", json!([image(json!([{"key": "", "operator": "EQUALS", "value": 1}]))]), json!([]))}]}),
        ),
        (
            "wrong call",
            json!({"hash": "h6", "implementations": [{
            "interface": "wrong",
            "definition": {"key": "wrong", "name": "wrong", "kind": "FUNCTION", "args": [
                {"key": "a", "kind": "INT", "nullable": false,
                 "validators": [{"call": {"operation": "gt", "arguments": [{"key": "nope", "value_literal": 1}]}}]}]}}]}),
        ),
        (
            "empty",
            json!({"hash": "h7", "implementations": [], "states": [], "bloks": []}),
        ),
    ]
}

/// The paths at which two JSON values differ, with both sides.
fn diff(path: &str, got: &Value, expected: &Value, out: &mut Vec<String>) {
    match (got, expected) {
        (Value::Object(a), Value::Object(b)) => {
            let keys: std::collections::BTreeSet<&String> = a.keys().chain(b.keys()).collect();
            for key in keys {
                diff(
                    &format!("{path}.{key}"),
                    a.get(key).unwrap_or(&Value::Null),
                    b.get(key).unwrap_or(&Value::Null),
                    out,
                );
            }
        }
        (Value::Array(a), Value::Array(b)) if a.len() == b.len() => {
            for (i, (x, y)) in a.iter().zip(b).enumerate() {
                diff(&format!("{path}[{i}]"), x, y, out);
            }
        }
        _ if got != expected => {
            let short = |v: &Value| v.to_string().chars().take(300).collect::<String>();
            out.push(format!(
                "{path}: rust {} | python {}",
                short(got),
                short(expected)
            ));
        }
        _ => {}
    }
}

fn for_side(payload: &Value, side: &str) -> Value {
    serde_json::from_str(&payload.to_string().replace("{side}", side)).unwrap()
}

#[tokio::test]
async fn registration_writes_what_python_writes() {
    let Ok(url) = std::env::var("AGENTD_TEST_DATABASE_URL") else {
        eprintln!("skipped: AGENTD_TEST_DATABASE_URL is not set");
        return;
    };
    let db = PgPool::connect(&url).await.unwrap();
    let org = format!("parity-{}", uuid::Uuid::new_v4().simple());
    let python = agent(&db, &org, "py").await;
    let rust = agent(&db, &org, "rs").await;

    let apps: serde_json::Map<String, Value> = serde_json::from_str(include_str!(
        "../../rekuest-server-core/tests/fixtures/app_declarations.json"
    ))
    .unwrap();
    let mut steps: Vec<(String, Value)> = apps
        .into_iter()
        .map(|(name, mut payload)| {
            payload["hash"] = json!(format!("app-{name}"));
            (format!("app {name}"), payload)
        })
        .collect();
    steps.extend(
        synthetic_steps()
            .into_iter()
            .map(|(name, payload)| (name.to_owned(), payload)),
    );

    let fixture_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/registration_parity.json");
    let recording = std::env::var("PARITY_RECORD").is_ok();
    let recorded: Vec<Value> = if recording {
        vec![]
    } else {
        serde_json::from_str(&std::fs::read_to_string(&fixture_path).expect("the recorded fixture"))
            .unwrap()
    };
    let mut recording_out = vec![];

    let mut failures = vec![];
    for (index, (name, payload)) in steps.iter().enumerate() {
        let (expected, expected_rows) = if recording {
            let outcome = python_implement(python, &for_side(payload, "py")).await;
            let rows = mask(snapshot(&db, python, "py").await);
            recording_out.push(json!({"step": name, "outcome": outcome.to_json(), "rows": rows}));
            (outcome, rows)
        } else {
            let step = &recorded[index];
            assert_eq!(
                step["step"].as_str(),
                Some(name.as_str()),
                "the fixture's steps are these steps"
            );
            (Outcome::from_json(&step["outcome"]), step["rows"].clone())
        };
        let got = rust_implement(&db, rust, &for_side(payload, "rs")).await;
        let got_rows = mask(snapshot(&db, rust, "rs").await);
        eprintln!(
            "{name}: {}",
            match &got {
                Outcome::Accepted(d) => format!(
                    "accepted, {} diagnostic(s)",
                    d.as_array().map_or(0, Vec::len)
                ),
                Outcome::Refused(e) => format!("refused: {e}"),
            }
        );
        match (&expected, &got) {
            (Outcome::Refused(_), Outcome::Refused(_))
            | (Outcome::Accepted(_), Outcome::Accepted(_))
                if expected == got => {}
            // pydantic words its own shape errors; only that both refused matters there.
            (Outcome::Refused(e), Outcome::Refused(g))
                if g.starts_with("shape: ") || e.contains("validation error") => {}
            _ => failures.push(format!(
                "{name}: outcome differs\n  rust:   {got:?}\n  python: {expected:?}"
            )),
        }
        if expected_rows != got_rows {
            let mut differences = vec![];
            diff("", &got_rows, &expected_rows, &mut differences);
            failures.push(format!(
                "{name}: rows differ\n  {}",
                differences.join("\n  ")
            ));
        }
    }
    if recording {
        std::fs::write(
            &fixture_path,
            serde_json::to_string_pretty(&recording_out).unwrap() + "\n",
        )
        .unwrap();
        eprintln!(
            "recorded {} steps into {}",
            recording_out.len(),
            fixture_path.display()
        );
    }
    assert!(
        failures.is_empty(),
        "{} of {} steps differ:\n{}",
        failures.len(),
        steps.len(),
        failures.join("\n")
    );
}
