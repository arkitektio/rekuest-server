//! A v2 blok is held to the language alone. The server reads no catalog: whatever a module
//! registered, a component nobody draws, a prop a component does not take or a control whose
//! `bind` would type a local is the frontend's to check, the one that draws the blok. What the
//! language says (a root the kind does not read, an undeclared dependency) still refuses it.

use facade::catalog_validation::{validate_blok_against_catalog, Catalog};
use rekuest_core::blok::BlokScope;
use rekuest_core::bsx::parse_bsx_default;
use serde_json::json;

fn catalog(name: &str, components: serde_json::Value) -> Catalog {
    Catalog {
        id: 1,
        name: name.into(),
        components: serde_json::from_value(components).unwrap(),
        operations: vec![],
    }
}

const PICKER: &str = r#"<Blok kind="agent" context="panel">
  <Column>
    <foreach items={args.lenses} let="lens" key={lens.id}>
      <Button>Use</Button>
    </foreach>
  </Column>
</Blok>"#;

/// Mounted as an agent's blok, with no dependencies.
fn agent() -> BlokScope {
    BlokScope::mounted(&["agent"], "the agent's blok", vec![])
}

#[test]
fn the_language_and_the_modules_need_no_registration() {
    let tree = vec![parse_bsx_default(PICKER).unwrap()];
    let empty = catalog("orkestrator", json!([]));
    let checked = validate_blok_against_catalog(&empty, Some(&tree), &agent());
    assert_eq!(checked.map(|c| c.diagnostics), Ok(vec![]));
}

#[test]
fn the_orkestrators_registration_never_replaces_the_spec() {
    let tree = vec![parse_bsx_default(PICKER).unwrap()];
    let older = catalog(
        "orkestrator",
        json!([{"name": "Button", "props": [{"key": "label", "kind": "STRING"}]}]),
    );
    let found = validate_blok_against_catalog(&older, Some(&tree), &agent())
        .unwrap()
        .diagnostics;
    assert_eq!(found, vec![]);
}

#[test]
fn what_a_module_registered_is_not_read() {
    let tree = vec![parse_bsx_default(PICKER).unwrap()];
    // Its Button takes no label: that is the catalog's to say, to whoever draws the blok.
    let mine = catalog(
        "mine",
        json!([{"name": "Button", "props": [{"key": "tone", "kind": "STRING"}]}]),
    );
    let found = validate_blok_against_catalog(&mine, Some(&tree), &agent())
        .unwrap()
        .diagnostics;
    assert_eq!(found, vec![]);
}

#[test]
fn what_no_catalog_draws_is_not_the_servers_to_judge() {
    let tree = vec![parse_bsx_default(
        r#"<Blok kind="agent" context="panel"><Column><Knob /></Column></Blok>"#,
    )
    .unwrap()];
    for components in [json!([]), json!([{"name": "Knob"}])] {
        let found = validate_blok_against_catalog(
            &catalog("orkestrator", components),
            Some(&tree),
            &agent(),
        )
        .unwrap()
        .diagnostics;
        assert_eq!(found, vec![]);
    }
}

#[test]
fn an_error_refuses_with_its_code() {
    let tree = vec![parse_bsx_default(
        r#"<Blok kind="agent" context="panel"><Text text={task.status} /></Blok>"#,
    )
    .unwrap()];
    let refused =
        validate_blok_against_catalog(&catalog("orkestrator", json!([])), Some(&tree), &agent())
            .unwrap_err();
    assert!(refused.contains("[roots.not_in_kind]"), "{refused}");
}

#[test]
fn a_checked_blok_carries_its_resolved_locals() {
    let checked = |source: &str| {
        let tree = vec![parse_bsx_default(source).unwrap()];
        validate_blok_against_catalog(&catalog("orkestrator", json!([])), Some(&tree), &agent())
            .unwrap()
    };
    // What a Switch writes through its `bind` is its catalog's to say: with nothing else to
    // tell its kind the local stays ANY here, and the renderer types it.
    let found = checked(
        r#"<Blok kind="agent" context="panel"><local name="on" /><Switch bind={on} /></Blok>"#,
    );
    assert_eq!(found.locals[0].0, "Blok[0]/local[0]");
    assert_eq!(
        found.locals[0].1.local.kind,
        Some(rekuest_core::enums::LocalKind::ANY)
    );
    let found = checked(
        r#"<Blok kind="agent" context="panel"><local name="on" default={false} /><Switch bind={on} /></Blok>"#,
    );
    assert_eq!(
        found.locals[0].1.local.kind,
        Some(rekuest_core::enums::LocalKind::BOOL)
    );
    assert_eq!(
        found.stored_locals(),
        json!([{"node": "Blok[0]/local[0]", "key": "on", "kind": "BOOL", "default": false,
                "nullable": false, "persist": false}]),
        "stored as its node and its port"
    );
}

#[test]
fn a_custom_blok_must_declare_the_dependencies_it_reads() {
    let tree = vec![parse_bsx_default(
        r#"<Blok kind="custom" context="panel"><Text text={agents.robot.arm.mode} /></Blok>"#,
    )
    .unwrap()];
    let scope = BlokScope::mounted(&["custom"], "a custom blok", vec![]);
    let refused =
        validate_blok_against_catalog(&catalog("orkestrator", json!([])), Some(&tree), &scope)
            .unwrap_err();
    assert!(refused.contains("[deps.unknown]"), "{refused}");
    assert!(
        refused.contains(r#"Fix: dependencies=[AgentDependencyInput(key="robot")]"#),
        "{refused}"
    );
}
