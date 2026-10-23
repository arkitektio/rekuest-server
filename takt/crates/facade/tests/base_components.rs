//! The base catalog's control flow (`foreach`, `if`, …) is in force under every UI catalog,
//! which registers only what it draws: a blok looping over its items checks out against a
//! catalog that does not list `foreach`, and base wins over a catalog that still does. What the
//! UI catalog lacks is a finding, never a refusal.

use facade::catalog_validation::{validate_manifest_against_catalog, Catalog};
use rekuest_core::bsx::parse_bsx;
use rekuest_core::enums::ComponentContext;
use serde_json::json;

fn catalog(components: serde_json::Value) -> Catalog {
    Catalog {
        id: 1,
        name: "orkestrator".into(),
        components: serde_json::from_value(components).unwrap(),
        operations: vec![],
    }
}

const PICKER: &str = r##"<Column>
  <foreach items="@args.lenses" let="#lens">
    <Button label="Use" />
  </foreach>
</Column>"##;

#[test]
fn a_loop_needs_no_foreach_in_the_ui_catalog() {
    let tree = vec![parse_bsx(PICKER).unwrap()];
    let ui = catalog(
        json!([{"name": "Column"}, {"name": "Button", "props": [{"key": "label", "kind": "STRING"}]}]),
    );
    let checked = validate_manifest_against_catalog(&ui, Some(&tree), ComponentContext::HTML, None);
    assert!(checked.is_ok(), "{checked:?}");

    // An older registration that still lists its own `foreach` is not a conflict: base wins.
    let older = catalog(
        json!([{"name": "Column"}, {"name": "Button", "props": [{"key": "label", "kind": "STRING"}]}, {"name": "foreach", "accepts_children": false}]),
    );
    assert!(
        validate_manifest_against_catalog(&older, Some(&tree), ComponentContext::HTML, None)
            .is_ok()
    );
}

#[test]
fn what_the_ui_draws_is_a_finding() {
    let tree = vec![parse_bsx("<Column><Knob /></Column>").unwrap()];
    let ui = catalog(json!([{"name": "Column"}]));
    // The renderer draws what it does not know as an inline error: the server only notes it.
    let found =
        validate_manifest_against_catalog(&ui, Some(&tree), ComponentContext::HTML, None).unwrap();
    assert_eq!(found.len(), 1, "{found:?}");
    assert_eq!(found[0].code, "unknown_component");
    assert!(found[0].message.contains("'Knob'"), "{}", found[0].message);
}
