//! Hashes and scope of what an agent declares (`facade/unique.py`).

use rekuest_core::enums::{ActionScope, PortKind};
use rekuest_core::inputs::{ArgPortInputModel, DefinitionInputModel, ReturnPortInputModel, StateDefinitionInputModel};
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
