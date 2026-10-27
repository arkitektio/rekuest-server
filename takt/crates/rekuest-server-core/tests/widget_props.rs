//! A port widget's props feed the definition hash, so they keep the v1 rules and dump exactly
//! as they did before blok v2: a bool static value is its int.
use rekuest_core::inputs::ComponentPropInputModel;
use serde_json::json;

#[test]
fn a_widget_prop_dumps_as_before_blok_v2() {
    let mut prop: ComponentPropInputModel =
        serde_json::from_value(json!({"key": "flag", "static_value": true})).unwrap();
    prop.validate().unwrap();
    assert_eq!(
        serde_json::to_value(&prop).unwrap(),
        json!({"key": "flag", "static_value": 1, "dynamic_value": null, "declares_value": null, "agent_call": null, "util_call": null})
    );
}

#[test]
fn a_widget_prop_keeps_the_v1_refusals() {
    let mut list: ComponentPropInputModel =
        serde_json::from_value(json!({"key": "xs", "static_value": [1]})).unwrap();
    assert!(list.validate().is_err());
    let mut empty: ComponentPropInputModel = serde_json::from_value(json!({"key": "x"})).unwrap();
    assert!(empty.validate().is_err());
}

#[test]
fn a_blok_prop_is_kept_as_written() {
    let prop: ComponentPropInputModel =
        serde_json::from_value(json!({"key": "persist", "static_value": true})).unwrap();
    prop.validate_blok().unwrap();
    assert_eq!(prop.static_value, Some(json!(true)));
    let null: ComponentPropInputModel = serde_json::from_value(json!({"key": "value"})).unwrap();
    null.validate_blok().unwrap();
    let twice: ComponentPropInputModel = serde_json::from_value(
        json!({"key": "x", "static_value": 1, "util_call": {"operation": "query.refetch"}}),
    )
    .unwrap();
    assert!(twice.validate_blok().is_err());
}
