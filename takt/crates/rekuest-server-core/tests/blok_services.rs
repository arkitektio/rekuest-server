//! A blok names the services its `query` components read, each once (`check_blok_services`).

use rekuest_core::inputs::BlokImplementationInputModel;
use serde_json::json;

fn blok(services: serde_json::Value) -> Result<(), String> {
    let mut model: BlokImplementationInputModel = serde_json::from_value(json!({
        "key": "cells",
        "components": [{"id": "root", "component": "Box", "children": [
            {"id": "q", "component": "query", "props": [{"key": "schema", "static_value": "mikro"}]},
        ]}],
        "services": services,
    }))
    .map_err(|e| e.to_string())?;
    model.validate().map_err(|e| e.0)
}

#[test]
fn a_query_reads_only_a_service_the_blok_declares() {
    assert_eq!(blok(json!([{"key": "mikro", "version": "8"}])), Ok(()));
    assert_eq!(
        blok(json!([])).unwrap_err(),
        "Blok manifest: component 'q' queries the 'mikro' service, which the blok does not declare in its services (declared: none)"
    );
    assert_eq!(
        blok(json!([{"key": "kraph"}])).unwrap_err(),
        "Blok manifest: component 'q' queries the 'mikro' service, which the blok does not declare in its services (declared: ['kraph'])"
    );
}

#[test]
fn each_service_is_named_once() {
    assert!(blok(json!([{"key": "mikro"}, {"key": "mikro"}]))
        .unwrap_err()
        .contains("duplicate key 'mikro'"));
}

#[test]
fn services_stay_out_of_a_dump_without_them() {
    let model: BlokImplementationInputModel =
        serde_json::from_value(json!({"key": "plain", "components": []})).unwrap();
    assert!(serde_json::to_value(&model).unwrap().get("services").is_none());
}
