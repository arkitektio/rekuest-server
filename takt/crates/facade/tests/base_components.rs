//! A v2 blok is checked against the spec's catalogs plus what its UI catalog registered beyond
//! them: the language (`foreach`, `if`, ...) and the standard modules are always in force; the
//! Orkestrator's registered module only adds what the spec lacks, another module's definition
//! of a standard component is drawn (`catalog.shadow`), and what the catalog lacks is a
//! warning, never a refusal.

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

const PICKER: &str = r#"<Html>
  <Column>
    <foreach items={args.lenses} let="lens" key={lens.id}>
      <Button>Use</Button>
    </foreach>
  </Column>
</Html>"#;

#[test]
fn the_language_and_the_modules_need_no_registration() {
    let tree = vec![parse_bsx_default(PICKER).unwrap()];
    let empty = catalog("orkestrator", json!([]));
    let checked = validate_blok_against_catalog(&empty, Some(&tree), &BlokScope::default());
    assert_eq!(checked.map(|c| c.diagnostics), Ok(vec![]));
}

#[test]
fn the_orkestrators_registration_never_replaces_the_spec() {
    let tree = vec![parse_bsx_default(PICKER).unwrap()];
    let older = catalog(
        "orkestrator",
        json!([{"name": "Button", "props": [{"key": "label", "kind": "STRING"}]}]),
    );
    let found = validate_blok_against_catalog(&older, Some(&tree), &BlokScope::default())
        .unwrap()
        .diagnostics;
    assert_eq!(found, vec![]);
}

#[test]
fn a_module_shadows_a_standard_component() {
    let tree = vec![parse_bsx_default(PICKER).unwrap()];
    let mine = catalog(
        "mine",
        json!([{"name": "Button", "props": [{"key": "tone", "kind": "STRING"}]}]),
    );
    let found = validate_blok_against_catalog(&mine, Some(&tree), &BlokScope::default())
        .unwrap()
        .diagnostics;
    let codes: Vec<&str> = found.iter().map(|d| d.code.as_str()).collect();
    // Its Button is drawn: one that takes no label prop (the text child fills `label`).
    assert_eq!(codes, ["catalog.props", "catalog.shadow"], "{found:?}");
    assert!(
        found[1].message.contains("mine's definition is used"),
        "{found:?}"
    );
}

#[test]
fn what_no_catalog_draws_is_a_warning() {
    let tree = vec![parse_bsx_default("<Html><Column><Knob /></Column></Html>").unwrap()];
    let found = validate_blok_against_catalog(
        &catalog("orkestrator", json!([])),
        Some(&tree),
        &BlokScope::default(),
    )
    .unwrap()
    .diagnostics;
    assert_eq!(found.len(), 1, "{found:?}");
    assert_eq!(found[0].code, "catalog.unknown_component");
    let knob = catalog("orkestrator", json!([{"name": "Knob"}]));
    assert_eq!(
        validate_blok_against_catalog(&knob, Some(&tree), &BlokScope::default())
            .map(|c| c.diagnostics),
        Ok(vec![])
    );
}

#[test]
fn an_error_refuses_with_its_code() {
    let tree = vec![parse_bsx_default("<Html><Text text={task.status} /></Html>").unwrap()];
    let refused = validate_blok_against_catalog(
        &catalog("orkestrator", json!([])),
        Some(&tree),
        &BlokScope::default(),
    )
    .unwrap_err();
    assert!(refused.contains("[roots.not_in_kind]"), "{refused}");
}

#[test]
fn a_checked_blok_carries_its_resolved_scope() {
    let tree = vec![parse_bsx_default("<Html><Switch bind={on} /></Html>").unwrap()];
    let scope = BlokScope {
        locals: vec![serde_json::from_value(json!({"key": "on"})).unwrap()],
        ..BlokScope::default()
    };
    assert_eq!(scope.locals[0].kind, None);
    let checked =
        validate_blok_against_catalog(&catalog("orkestrator", json!([])), Some(&tree), &scope)
            .unwrap();
    assert_eq!(
        checked.scope.locals[0].kind,
        Some(rekuest_core::enums::LocalKind::BOOL)
    );
}

#[test]
fn a_custom_blok_must_declare_the_dependencies_it_reads() {
    let tree = vec![parse_bsx_default("<Html><Text text={deps.robot.arm.mode} /></Html>").unwrap()];
    let scope = BlokScope {
        kind: "custom".into(),
        ..BlokScope::default()
    };
    let refused =
        validate_blok_against_catalog(&catalog("orkestrator", json!([])), Some(&tree), &scope)
            .unwrap_err();
    assert!(refused.contains("[deps.unknown]"), "{refused}");
    assert!(
        refused.contains(r#"Fix: dependencies=[AgentDependencyInput(key="robot")]"#),
        "{refused}"
    );
}
