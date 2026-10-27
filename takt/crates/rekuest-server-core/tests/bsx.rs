//! takt parses BSX v2 into exactly the tree arkitekt-spec's parser makes, refuses what it
//! refuses (same code, line and column, in code points), and its checker finds the codes the
//! spec's validator finds. The corpus is written by the spec (`arkitekt_spec.blok.corpus`, see
//! `tests/fixtures/generate_bsx.py`).
use std::collections::HashSet;

use rekuest_core::blok::infer::{resolve_scope, ArgPorts};
use rekuest_core::blok::{blok_view, check_blok_tree, default_view, BlokScope, BlokView};
use rekuest_core::bsx::{parse_bsx, parse_bsx_default, MAX_SOURCE_BYTES};
use rekuest_core::inputs::{
    AgentDependencyInputModel, BlokLocalInputModel, BlokQueryInputModel,
    BlokSubscriptionInputModel, CatalogComponentInputModel, CatalogOperationInputModel,
    ComponentNodeInputModel,
};
use serde::de::DeserializeOwned;
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

fn corpus() -> Value {
    serde_json::from_str(include_str!("fixtures/bsx_v2.json")).expect("the corpus is JSON")
}

fn cases() -> Vec<Value> {
    corpus()["cases"].as_array().expect("cases").clone()
}

fn typed<T: DeserializeOwned>(value: &Value) -> T {
    serde_json::from_value(value.clone()).unwrap_or_else(|e| panic!("{e}: {value}"))
}

fn tree_json(tree: &ComponentNodeInputModel) -> Value {
    without_nulls(serde_json::to_value(tree).expect("a tree serializes"))
}

#[test]
fn every_source_parses_as_the_spec_parses_it() {
    let mut differences = vec![];
    let mut count = 0;
    for case in cases().iter().filter(|c| c.get("tree").is_some()) {
        count += 1;
        let source = case["source"].as_str().expect("a source");
        match parse_bsx_default(source) {
            Ok(tree) if tree_json(&tree) == case["tree"] => {}
            Ok(tree) => differences.push(format!(
                "{}: {source:?}\n  takt: {}\n  spec: {}",
                case["name"],
                tree_json(&tree),
                case["tree"]
            )),
            Err(error) => differences.push(format!(
                "{}: {source:?}\n  takt refused: {error}",
                case["name"]
            )),
        }
    }
    assert!(count > 100, "the corpus has its parsed and checked cases");
    assert!(
        differences.is_empty(),
        "{} of {count} cases differ:\n{}",
        differences.len(),
        differences.join("\n")
    );
}

#[test]
fn every_refused_source_is_refused_with_the_spec_code_and_place() {
    let mut differences = vec![];
    let mut count = 0;
    for case in cases().iter().filter(|c| c.get("error").is_some()) {
        count += 1;
        let source = case["source"].as_str().expect("a source");
        let expected = &case["error"];
        match parse_bsx_default(source) {
            Ok(tree) => differences.push(format!(
                "{}: {source:?} parsed: {}",
                case["name"],
                tree_json(&tree)
            )),
            Err(error) => {
                let got = serde_json::json!({"code": error.code, "line": error.line, "column": error.column});
                if &got != expected {
                    differences.push(format!(
                        "{}: {source:?}\n  takt: {got} ({error})\n  spec: {expected}",
                        case["name"]
                    ));
                }
            }
        }
    }
    assert!(count >= 50, "the corpus has its refused cases");
    assert!(
        differences.is_empty(),
        "{} of {count} cases differ:\n{}",
        differences.len(),
        differences.join("\n")
    );
}

fn scope_of(scope: &Value) -> BlokScope {
    let list = |key: &str| scope[key].as_array().cloned().unwrap_or_default();
    BlokScope {
        kind: scope["kind"].as_str().expect("a kind").to_owned(),
        context: scope["context"].as_str().expect("a context").to_owned(),
        dependencies: list("dependencies")
            .iter()
            .map(typed::<AgentDependencyInputModel>)
            .collect(),
        locals: list("locals")
            .iter()
            .map(typed::<BlokLocalInputModel>)
            .collect(),
        queries: list("queries")
            .iter()
            .map(typed::<BlokQueryInputModel>)
            .collect(),
        subscriptions: list("subscriptions")
            .iter()
            .map(typed::<BlokSubscriptionInputModel>)
            .collect(),
        args: scope["args"].as_array().map(|args| {
            args.iter()
                .map(|a| a.as_str().expect("an arg key").to_owned())
                .collect()
        }),
        catalogs: list("catalogs")
            .iter()
            .map(|c| c.as_str().expect("a catalog").to_owned())
            .collect(),
        demo: None,
        assets: None,
    }
}

/// The view a case is checked against: none without `catalog`, else base, the standard
/// catalogs, the Orkestrator's module and the case's `module` (`blok_view([orkestrator, module])`).
fn view_of(scope: &Value) -> Option<BlokView> {
    if scope["catalog"] != Value::Bool(true) {
        return None;
    }
    let module = &scope["module"];
    if module.is_null() {
        return Some(default_view().clone());
    }
    let name = module["name"].as_str().expect("a module name");
    let components: Vec<CatalogComponentInputModel> = typed(&module["components"]);
    let operations: Vec<CatalogOperationInputModel> = if module["operations"].is_null() {
        vec![]
    } else {
        typed(&module["operations"])
    };
    Some(blok_view(Some((name, &components)), &operations, true))
}

/// A list of models as the spec dumps them (`exclude_none`): read and written again, so the
/// defaults each side fills in compare equal.
fn normalized<T: DeserializeOwned + serde::Serialize>(values: &Value) -> Value {
    let models: Vec<T> = typed(values);
    without_nulls(serde_json::to_value(models).expect("models serialize"))
}

#[test]
fn every_checked_source_resolves_its_scope_as_the_spec_does() {
    let mut differences = vec![];
    let mut count = 0;
    for case in cases().iter().filter(|c| c.get("resolved").is_some()) {
        count += 1;
        let tree: ComponentNodeInputModel = typed(&case["tree"]);
        let scope = scope_of(&case["scope"]);
        let view = view_of(&case["scope"]);
        let resolved = resolve_scope(&tree, &scope, view.as_ref(), &ArgPorts::new());
        let got = serde_json::json!({
            "locals": without_nulls(serde_json::to_value(&resolved.locals).expect("locals")),
        });
        let expected = serde_json::json!({
            "locals": normalized::<BlokLocalInputModel>(&case["resolved"]["locals"]),
        });
        if got != expected {
            differences.push(format!(
                "{}: {}\n  takt: {got}\n  spec: {expected}",
                case["name"], case["source"]
            ));
        }
        let again = resolve_scope(&tree, &resolved, view.as_ref(), &ArgPorts::new());
        if serde_json::to_value(&again.locals).ok() != serde_json::to_value(&resolved.locals).ok() {
            differences.push(format!("{}: resolving twice changes it", case["name"]));
        }
    }
    assert!(count >= 100, "the corpus has its resolved cases");
    assert!(
        differences.is_empty(),
        "{} of {count} cases differ:\n{}",
        differences.len(),
        differences.join("\n")
    );
}

#[test]
fn every_checked_source_finds_the_codes_the_spec_finds() {
    let mut differences = vec![];
    let mut count = 0;
    for case in cases().iter().filter(|c| c.get("scope").is_some()) {
        count += 1;
        let tree: ComponentNodeInputModel = typed(&case["tree"]);
        let scope = scope_of(&case["scope"]);
        let view = view_of(&case["scope"]);
        let codes: Vec<String> = check_blok_tree(&tree, &scope, view.as_ref())
            .into_iter()
            .map(|f| f.code)
            .collect();
        let expected: Vec<String> = typed(&case["codes"]);
        if codes != expected {
            differences.push(format!(
                "{}: {}\n  takt: {codes:?}\n  spec: {expected:?}",
                case["name"], case["source"]
            ));
        }
    }
    assert!(count >= 50, "the corpus has its checked cases");
    assert!(
        differences.is_empty(),
        "{} of {count} cases differ:\n{}",
        differences.len(),
        differences.join("\n")
    );
}

#[test]
fn the_vendored_catalogs_place_text_as_the_corpus_was_parsed_with() {
    let corpus = corpus();
    let table = corpus["text_table"].as_object().expect("a text table");
    let view = default_view();
    let mut differences = vec![];
    for (name, expected) in table {
        let Some(spec) = view.component(name) else {
            differences.push(format!("{name}: not in takt's view"));
            continue;
        };
        let got = serde_json::json!({
            "text_prop": spec.text_prop,
            "context": spec.context,
            "children_context": spec.children_context,
            "inherits_context": spec.inherits_context,
            "accepts_children": spec.accepts_children,
            "catalog": spec.catalog,
        });
        if &got != expected {
            differences.push(format!("{name}\n  takt: {got}\n  spec: {expected}"));
        }
    }
    let known: HashSet<&String> = table.keys().collect();
    for name in view.components.keys().filter(|name| !known.contains(name)) {
        differences.push(format!("{name}: in takt's view, not in the corpus"));
    }
    assert!(differences.is_empty(), "{}", differences.join("\n"));
    assert_eq!(corpus["vocab"], 2);
}

#[test]
fn a_parse_error_says_where_in_code_points() {
    let error = parse_bsx_default("<Card>\n  <Text>é {a +}</Text>\n</Card>").unwrap_err();
    assert_eq!(
        (error.code.as_str(), error.line, error.column),
        ("expr.arithmetic", Some(2), Some(14)),
        "{error}"
    );
    assert!(
        error.to_string().starts_with("line 2, column 14: "),
        "{error}"
    );
}

#[test]
fn a_source_larger_than_a_blok_is_refused_unread() {
    let source = format!("<Text text=\"{}\" />", "x".repeat(MAX_SOURCE_BYTES));
    let error = parse_bsx(&source, default_view()).unwrap_err();
    assert_eq!(error.code, "bsx.syntax");
    assert!(error.message.contains("at most"), "{error}");
}

#[test]
fn a_finding_is_json_as_the_spec_writes_it() {
    let finding = rekuest_core::blok::Finding::of(
        "roots.reserved",
        Some("locals.self"),
        &[("name", Value::from("self"))],
    );
    assert_eq!(
        finding.to_json(),
        serde_json::json!({"code": "roots.reserved", "level": "error", "message": "'self' is reserved: no local, let or into may use it.", "path": "locals.self"})
    );
}

#[test]
fn the_deps_unknown_hint_says_what_the_kind_does() {
    let tree = parse_bsx_default("<Html><Text text={deps.laser.power} /></Html>").expect("parses");
    for (kind, hint) in [
        (
            "custom",
            r#"dependencies=[AgentDependencyInput(key="laser")]"#,
        ),
        (
            "task",
            r#"dependencies=[AgentDependencyInput(key="laser")]"#,
        ),
        ("action", "add a dependency parameter `laser` to the action"),
    ] {
        let scope = BlokScope {
            kind: kind.into(),
            ..BlokScope::default()
        };
        let findings = check_blok_tree(&tree, &scope, None);
        let codes: Vec<&str> = findings.iter().map(|f| f.code.as_str()).collect();
        assert_eq!(codes, ["deps.unknown"], "{kind}");
        assert!(
            findings[0].message.ends_with(&format!("Fix: {hint}.")),
            "{kind}: {}",
            findings[0].message
        );
    }
    // An agent blok has no `deps` root: the kind refuses it before any key is looked up.
    let scope = BlokScope {
        kind: "agent".into(),
        ..BlokScope::default()
    };
    let codes: Vec<String> = check_blok_tree(&tree, &scope, None)
        .into_iter()
        .map(|f| f.code)
        .collect();
    assert_eq!(codes, ["roots.not_in_kind"]);
    assert_eq!(
        rekuest_core::blok::check::dependency_hint("laser", "agent"),
        "agent bloks read only `self`"
    );
}
