//! What a v2 blok says of itself, and its tree where it is mounted
//! (`BlokImplementationInputModel::validate`, `ActionBlokInputModel` via `check_action_blok`):
//! one `<Blok kind context>` declaring its locals, queries and subscriptions inside.

use rekuest_core::inputs::{check_action_blok, ActionBlokInputModel, BlokImplementationInputModel};
use serde_json::{json, Value};

fn blok(fields: Value) -> Result<BlokImplementationInputModel, String> {
    let mut model: BlokImplementationInputModel =
        serde_json::from_value(fields).map_err(|e| e.to_string())?;
    model.validate().map_err(|e| e.0)?;
    Ok(model)
}

/// An agent's panel blok holding `inner`.
fn panel(inner: &str) -> Value {
    json!({"key": "panel", "vocab": 2,
        "source": format!("<Blok kind=\"agent\" context=\"panel\">{inner}</Blok>")})
}

#[test]
fn a_blok_says_which_language_it_speaks() {
    let source = "<Blok kind=\"agent\" context=\"panel\"><Card /></Blok>";
    let error = blok(json!({"key": "panel", "source": source})).unwrap_err();
    assert!(error.contains("vocab"), "{error}");
    let error = blok(json!({"key": "panel", "vocab": 1, "source": source})).unwrap_err();
    assert!(error.contains("vocab 1"), "{error}");
}

#[test]
fn a_source_is_parsed_into_one_blok() {
    let model = blok(panel("<Card><Button>Go</Button></Card>")).unwrap();
    let components = model.components.unwrap();
    assert_eq!(components.len(), 1);
    assert_eq!(components[0].id, "Blok[0]");
    assert_eq!(
        components[0].children.as_ref().unwrap()[0].id,
        "Blok[0]/Card[0]"
    );
    assert!(model.source.is_none());
}

#[test]
fn a_blok_is_a_blok_element_saying_the_context_it_is_sent_as() {
    let error =
        blok(json!({"key": "panel", "vocab": 2, "source": "<Html><Card /></Html>"})).unwrap_err();
    assert!(error.contains("[blok.root]"), "{error}");
    let error = blok(json!({"key": "panel", "vocab": 2, "context": "SCENE",
        "source": "<Blok kind=\"agent\" context=\"panel\"><Card /></Blok>"}))
    .unwrap_err();
    assert_eq!(
        error,
        "Blok manifest: sent as a SCENE blok, but its <Blok> says PANEL"
    );
    let error = blok(json!({"key": "panel", "vocab": 2,
        "source": "<Blok kind=\"custom\" context=\"panel\"><Card /></Blok>"}))
    .unwrap_err();
    assert!(error.contains("[kind.placement]"), "{error}");
}

#[test]
fn an_agent_blok_reads_self_not_deps() {
    let mut fields = panel("<Text text={deps.robot.arm} />");
    fields["dependencies"] = json!([{"key": "robot"}]);
    let error = blok(fields).unwrap_err();
    assert!(error.contains("[roots.not_in_kind]"), "{error}");
}

#[test]
fn a_local_is_no_reserved_root_and_a_query_no_mutation() {
    let error = blok(panel("<local name=\"task\" kind=\"STRING\" /><Card />")).unwrap_err();
    assert!(error.contains("[roots.reserved]"), "{error}");
    let error = blok(panel(
        "<query name=\"q\" service=\"mikro\" document=\"mutation { a }\" /><Card />",
    ))
    .unwrap_err();
    assert!(error.contains("[query.read_only]"), "{error}");
}

#[test]
fn a_query_fetches_more_only_into_a_field_path_it_declares() {
    let query = |more: &str| {
        format!("<query name=\"q\" service=\"mikro\" document=\"query {{ a }}\" more=\"{more}\" />")
    };
    let error = blok(panel(&format!("{}<Card />", query("images[0]")))).unwrap_err();
    assert!(error.contains("[query.more]"), "{error}");
    let button = "<Button onClick={queries.q.fetch_more(after: 1)}>More</Button>";
    let error = blok(panel(&format!(
        "<query name=\"q\" service=\"mikro\" document=\"query {{ a }}\" />{button}"
    )))
    .unwrap_err();
    assert!(error.contains("[query.fetch_more]"), "{error}");
    blok(panel(&format!("{}{button}", query("images.items")))).unwrap();
}

#[test]
fn a_query_is_a_child_of_the_blok() {
    let error = blok(panel(
        "<Card><query name=\"q\" service=\"mikro\" document=\"query { a }\" /></Card>",
    ))
    .unwrap_err();
    assert!(error.contains("[query.position]"), "{error}");
}

#[test]
fn a_local_is_a_port_whose_default_is_the_checkers() {
    let error = blok(panel(
        "<local name=\"n\" kind=\"INT\" default=\"15\" /><Card />",
    ))
    .unwrap_err();
    assert!(error.contains("[local.default]"), "{error}");
    // A local's kind may be left out: it is inferred (here from its default), never refused.
    blok(panel("<local name=\"n\" default={15} /><Text>{n}</Text>")).unwrap();
    let mut fields = panel("<Card />");
    fields["catalogs"] = json!(["ui"]);
    let error = blok(fields).unwrap_err();
    assert!(error.contains("[manifest.catalog]"), "{error}");
}

#[test]
fn a_local_comes_first_and_is_in_scope_in_its_parents_subtree() {
    let error = blok(panel("<Card /><local name=\"n\" default={1} />")).unwrap_err();
    assert!(error.contains("[local.position]"), "{error}");
    let error = blok(panel(
        "<Card><local name=\"open\" default={false} /></Card><Text>{open}</Text>",
    ))
    .unwrap_err();
    assert!(error.contains("[roots.unknown]"), "{error}");
    blok(panel(
        "<Card><local name=\"open\" default={false} /><Card><Text>{open}</Text></Card></Card>",
    ))
    .unwrap();
}

#[test]
fn an_enum_local_needs_its_choices() {
    let error = blok(panel("<local name=\"mode\" kind=\"ENUM\" /><Card />")).unwrap_err();
    assert!(error.contains("[local.attribute]"), "{error}");
    assert!(error.contains("must declare choices"), "{error}");
    blok(panel(
        "<local name=\"mode\" kind=\"ENUM\" choices={[{value: \"fast\", label: \"Fast\"}]} /><Card />",
    ))
    .unwrap();
}

#[test]
fn an_action_logo_reads_only_its_task() {
    let logo = |inner: &str| {
        json!({"key": "logo", "vocab": 2, "context": "LOGO",
            "source": format!("<Blok kind=\"action\" context=\"logo\">{inner}<Logo><Path d=\"M0 0\" fill=\"brand\" /></Logo></Blok>")})
    };
    blok(logo("")).unwrap();
    let error = blok(logo("<local name=\"x\" kind=\"STRING\" />")).unwrap_err();
    assert!(error.contains("[roots.not_in_kind]"), "{error}");
    let mut fields = logo("");
    fields["dependencies"] = json!([{"key": "self"}]);
    let error = blok(fields).unwrap_err();
    assert!(error.contains("[manifest.action_logo]"), "{error}");
    // An action's logo is declared by the agent only in a logo.
    let error = blok(json!({"key": "panel", "vocab": 2,
        "source": "<Blok kind=\"action\" context=\"panel\"><Card /></Blok>"}))
    .unwrap_err();
    assert!(error.contains("[kind.placement]"), "{error}");
}

#[test]
fn an_action_blok_places_only_its_dependencies_and_says_its_phase() {
    let action = |phase: &str, kind: &str| {
        let mut model: ActionBlokInputModel = serde_json::from_value(json!({"context": "SCENE", "vocab": 2, "phase": phase,
            "source": format!("<Blok kind=\"{kind}\" context=\"scene\"><Space room=\"lab\"><Placed dep=\"robot\" /><Placed dep=\"crane\" /></Space></Blok>")}))
        .unwrap();
        model.validate().unwrap();
        model
    };
    let dependencies: Vec<rekuest_core::inputs::AgentDependencyInputModel> =
        serde_json::from_value(json!([{"key": "robot"}])).unwrap();
    let error = check_action_blok(&action("RUN", "action"), &dependencies, None).unwrap_err();
    assert!(error.0.contains("'crane'"), "{error}");
    let error = check_action_blok(&action("ASSIGN", "action"), &dependencies, None).unwrap_err();
    assert!(error.0.contains("[kind.placement]"), "{error}");
}
