//! The catalog rules against the cases both servers share
//! (`rekuest_core/catalogs/validation_cases.json`, which the Python runner
//! `tests/models/test_validation_cases.py` reads too): what is refused, and which findings come,
//! in order. A rule changed on one side only fails here or there. Skipped outside the
//! repository, where the file is not.

use facade::catalog_validation::{validate_components_against_catalogs, Catalog};
use rekuest_core::enums::ComponentContext;
use serde_json::Value;

fn cases() -> Option<Value> {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../rekuest_core/catalogs/validation_cases.json");
    let text = std::fs::read_to_string(path).ok()?;
    Some(serde_json::from_str(&text).expect("the shared cases parse"))
}

fn catalog(index: usize, value: &Value) -> Catalog {
    Catalog {
        id: index as i64,
        name: value["name"].as_str().unwrap().to_owned(),
        components: serde_json::from_value(
            value
                .get("components")
                .cloned()
                .unwrap_or(Value::Array(vec![])),
        )
        .unwrap(),
        operations: serde_json::from_value(
            value
                .get("operations")
                .cloned()
                .unwrap_or(Value::Array(vec![])),
        )
        .unwrap(),
    }
}

#[test]
fn every_shared_case_holds() {
    let Some(cases) = cases() else { return };
    let cases = cases["cases"].as_array().unwrap();
    assert!(!cases.is_empty(), "the shared file holds no cases");
    println!("{} shared cases", cases.len());
    let mut wrong = vec![];
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let catalogs: Vec<Catalog> = case["catalogs"]
            .as_array()
            .unwrap()
            .iter()
            .enumerate()
            .map(|(i, c)| catalog(i, c))
            .collect();
        let components: Vec<_> = serde_json::from_value(case["components"].clone()).unwrap();
        let context: ComponentContext =
            serde_json::from_value(case.get("context").cloned().unwrap_or(Value::from("HTML")))
                .unwrap();
        let demo_state = case.get("demo_state").and_then(Value::as_object);
        let action = case.get("action").and_then(Value::as_bool).unwrap_or(false);
        let outcome = validate_components_against_catalogs(
            &catalogs,
            Some(&components),
            context,
            demo_state,
            action,
        );
        match (case["expect"]["refused"].as_str(), outcome) {
            (Some(refused), Err(reason)) if reason.contains(refused) => {}
            (None, Ok(diagnostics)) => {
                let got: Vec<&str> = diagnostics.iter().map(|d| d.code).collect();
                let expected: Vec<&str> = case["expect"]["diagnostics"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|c| c.as_str().unwrap())
                    .collect();
                if got != expected {
                    wrong.push(format!("{name}: findings {got:?}, expected {expected:?}"));
                }
            }
            (expected, outcome) => {
                wrong.push(format!("{name}: refused {expected:?}, got {outcome:?}"))
            }
        }
    }
    assert!(
        wrong.is_empty(),
        "{} of the shared cases differ:\n{}",
        wrong.len(),
        wrong.join("\n")
    );
}
