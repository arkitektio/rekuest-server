//! Hashes and scope of what an agent declares (`facade/unique.py`).

use rekuest_core::enums::{ActionScope, PortKind};
use rekuest_core::inputs::{
    ArgPortInputModel, BlokImplementationInputModel, DefinitionInputModel, ReturnPortInputModel,
    StateDefinitionInputModel,
};
use rekuest_core::pyjson::dumps;
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

/// sha256 over a declared blok's content: what makes two agents' bloks the same template. The
/// agent's own choices (`default`) and the BSX `source` (already parsed into `components`) are
/// left out, so a blok sent as BSX and the same one sent as a tree share a template. So are
/// empty `args`: a blok without them keeps the template it had before bloks took args.
pub fn hash_blok_definition(blok: &BlokImplementationInputModel) -> String {
    let mut dump = serde_json::to_value(blok).expect("a blok declaration serializes");
    if let Value::Object(fields) = &mut dump {
        fields.remove("default");
        fields.remove("source");
        if blok.args.is_empty() {
            fields.remove("args");
        }
        // Declarations made before bloks had returns keep their hash.
        if blok.returns.is_empty() {
            fields.remove("returns");
        }
    }
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

    #[test]
    fn a_blok_without_args_hashes_as_before_args_existed() {
        let blok: BlokImplementationInputModel = serde_json::from_value(serde_json::json!({
            "key": "panel", "components": [{"id": "root", "component": "Box"}]}))
        .unwrap();
        let mut dump = serde_json::to_value(&blok).unwrap();
        let fields = dump.as_object_mut().unwrap();
        assert_eq!(fields.remove("args"), Some(serde_json::json!([])));
        fields.remove("default");
        fields.remove("source");
        let before = hex::encode(Sha256::digest(dumps(&dump, true).as_bytes()));
        assert_eq!(hash_blok_definition(&blok), before);
        let mut with_args = blok.clone();
        with_args.args = serde_json::from_value(serde_json::json!([
            {"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": false}]))
        .unwrap();
        assert_ne!(
            hash_blok_definition(&blok),
            hash_blok_definition(&with_args)
        );
    }
}
