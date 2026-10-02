//! What a definition's shape says it is (`facade/inference/`): each check names the protocol a
//! matching action implements.

use rekuest_core::enums::PortKind;
use rekuest_core::inputs::DefinitionInputModel;

/// A protocol a definition implements: its name and description.
pub type Inferred = (&'static str, &'static str);

/// One BOOL return: a predicate (`is_predicate`).
pub fn is_predicate(definition: &DefinitionInputModel) -> Option<Inferred> {
    match definition.returns.as_slice() {
        [only] if only.kind == PortKind::BOOL => Some(("predicate", "Is this a predicate?")),
        _ => None,
    }
}

/// One `@rekuest/taskevent` argument: a hook (`is_hook`).
pub fn is_hook(definition: &DefinitionInputModel) -> Option<Inferred> {
    match definition.args.as_slice() {
        [only] if only.identifier.as_deref() == Some("@rekuest/taskevent") => {
            Some(("hook", "Is this a hook?"))
        }
        _ => None,
    }
}

/// One `@lok/room` argument: an LLM agent (`is_agent`).
pub fn is_agent(definition: &DefinitionInputModel) -> Option<Inferred> {
    match definition.args.as_slice() {
        [only] if only.identifier.as_deref() == Some("@lok/room") => {
            Some(("agent", "Is this a agent?"))
        }
        _ => None,
    }
}
