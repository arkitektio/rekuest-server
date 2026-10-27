//! What a v2 blok declares beside its tree, and its tree against its kind's roots
//! (`BlokImplementationInputModel::validate`, `ActionBlokInputModel` via `check_action_blok`).

use rekuest_core::inputs::{check_action_blok, ActionBlokInputModel, BlokImplementationInputModel};
use serde_json::{json, Value};

fn blok(fields: Value) -> Result<BlokImplementationInputModel, String> {
    let mut model: BlokImplementationInputModel =
        serde_json::from_value(fields).map_err(|e| e.to_string())?;
    model.validate().map_err(|e| e.0)?;
    Ok(model)
}

#[test]
fn a_blok_says_which_language_it_speaks() {
    let error = blok(json!({"key": "panel", "source": "<Html><Card /></Html>"})).unwrap_err();
    assert!(error.contains("vocab"), "{error}");
    let error =
        blok(json!({"key": "panel", "vocab": 1, "source": "<Html><Card /></Html>"})).unwrap_err();
    assert!(error.contains("vocab 1"), "{error}");
}

#[test]
fn a_source_is_parsed_into_one_root() {
    let model =
        blok(json!({"key": "panel", "vocab": 2, "source": "<Html><Card><Button>Go</Button></Card></Html>"}))
            .unwrap();
    let components = model.components.unwrap();
    assert_eq!(components.len(), 1);
    assert_eq!(components[0].id, "Html[0]");
    assert_eq!(
        components[0].children.as_ref().unwrap()[0].id,
        "Html[0]/Card[0]"
    );
    assert!(model.source.is_none());
}

#[test]
fn an_agent_blok_reads_self_not_deps() {
    let error = blok(
        json!({"key": "panel", "vocab": 2, "source": "<Html><Text text={deps.robot.arm} /></Html>",
        "dependencies": [{"key": "robot"}]}),
    )
    .unwrap_err();
    assert!(error.contains("[roots.not_in_kind]"), "{error}");
}

#[test]
fn a_local_is_no_reserved_root_and_a_query_no_mutation() {
    let error = blok(
        json!({"key": "panel", "vocab": 2, "source": "<Html><Card /></Html>", "locals": [{"key": "task", "kind": "STRING"}]}),
    )
    .unwrap_err();
    assert!(error.contains("[roots.reserved]"), "{error}");
    let error = blok(
        json!({"key": "panel", "vocab": 2, "source": "<Html><Card /></Html>",
        "queries": [{"key": "q", "service": "mikro", "document": "mutation { a }"}]}),
    )
    .unwrap_err();
    assert!(error.contains("[query.read_only]"), "{error}");
}

#[test]
fn a_query_fetches_more_only_into_a_field_path_it_declares() {
    let query = |more: &str| json!([{"key": "q", "service": "mikro", "document": "query { a }", "more": more}]);
    let error = blok(
        json!({"key": "panel", "vocab": 2, "source": "<Html><Card /></Html>",
        "queries": query("images[0]")}),
    )
    .unwrap_err();
    assert!(error.contains("[query.more]"), "{error}");
    let source = "<Html><Button onClick={queries.q.fetch_more(after: 1)}>More</Button></Html>";
    let error = blok(json!({"key": "panel", "vocab": 2, "source": source,
        "queries": [{"key": "q", "service": "mikro", "document": "query { a }"}]}))
    .unwrap_err();
    assert!(error.contains("[query.fetch_more]"), "{error}");
    let model = blok(json!({"key": "panel", "vocab": 2, "source": source,
        "queries": query("images.items")}))
    .unwrap();
    assert_eq!(model.queries[0].more.as_deref(), Some("images.items"));
}

#[test]
fn a_local_is_a_port_whose_default_is_the_checkers() {
    let error = blok(
        json!({"key": "panel", "vocab": 2, "source": "<Html><Card /></Html>",
        "locals": [{"key": "n", "kind": "INT", "default": "15"}]}),
    )
    .unwrap_err();
    assert!(error.contains("[local.default]"), "{error}");
    // A local's kind may be left out: it is inferred (here from its default), never refused.
    let model = blok(
        json!({"key": "panel", "vocab": 2, "source": "<Html><Card /></Html>",
        "locals": [{"key": "n", "default": 15}]}),
    )
    .unwrap();
    assert_eq!(
        model.locals[0].kind, None,
        "left out stays left out until resolved"
    );
    let error = blok(
        json!({"key": "panel", "vocab": 2, "source": "<Html><Card /></Html>",
        "catalogs": ["ui"]}),
    )
    .unwrap_err();
    assert!(error.contains("[manifest.catalog]"), "{error}");
}

#[test]
fn an_enum_local_needs_its_choices() {
    let error = blok(
        json!({"key": "panel", "vocab": 2, "source": "<Html><Card /></Html>",
        "locals": [{"key": "mode", "kind": "ENUM"}]}),
    )
    .unwrap_err();
    assert!(error.contains("must declare choices"), "{error}");
    blok(json!({"key": "panel", "vocab": 2, "source": "<Html><Card /></Html>",
        "locals": [{"key": "mode", "kind": "ENUM", "choices": [{"value": "fast", "label": "Fast"}]}]}))
    .unwrap();
}

#[test]
fn an_action_logo_declares_nothing() {
    let error = blok(json!({"key": "logo", "vocab": 2, "context": "ACTION_LOGO",
        "source": "<ActionLogo><Path d=\"M0 0\" fill=\"brand\" /></ActionLogo>", "locals": [{"key": "x", "kind": "STRING"}]}))
    .unwrap_err();
    assert!(error.contains("[manifest.action_logo]"), "{error}");
}

#[test]
fn an_action_blok_anchors_only_at_its_dependencies() {
    let mut model: ActionBlokInputModel =
        serde_json::from_value(json!({"context": "OVERLAY", "vocab": 2,
        "source": "<Overlay><Anchor dep=\"robot\" /><Anchor dep=\"crane\" /></Overlay>"}))
        .unwrap();
    model.validate().unwrap();
    let dependencies: Vec<rekuest_core::inputs::AgentDependencyInputModel> =
        serde_json::from_value(json!([{"key": "robot"}])).unwrap();
    let error = check_action_blok(&model, &dependencies, None).unwrap_err();
    assert!(error.0.contains("'crane'"), "{error}");
}
