//! Hashes and scope of what an agent declares (`facade/unique.py`).

use rekuest_core::enums::{ActionScope, PortKind};
use rekuest_core::inputs::{
    ActionBlokInputModel, ArgPortInputModel, BlokImplementationInputModel, DefinitionInputModel,
    ReturnPortInputModel, StateDefinitionInputModel,
};
use rekuest_core::pyjson::dumps;

use crate::registration::without_nulls;
use serde_json::Value;
use sha2::{Digest, Sha256};

/// sha256 over a state definition's ports, as `json.dumps(…, sort_keys=True)` prints them
/// (`hash_state_definition`).
pub fn hash_state_definition(definition: &StateDefinitionInputModel) -> String {
    let dump = serde_json::to_value(definition).expect("a state definition serializes");
    let ports = dump.get("ports").cloned().unwrap_or(Value::Array(vec![]));
    let subset = serde_json::json!({ "ports": ports });
    hex::encode(Sha256::digest(dumps(&subset, true).as_bytes()))
}

/// The fields of a blok declaration hashed even when empty (the spec's dump rule): everything
/// else counts only when it is set, so a field added in a later vocabulary version leaves the
/// hash of a blok without it alone.
const ALWAYS_HASHED: [&str; 4] = ["key", "vocab", "dependencies", "context"];

/// A blok declaration's dump as hashed: without its nulls (at any depth), and at the top
/// without what is not set; `source` (already parsed into `components`) and `default` (the
/// agent's own choice) never count.
fn hashed_dump(dump: Value) -> Value {
    let Value::Object(mut fields) = dump else {
        unreachable!("a blok declaration dumps as an object")
    };
    // A demo is the author's JSON, its own nulls included.
    let demo = fields.remove("demo");
    let Value::Object(mut fields) = without_nulls(Value::Object(fields)) else {
        unreachable!("a blok declaration dumps as an object")
    };
    if let Some(Value::Object(demo)) = demo {
        fields.insert(
            "demo".into(),
            Value::Object(demo.into_iter().filter(|(_, v)| !v.is_null()).collect()),
        );
    }
    Value::Object(
        fields
            .into_iter()
            .filter(|(key, value)| {
                key != "source"
                    && key != "default"
                    && (ALWAYS_HASHED.contains(&key.as_str())
                        || !matches!(value, Value::Bool(false))
                            && value != &Value::Array(vec![])
                            && value != &Value::Object(serde_json::Map::new()))
            })
            .collect(),
    )
}

/// sha256 over an action's own blok (blok language v2): what makes two actions' bloks one row.
/// The `action_blok` envelope keeps it from ever equalling an agent's template
/// ([`hash_blok_definition`]), which shares the unique constraint.
pub fn hash_action_blok(blok: &ActionBlokInputModel) -> String {
    let dump = hashed_dump(serde_json::to_value(blok).expect("an action blok serializes"));
    let envelope = serde_json::json!({ "action_blok": dump });
    hex::encode(Sha256::digest(dumps(&envelope, true).as_bytes()))
}

/// sha256 over a declared blok's content (blok language v2): what makes two agents' bloks the
/// same template. A blok sent as BSX and the same one sent as a tree share a template.
pub fn hash_blok_definition(blok: &BlokImplementationInputModel) -> String {
    let dump = hashed_dump(serde_json::to_value(blok).expect("a blok declaration serializes"));
    hex::encode(Sha256::digest(dumps(&dump, true).as_bytes()))
}

fn arg_is_local(port: &ArgPortInputModel) -> bool {
    port.kind == PortKind::MEMORY_STRUCTURE || port.children.iter().flatten().any(arg_is_local)
}

fn return_is_local(port: &ReturnPortInputModel) -> bool {
    port.kind == PortKind::MEMORY_STRUCTURE || port.children.iter().flatten().any(return_is_local)
}

/// LOCAL when memory structures go in and out, a BRIDGE when only one side has them
/// (`infer_action_scope`).
pub fn infer_action_scope(definition: &DefinitionInputModel) -> ActionScope {
    let local_args = definition.args.iter().any(arg_is_local);
    let local_returns = definition.returns.iter().any(return_is_local);
    match (local_args, local_returns) {
        (true, true) => ActionScope::LOCAL,
        (false, false) => ActionScope::GLOBAL,
        (false, true) => ActionScope::BRIDGE_GLOBAL_TO_LOCAL,
        (true, false) => ActionScope::BRIDGE_LOCAL_TO_GLOBAL,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn blok(fields: Value) -> BlokImplementationInputModel {
        let mut blok: BlokImplementationInputModel = serde_json::from_value(fields).unwrap();
        blok.validate().unwrap();
        blok
    }

    /// The digest is pinned: a template keeps its row across server versions as long as its
    /// declaration does not change.
    #[test]
    fn a_v2_blok_has_a_pinned_hash() {
        let panel = blok(
            serde_json::json!({"key": "panel", "vocab": 2, "source": "<Html><Card><Button>Go</Button></Card></Html>"}),
        );
        assert_eq!(
            hash_blok_definition(&panel),
            "103a18f95c478800a4e9895abf4d215b75c06535cacf2577177567ff14144798"
        );
    }

    #[test]
    fn source_and_tree_share_a_template_and_defaults_do_not_count() {
        let sent = blok(
            serde_json::json!({"key": "panel", "vocab": 2, "source": "<Html><Card /></Html>", "default": true}),
        );
        let tree = blok(
            serde_json::json!({"key": "panel", "vocab": 2, "components": [{"id": "Html[0]", "component": "Html", "children": [{"id": "Html[0]/Card[0]", "component": "Card"}]}],
            "locals": [], "catalogs": []}),
        );
        assert_eq!(hash_blok_definition(&sent), hash_blok_definition(&tree));
    }

    #[test]
    fn what_a_blok_declares_beside_its_tree_counts() {
        let plain = blok(
            serde_json::json!({"key": "panel", "vocab": 2, "source": "<Html><Card /></Html>"}),
        );
        for extra in [
            serde_json::json!({"locals": [{"key": "n", "kind": "INT", "default": 1}]}),
            serde_json::json!({"catalogs": ["ui@1"]}),
            serde_json::json!({"module": "orkestrator"}),
            serde_json::json!({"demo": {"args": {"x": 1}}}),
        ] {
            let mut fields =
                serde_json::json!({"key": "panel", "vocab": 2, "source": "<Html><Card /></Html>"});
            fields
                .as_object_mut()
                .unwrap()
                .extend(extra.as_object().unwrap().clone());
            assert_ne!(
                hash_blok_definition(&plain),
                hash_blok_definition(&blok(fields))
            );
        }
    }
}
