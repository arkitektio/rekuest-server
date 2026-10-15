//! takt parses BSX into exactly the tree arkitekt-spec's parser makes, and refuses what it refuses.
//! The corpus is made by `tests/fixtures/generate_bsx.py` with the spec's own `bsx()`.
use rekuest_core::bsx::{parse_bsx, MAX_SOURCE_BYTES};
use serde_json::Value;

/// The spec dumps without the fields it does not set (`exclude_none`); so is the tree compared.
fn without_nulls(value: Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.into_iter()
                .filter(|(_, v)| !v.is_null())
                .map(|(k, v)| (k, without_nulls(v)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.into_iter().map(without_nulls).collect()),
        other => other,
    }
}

#[test]
fn every_source_parses_as_the_spec_parses_it() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/bsx.json")).expect("the corpus is JSON");
    let mut differences = vec![];
    for case in &cases {
        let source = case["source"].as_str().expect("a source");
        let parsed = parse_bsx(source);
        match (case.get("tree"), parsed) {
            (Some(expected), Ok(tree)) => {
                let tree = without_nulls(serde_json::to_value(&tree).unwrap());
                if &tree != expected {
                    differences.push(format!("{source:?}\n  takt: {tree}\n  spec: {expected}"));
                }
            }
            (Some(_), Err(error)) => {
                differences.push(format!("{source:?}\n  takt refused: {error}"))
            }
            (None, Ok(tree)) => differences.push(format!(
                "{source:?}\n  takt parsed what the spec refuses ({}): {}",
                case["error"],
                serde_json::to_value(&tree).unwrap()
            )),
            (None, Err(_)) => {}
        }
    }
    assert!(
        differences.is_empty(),
        "{} of {} cases differ:\n{}",
        differences.len(),
        cases.len(),
        differences.join("\n")
    );
}

#[test]
fn a_parse_error_says_where() {
    let error = parse_bsx("<Page>\n  <Header />\n  <Label>\n</Page>").unwrap_err();
    assert!(error.contains("line 4"), "{error}");
}

#[test]
fn a_source_larger_than_a_blok_is_refused_unread() {
    let source = format!("<Page text=\"{}\" />", "x".repeat(MAX_SOURCE_BYTES));
    assert!(parse_bsx(&source).unwrap_err().contains("at most"));
}
