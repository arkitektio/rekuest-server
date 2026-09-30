//! What the Python server's models make of declarations, reproduced: the same `model_dump`,
//! the same definition hashes, the same refusals worded the same way. The fixture is made by
//! `tests/fixtures/generate_declarations.py` with the server's own models.

use rekuest_core::inputs::ImplementAgentInputModel;
use serde_json::Value;

fn cases() -> serde_json::Map<String, Value> {
    serde_json::from_str::<Value>(include_str!("fixtures/declarations.json"))
        .unwrap()
        .as_object()
        .unwrap()
        .clone()
}

/// Parse and validate, as the server would.
fn validate(input: &Value) -> Result<ImplementAgentInputModel, String> {
    let mut model: ImplementAgentInputModel =
        serde_json::from_value(input.clone()).map_err(|e| format!("shape: {e}"))?;
    model.validate().map_err(|e| e.0)?;
    Ok(model)
}

#[test]
fn every_declaration_is_judged_as_python_judges_it() {
    let cases = cases();
    assert!(cases.len() >= 60, "the fixture lost cases");
    let mut failures = vec![];
    for (name, case) in &cases {
        let outcome = validate(&case["input"]);
        match (outcome, case.get("error")) {
            (Ok(model), None) => {
                let dump = serde_json::to_value(&model).unwrap();
                if dump != case["dump"] {
                    failures.push(format!(
                        "{name}: model_dump differs\n  rust:   {dump}\n  python: {}",
                        case["dump"]
                    ));
                }
                let hashes: Vec<String> = model
                    .implementations
                    .iter()
                    .flatten()
                    .map(|i| i.definition.unique_hash())
                    .collect();
                let expected: Vec<String> = serde_json::from_value(case["hashes"].clone()).unwrap();
                if hashes != expected {
                    failures.push(format!(
                        "{name}: hashes differ\n  rust:   {hashes:?}\n  python: {expected:?}"
                    ));
                }
            }
            (Ok(_), Some(error)) => {
                failures.push(format!("{name}: accepted, Python refused with {error}"))
            }
            (Err(message), None) => {
                failures.push(format!("{name}: refused ({message}), Python accepted"))
            }
            (Err(message), Some(error)) => {
                // Our own validators word it as Python's value errors; pydantic's own shape
                // errors (an unknown field, a missing one) only need to be refused.
                let expected = error.as_str().unwrap();
                // A GraphQL syntax error's detail is the parser's own (graphql-core in Python,
                // graphql-parser here); the sentence around it is ours.
                const QUERY_PARSE: &str = "SEARCH widget query does not parse:";
                let same = if expected.starts_with(QUERY_PARSE) {
                    message.starts_with(QUERY_PARSE)
                } else {
                    message == expected
                };
                if case["error_type"] == "value_error" && !same {
                    failures.push(format!(
                        "{name}: refused differently\n  rust:   {message}\n  python: {error}"
                    ));
                }
            }
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} cases differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}
