//! Ports and what hangs off them: effects, validators, widgets, descriptors
//! (`rekuest_core/inputs/models.py`, the port half).

use std::collections::HashSet;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::calls::{
    check_pure_call, check_widget_props, ComponentPropInputModel, UtilCallInputModel, ITEM_KEY,
    PORT_PATH_SEPARATOR,
};
use super::{min_length, ValidationError};
use crate::enums::{DescriptorOperator, EffectKind, OptionKey, PortKind};
use crate::pyjson::{repr, repr_str};
use crate::values::{value_mismatch, PortLike};

/// A port effect: a pure call deciding whether it applies (`EffectInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EffectInputModel {
    pub call: UtilCallInputModel,
    #[serde(default = "empty_list")]
    pub dependencies: Option<Vec<String>>,
    #[serde(default)]
    pub message: Option<String>,
    pub kind: EffectKind,
    /// Whether `fade` was given: only HIDE may set it (`model_fields_set`).
    #[serde(default = "default_fade", deserialize_with = "track_fade")]
    pub fade: Fade,
    #[serde(default)]
    pub source: Option<String>,
}

/// `fade: bool = True`, remembering whether it was set explicitly.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Fade {
    pub value: bool,
    pub set: bool,
}

fn default_fade() -> Fade {
    Fade {
        value: true,
        set: false,
    }
}

fn track_fade<'de, D: serde::Deserializer<'de>>(d: D) -> Result<Fade, D::Error> {
    Ok(Fade {
        value: bool::deserialize(d)?,
        set: true,
    })
}

impl Serialize for Fade {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        self.value.serialize(s)
    }
}

fn empty_list() -> Option<Vec<String>> {
    Some(vec![])
}

impl EffectInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        self.call.validate()?;
        check_pure_call(
            &self.call,
            self.dependencies.as_deref(),
            &format!("Effect {} ({})", self.kind, self.call.operation),
            &[],
        )?;
        if self.kind == EffectKind::MESSAGE && self.message.as_deref().is_none_or(str::is_empty) {
            return Err(ValidationError("MESSAGE effect requires a message".into()));
        }
        if self.kind != EffectKind::HIDE && self.fade.set {
            return Err(ValidationError(format!(
                "{} effect must not set fade (HIDE only)",
                self.kind
            )));
        }
        Ok(())
    }
}

/// A value a port accepts (`ChoiceInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChoiceInputModel {
    pub value: Value,
    pub label: String,
    #[serde(default)]
    pub image: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
}

/// A pure call validating the port value (`ValidatorInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ValidatorInputModel {
    pub call: UtilCallInputModel,
    #[serde(default = "empty_list")]
    pub dependencies: Option<Vec<String>>,
    #[serde(default)]
    pub label: Option<String>,
    #[serde(default)]
    pub error_message: Option<String>,
    #[serde(default)]
    pub source: Option<String>,
}

impl ValidatorInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        self.call.validate()?;
        let owner = format!(
            "Validator {}",
            self.label
                .clone()
                .filter(|l| !l.is_empty())
                .unwrap_or_else(|| self.call.operation.clone())
        );
        check_pure_call(&self.call, self.dependencies.as_deref(), &owner, &[])
    }
}

/// How a state-choice widget reads one part of a state entry (`StateAccessorInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StateAccessorInputModel {
    pub option_key: OptionKey,
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default)]
    pub call: Option<UtilCallInputModel>,
}

impl StateAccessorInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        if let Some(call) = self.call.as_mut() {
            call.validate()?;
        }
        if self.path.is_some() && self.call.is_some() {
            return Err(ValidationError(
                "StateAccessor: set either path or call, not both".into(),
            ));
        }
        Ok(())
    }
}

/// An assign widget, discriminated by `kind` (`AssignWidgetInputModel`). Each member forbids the
/// fields of the others.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind")]
pub enum AssignWidgetInputModel {
    #[serde(rename = "SLIDER")]
    Slider(SliderWidget),
    #[serde(rename = "CHOICE")]
    Choice(ChoiceWidget),
    #[serde(rename = "STRING")]
    String(StringWidget),
    #[serde(rename = "SEARCH")]
    Search(SearchWidget),
    #[serde(rename = "CUSTOM")]
    Custom(CustomWidget),
    #[serde(rename = "STATE_CHOICE")]
    StateChoice(StateChoiceWidget),
    #[serde(rename = "PROXY")]
    Proxy(ProxyWidget),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SliderWidget {
    #[serde(default)]
    pub follow_value: Option<String>,
    #[serde(default)]
    pub min: Option<f64>,
    #[serde(default)]
    pub max: Option<f64>,
    #[serde(default)]
    pub step: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChoiceWidget {
    #[serde(default)]
    pub follow_value: Option<String>,
    #[serde(default)]
    pub placeholder: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StringWidget {
    #[serde(default)]
    pub follow_value: Option<String>,
    #[serde(default)]
    pub placeholder: Option<String>,
    #[serde(default)]
    pub as_paragraph: Option<bool>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SearchWidget {
    #[serde(default)]
    pub follow_value: Option<String>,
    pub query: String,
    pub ward: String,
    #[serde(default)]
    pub filters: Option<Vec<ArgPortInputModel>>,
    #[serde(default = "empty_list")]
    pub dependencies: Option<Vec<String>>,
    #[serde(default)]
    pub placeholder: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CustomWidget {
    #[serde(default)]
    pub follow_value: Option<String>,
    pub component: String,
    #[serde(default)]
    pub props: Option<Vec<ComponentPropInputModel>>,
    #[serde(default = "empty_list")]
    pub dependencies: Option<Vec<String>>,
    #[serde(default)]
    pub fallback: Option<Box<AssignWidgetInputModel>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StateChoiceWidget {
    #[serde(default)]
    pub follow_value: Option<String>,
    #[serde(default)]
    pub dependency: Option<String>,
    #[serde(default)]
    pub state_path: Option<String>,
    #[serde(default)]
    pub state_call: Option<UtilCallInputModel>,
    #[serde(default)]
    pub state_accessors: Option<Vec<StateAccessorInputModel>>,
    #[serde(default = "empty_list")]
    pub dependencies: Option<Vec<String>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProxyWidget {
    #[serde(default)]
    pub follow_value: Option<String>,
    pub target_port: String,
    pub target_action: String,
    #[serde(default)]
    pub target_dependency: Option<String>,
}

/// The variables every SEARCH widget query declares.
pub const SEARCH_QUERY_VARIABLES: [&str; 2] = ["search", "values"];

/// The variables of a query's single `query` operation, or why the query is not one.
fn search_query_variables(query: &str) -> Result<HashSet<String>, ValidationError> {
    use graphql_parser::query::{parse_query, Definition, OperationDefinition};
    let document = parse_query::<String>(query)
        .map_err(|e| ValidationError(format!("SEARCH widget query does not parse: {e}")))?;
    let operations: Vec<&OperationDefinition<String>> = document
        .definitions
        .iter()
        .filter_map(|definition| match definition {
            Definition::Operation(operation) => Some(operation),
            Definition::Fragment(_) => None,
        })
        .collect();
    match operations.as_slice() {
        [OperationDefinition::Query(query)] => Ok(query
            .variable_definitions
            .iter()
            .map(|v| v.name.clone())
            .collect()),
        // An anonymous `{ … }` is a query with no variables.
        [OperationDefinition::SelectionSet(_)] => Ok(HashSet::new()),
        _ => Err(ValidationError(
            "SEARCH widget query must contain exactly one `query` operation".into(),
        )),
    }
}

fn py_list(items: &[String]) -> String {
    format!(
        "[{}]",
        items
            .iter()
            .map(|i| repr_str(i))
            .collect::<Vec<_>>()
            .join(", ")
    )
}

impl AssignWidgetInputModel {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Slider(_) => "SLIDER",
            Self::Choice(_) => "CHOICE",
            Self::String(_) => "STRING",
            Self::Search(_) => "SEARCH",
            Self::Custom(_) => "CUSTOM",
            Self::StateChoice(_) => "STATE_CHOICE",
            Self::Proxy(_) => "PROXY",
        }
    }

    pub fn follow_value(&self) -> Option<&str> {
        match self {
            Self::Slider(w) => w.follow_value.as_deref(),
            Self::Choice(w) => w.follow_value.as_deref(),
            Self::String(w) => w.follow_value.as_deref(),
            Self::Search(w) => w.follow_value.as_deref(),
            Self::Custom(w) => w.follow_value.as_deref(),
            Self::StateChoice(w) => w.follow_value.as_deref(),
            Self::Proxy(w) => w.follow_value.as_deref(),
        }
    }

    pub fn dependencies(&self) -> Option<&[String]> {
        match self {
            Self::Search(w) => w.dependencies.as_deref(),
            Self::Custom(w) => w.dependencies.as_deref(),
            Self::StateChoice(w) => w.dependencies.as_deref(),
            _ => None,
        }
    }

    /// This widget, then its fallbacks (`iter_widget_chain`).
    pub fn chain(&self) -> Vec<&AssignWidgetInputModel> {
        let mut out = vec![self];
        let mut current = self;
        while let Self::Custom(CustomWidget {
            fallback: Some(next),
            ..
        }) = current
        {
            out.push(next);
            current = next;
        }
        out
    }

    pub fn validate(&mut self) -> Result<(), ValidationError> {
        match self {
            Self::Slider(w) => {
                if let (Some(min), Some(max)) = (w.min, w.max) {
                    if min >= max {
                        return Err(ValidationError(format!(
                            "SLIDER widget: min ({}) must be smaller than max ({})",
                            crate::pyjson::float_repr(min),
                            crate::pyjson::float_repr(max)
                        )));
                    }
                }
                if let Some(step) = w.step {
                    if step <= 0.0 {
                        return Err(ValidationError(format!(
                            "SLIDER widget: step must be positive, got {}",
                            crate::pyjson::float_repr(step)
                        )));
                    }
                }
            }
            Self::Search(w) => {
                for filter in w.filters.iter_mut().flatten() {
                    filter.validate()?;
                }
                min_length(&w.ward, "ward")?;
                let declared = search_query_variables(&w.query)?;
                let mut missing: Vec<String> = SEARCH_QUERY_VARIABLES
                    .iter()
                    .filter(|v| !declared.contains(**v))
                    .map(|v| format!("${v}"))
                    .collect();
                missing.sort();
                if !missing.is_empty() {
                    return Err(ValidationError(format!(
                        "SEARCH widget query must declare the variables {}",
                        py_list(&missing)
                    )));
                }
                let keys: Vec<&str> = w.filters.iter().flatten().map(|p| p.key.as_str()).collect();
                let mut duplicates: Vec<String> = keys
                    .iter()
                    .filter(|k| keys.iter().filter(|o| o == k).count() > 1)
                    .map(|k| (*k).to_owned())
                    .collect::<HashSet<_>>()
                    .into_iter()
                    .collect();
                duplicates.sort();
                if !duplicates.is_empty() {
                    return Err(ValidationError(format!(
                        "SEARCH widget filters have duplicate keys {}",
                        py_list(&duplicates)
                    )));
                }
                if keys.contains(&"value") {
                    return Err(ValidationError(
                        "SEARCH widget filters may not use the reserved key 'value'".into(),
                    ));
                }
                let mut undeclared: Vec<String> = keys
                    .iter()
                    .filter(|k| !declared.contains(**k))
                    .map(|k| format!("${k}"))
                    .collect::<HashSet<_>>()
                    .into_iter()
                    .collect();
                undeclared.sort();
                if !undeclared.is_empty() {
                    return Err(ValidationError(format!(
                        "SEARCH widget query must declare a variable for each filter port: missing {}",
                        py_list(&undeclared)
                    )));
                }
            }
            Self::Custom(w) => {
                for prop in w.props.iter_mut().flatten() {
                    prop.validate()?;
                }
                if let Some(fallback) = w.fallback.as_mut() {
                    fallback.validate()?;
                }
                min_length(&w.component, "component")?;
                check_widget_props(
                    w.props.as_deref(),
                    w.dependencies.as_deref(),
                    "CustomAssignWidget",
                )?;
            }
            Self::StateChoice(w) => {
                if let Some(call) = w.state_call.as_mut() {
                    call.validate()?;
                }
                for accessor in w.state_accessors.iter_mut().flatten() {
                    accessor.validate()?;
                }
                if w.state_path.is_none() == w.state_call.is_none() {
                    return Err(ValidationError(
                        "STATE_CHOICE widget needs exactly one of state_path or state_call".into(),
                    ));
                }
                if let Some(call) = &w.state_call {
                    check_pure_call(
                        call,
                        w.dependencies.as_deref(),
                        "StateChoice state_call",
                        &["state"],
                    )?;
                }
                for (index, accessor) in w.state_accessors.iter().flatten().enumerate() {
                    if let Some(call) = &accessor.call {
                        check_pure_call(
                            call,
                            w.dependencies.as_deref(),
                            &format!("StateAccessor {index}"),
                            &["state"],
                        )?;
                    }
                }
            }
            Self::Proxy(w) => {
                min_length(&w.target_port, "target_port")?;
                min_length(&w.target_action, "target_action")?;
            }
            Self::Choice(_) | Self::String(_) => {}
        }
        Ok(())
    }
}

/// A return widget, discriminated by `kind` (`ReturnWidgetInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind")]
pub enum ReturnWidgetInputModel {
    #[serde(rename = "CHOICE")]
    Choice(ChoiceReturnWidget),
    #[serde(rename = "CUSTOM")]
    Custom(CustomReturnWidget),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChoiceReturnWidget {}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CustomReturnWidget {
    pub component: String,
    #[serde(default)]
    pub props: Option<Vec<ComponentPropInputModel>>,
}

impl ReturnWidgetInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        if let Self::Custom(w) = self {
            for prop in w.props.iter_mut().flatten() {
                prop.validate()?;
            }
            min_length(&w.component, "component")?;
            check_widget_props(w.props.as_deref(), None, "CustomReturnWidget")?;
        }
        Ok(())
    }
}

/// A requires/provides descriptor (`RequiresInputModel` / `ProvidesInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DescriptorConstraint {
    pub key: String,
    pub operator: DescriptorOperator,
    #[serde(default)]
    pub value: Option<Value>,
}

impl DescriptorConstraint {
    /// Operator and value agree: IN/NOT_IN take a list, LTE/GTE a number, EXISTS no value or a
    /// boolean.
    pub fn validate(&self, owner: &str) -> Result<(), ValidationError> {
        min_length(&self.key, "key")?;
        let value = self.value.as_ref().unwrap_or(&Value::Null);
        let key = repr_str(&self.key);
        match self.operator {
            DescriptorOperator::IN | DescriptorOperator::NOT_IN if !value.is_array() => {
                Err(ValidationError(format!(
                    "{owner} {key}: {} needs a list value",
                    self.operator
                )))
            }
            DescriptorOperator::LTE | DescriptorOperator::GTE if !value.is_number() => {
                Err(ValidationError(format!(
                    "{owner} {key}: {} needs a numeric value",
                    self.operator
                )))
            }
            DescriptorOperator::EXISTS if !(value.is_null() || value.is_boolean()) => Err(
                ValidationError(format!("{owner} {key}: EXISTS takes no value or a boolean")),
            ),
            _ => Ok(()),
        }
    }
}

/// A structure identifier: `@package/key`.
pub fn is_identifier(identifier: &str) -> bool {
    let valid = |part: &str| {
        !part.is_empty()
            && part
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
    };
    identifier
        .strip_prefix('@')
        .and_then(|rest| rest.split_once('/'))
        .is_some_and(|(package, key)| valid(package) && valid(key))
}

/// Declares a port model: the fields every port has (`PortInputModel`), then the model's own.
/// Inlined rather than flattened, so `deny_unknown_fields` (pydantic's `extra="forbid"`) holds.
macro_rules! port_model {
    ($(#[$meta:meta])* $name:ident { $($(#[$fmeta:meta])* pub $field:ident: $ty:ty,)* }) => {
        $(#[$meta])*
        #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
        #[serde(deny_unknown_fields)]
        pub struct $name {
            pub key: String,
            #[serde(default)]
            pub label: Option<String>,
            pub kind: PortKind,
            #[serde(default)]
            pub description: Option<String>,
            #[serde(default)]
            pub identifier: Option<String>,
            #[serde(default)]
            pub nullable: bool,
            #[serde(default)]
            pub effects: Option<Vec<EffectInputModel>>,
            #[serde(default)]
            pub choices: Option<Vec<ChoiceInputModel>>,
            #[serde(default)]
            pub reference_unit: Option<String>,
            #[serde(default)]
            pub proposed_units: Option<Vec<String>>,
            #[serde(default)]
            pub dimension: Option<String>,
            $($(#[$fmeta])* pub $field: $ty,)*
        }

        impl $name {
            fn common(&mut self) -> PortCommon<'_> {
                PortCommon {
                    key: &self.key,
                    kind: self.kind,
                    identifier: self.identifier.as_deref(),
                    effects: &mut self.effects,
                    choices: self.choices.as_deref(),
                    reference_unit: self.reference_unit.as_deref(),
                    proposed_units: self.proposed_units.as_deref(),
                    dimension: &mut self.dimension,
                }
            }
        }

        impl PortLike for $name {
            fn key(&self) -> &str {
                &self.key
            }
            fn kind(&self) -> PortKind {
                self.kind
            }
            fn nullable(&self) -> bool {
                self.nullable
            }
            fn children(&self) -> &[Self] {
                self.children.as_deref().unwrap_or_default()
            }
            fn choice_values(&self) -> Vec<&Value> {
                self.choices.iter().flatten().map(|c| &c.value).collect()
            }
            fn identifier(&self) -> Option<&str> {
                self.identifier.as_deref()
            }
            fn default_value(&self) -> Option<&Value> {
                HasDefault::default_of(self)
            }
        }
    };
}

/// The declared default of a port: argument ports have one, return ports do not.
trait HasDefault {
    fn default_of(&self) -> Option<&Value>;
}

impl HasDefault for ArgPortInputModel {
    fn default_of(&self) -> Option<&Value> {
        self.default.as_ref()
    }
}

impl HasDefault for ReturnPortInputModel {
    fn default_of(&self) -> Option<&Value> {
        None
    }
}

/// The common fields of a port, as the shared checks see them.
struct PortCommon<'a> {
    key: &'a str,
    kind: PortKind,
    identifier: Option<&'a str>,
    effects: &'a mut Option<Vec<EffectInputModel>>,
    choices: Option<&'a [ChoiceInputModel]>,
    reference_unit: Option<&'a str>,
    proposed_units: Option<&'a [String]>,
    dimension: &'a mut Option<String>,
}

/// A key is non-empty, not `value`, and free of the separator (bar the item key).
fn check_port_key(key: &str, owner: &str) -> Result<(), ValidationError> {
    if key.is_empty() {
        return Err(ValidationError(format!(
            "{owner}: port key must not be empty"
        )));
    }
    if key == "value" {
        return Err(ValidationError(format!(
            "{owner}: {} is a reserved port key (it names the port's own value in calls)",
            repr_str(key)
        )));
    }
    if key.contains(PORT_PATH_SEPARATOR) && key != ITEM_KEY {
        return Err(ValidationError(format!(
            "{owner}: port key {} may not contain {}",
            repr_str(key),
            repr_str(PORT_PATH_SEPARATOR)
        )));
    }
    Ok(())
}

fn check_unique<'a>(
    values: impl Iterator<Item = &'a Value>,
    attr: &str,
    owner: &str,
) -> Result<(), ValidationError> {
    let mut seen: Vec<&Value> = vec![];
    for value in values {
        if seen.contains(&value) {
            return Err(ValidationError(format!(
                "{owner}: duplicate {attr} {}",
                repr(value)
            )));
        }
        seen.push(value);
    }
    Ok(())
}

pub fn check_unique_keys<'a>(
    keys: impl Iterator<Item = &'a str>,
    owner: &str,
) -> Result<(), ValidationError> {
    let mut seen = HashSet::new();
    for key in keys {
        if !seen.insert(key) {
            return Err(ValidationError(format!(
                "{owner}: duplicate key {}",
                repr_str(key)
            )));
        }
    }
    Ok(())
}

impl PortCommon<'_> {
    /// Key, the per-kind shape table (children, identifier, choices) and the QUANTITY rules
    /// (`check_kind_specific_fields`), with the port's children keys given.
    fn validate(self, child_keys: &[&str]) -> Result<(), ValidationError> {
        for effect in self.effects.iter_mut().flatten() {
            effect.validate()?;
        }
        check_port_key(self.key, "Port")?;

        let owner = format!("Port {} of kind {}", repr_str(self.key), self.kind);
        let bounds = match self.kind {
            PortKind::LIST => Some((1, Some(1))),
            PortKind::DICT => Some((1, None)),
            PortKind::UNION => Some((2, None)),
            PortKind::MODEL => Some((1, None)),
            _ => None,
        };
        match bounds {
            None if !child_keys.is_empty() => {
                return Err(ValidationError(format!("{owner} must not have children")));
            }
            None => {}
            Some((low, high)) => {
                let count = child_keys.len();
                if count < low || high.is_some_and(|h| count > h) {
                    let expected = if (low, high) == (1, Some(1)) {
                        "exactly one child".to_owned()
                    } else {
                        format!("at least {low} children")
                    };
                    return Err(ValidationError(format!(
                        "{owner} must have {expected}, got {count}"
                    )));
                }
                check_unique_keys(child_keys.iter().copied(), &format!("{owner} children"))?;
                if self.kind == PortKind::DICT && count > 1 && child_keys.contains(&ITEM_KEY) {
                    return Err(ValidationError(format!(
                        "{owner}: a DICT is either homogeneous (one child keyed {}) or has named children, not both",
                        repr_str(ITEM_KEY)
                    )));
                }
            }
        }

        let identified = matches!(
            self.kind,
            PortKind::STRUCTURE | PortKind::MEMORY_STRUCTURE | PortKind::INTERFACE
        );
        let identifiable = matches!(self.kind, PortKind::MODEL | PortKind::ENUM);
        match self.identifier {
            _ if identified && self.identifier.is_none_or(str::is_empty) => {
                return Err(ValidationError(format!(
                    "{owner} must declare an identifier (@package/key)"
                )));
            }
            Some(identifier) if !identified && !identifiable => {
                let _ = identifier;
                return Err(ValidationError(format!(
                    "{owner} must not declare an identifier"
                )));
            }
            Some(identifier) if !is_identifier(identifier) => {
                return Err(ValidationError(format!(
                    "{owner}: identifier {} is not of the form @package/key",
                    repr_str(identifier)
                )));
            }
            _ => {}
        }

        let choices = self.choices.unwrap_or_default();
        if self.kind == PortKind::ENUM && choices.is_empty() {
            return Err(ValidationError(format!("{owner} must declare choices")));
        }
        let choice_kinds = [
            PortKind::ENUM,
            PortKind::INT,
            PortKind::FLOAT,
            PortKind::STRING,
        ];
        if !choices.is_empty() && !choice_kinds.contains(&self.kind) {
            return Err(ValidationError(format!(
                "{owner} must not declare choices (only ['ENUM', 'FLOAT', 'INT', 'STRING'] may)"
            )));
        }
        if !choices.is_empty() {
            check_unique(
                choices.iter().map(|c| &c.value),
                "value",
                &format!("{owner} choices"),
            )?;
        }

        if self.kind == PortKind::QUANTITY {
            let Some(reference_unit) = self.reference_unit.filter(|u| !u.is_empty()) else {
                return Err(ValidationError(format!(
                    "QUANTITY port '{}' must declare a reference_unit",
                    self.key
                )));
            };
            let derived = crate::units::dimensionality_of(reference_unit)
                .map_err(|e| ValidationError(e.to_string()))?;
            if let Some(dimension) = self.dimension.as_deref() {
                let given = crate::units::dimensionality_of(dimension)
                    .map_err(|e| ValidationError(e.to_string()))?;
                if given != derived {
                    return Err(ValidationError(format!(
                        "QUANTITY port '{}': dimension '{dimension}' is inconsistent with reference_unit '{reference_unit}' (dimensionality '{derived}')",
                        self.key
                    )));
                }
            }
            for unit in self.proposed_units.unwrap_or_default() {
                let unit_dim = crate::units::dimensionality_of(unit)
                    .map_err(|e| ValidationError(e.to_string()))?;
                if unit_dim != derived {
                    return Err(ValidationError(format!(
                        "QUANTITY port '{}': proposed unit '{unit}' has dimensionality '{unit_dim}', expected '{derived}'",
                        self.key
                    )));
                }
            }
            *self.dimension = Some(derived);
        } else {
            let offending: Vec<&str> = [
                ("reference_unit", self.reference_unit.is_some()),
                ("proposed_units", self.proposed_units.is_some()),
                ("dimension", self.dimension.is_some()),
            ]
            .into_iter()
            .filter(|(_, set)| *set)
            .map(|(name, _)| name)
            .collect();
            if !offending.is_empty() {
                return Err(ValidationError(format!(
                    "Port '{}' of kind {} must not set QUANTITY-only fields: {}",
                    self.key,
                    self.kind,
                    offending.join(", ")
                )));
            }
        }
        Ok(())
    }
}

port_model!(
    /// An argument port (`ArgPortInputModel`).
    ArgPortInputModel {
        #[serde(default)]
        pub validators: Option<Vec<ValidatorInputModel>>,
        #[serde(default)]
        pub default: Option<Value>,
        #[serde(default)]
        pub widget: Option<AssignWidgetInputModel>,
        #[serde(default)]
        pub requires: Option<Vec<DescriptorConstraint>>,
        #[serde(default)]
        pub children: Option<Vec<ArgPortInputModel>>,
    }
);

impl ArgPortInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        for child in self.children.iter_mut().flatten() {
            child.validate()?;
        }
        for validator in self.validators.iter_mut().flatten() {
            validator.validate()?;
        }
        if let Some(widget) = self.widget.as_mut() {
            widget.validate()?;
        }
        for descriptor in self.requires.iter().flatten() {
            descriptor.validate("requires")?;
        }
        let child_keys: Vec<String> = self.children().iter().map(|c| c.key.clone()).collect();
        let child_keys: Vec<&str> = child_keys.iter().map(String::as_str).collect();
        self.common().validate(&child_keys)?;

        if let Some(default) = self.default.as_ref() {
            if let Some(mismatch) = value_mismatch(self, default, None, false) {
                return Err(ValidationError(format!("Port default {mismatch}")));
            }
        }
        if let (Some(default), false) = (self.default.as_ref(), self.choice_values().is_empty()) {
            let values = self.choice_values();
            if !values.contains(&default) {
                return Err(ValidationError(format!(
                    "Port {}: default {} is not one of its choices [{}]",
                    repr_str(&self.key),
                    repr(default),
                    values
                        .iter()
                        .map(|v| repr(v))
                        .collect::<Vec<_>>()
                        .join(", ")
                )));
            }
        }
        if let Some(widget) = &self.widget {
            for widget in widget.chain() {
                self.check_widget_fits(widget)?;
            }
        }
        Ok(())
    }

    /// A widget only edits ports of the kinds it can; choices and defaults must agree.
    fn check_widget_fits(&self, widget: &AssignWidgetInputModel) -> Result<(), ValidationError> {
        let key = repr_str(&self.key);
        let fits: Option<&[PortKind]> = match widget.kind() {
            "SLIDER" => Some(&[PortKind::INT, PortKind::FLOAT, PortKind::QUANTITY]),
            "STRING" => Some(&[PortKind::STRING]),
            "SEARCH" => Some(&[
                PortKind::STRUCTURE,
                PortKind::MEMORY_STRUCTURE,
                PortKind::LIST,
            ]),
            _ => None,
        };
        if let Some(fits) = fits {
            if !fits.contains(&self.kind) {
                let mut names: Vec<&str> = fits.iter().map(|k| k.value()).collect();
                names.sort();
                return Err(ValidationError(format!(
                    "Port {key} of kind {} cannot use a {} widget (fits [{}])",
                    self.kind,
                    widget.kind(),
                    names
                        .iter()
                        .map(|n| repr_str(n))
                        .collect::<Vec<_>>()
                        .join(", ")
                )));
            }
        }
        if widget.kind() == "SEARCH" && self.kind == PortKind::LIST {
            let structured = self.children().first().is_some_and(|child| {
                matches!(child.kind, PortKind::STRUCTURE | PortKind::MEMORY_STRUCTURE)
            });
            if !structured {
                return Err(ValidationError(format!(
                    "Port {key}: a SEARCH widget on a LIST port needs a STRUCTURE child"
                )));
            }
        }
        if widget.kind() == "CHOICE" && self.choice_values().is_empty() {
            return Err(ValidationError(format!(
                "Port {key}: a CHOICE widget needs the port to declare `choices`"
            )));
        }
        if let (AssignWidgetInputModel::Slider(slider), Some(default)) =
            (widget, self.default.as_ref().and_then(Value::as_f64))
        {
            if !self.default.as_ref().is_some_and(Value::is_boolean) {
                let below = slider.min.is_some_and(|min| default < min);
                let above = slider.max.is_some_and(|max| default > max);
                if below || above {
                    let bound = |b: Option<f64>| {
                        b.map(crate::pyjson::float_repr)
                            .unwrap_or_else(|| "None".into())
                    };
                    return Err(ValidationError(format!(
                        "Port {key}: default {} lies outside the SLIDER range [{}, {}]",
                        repr(self.default.as_ref().expect("checked")),
                        bound(slider.min),
                        bound(slider.max)
                    )));
                }
            }
        }
        Ok(())
    }
}

port_model!(
    /// A return port (`ReturnPortInputModel`).
    ReturnPortInputModel {
        #[serde(default)]
        pub widget: Option<ReturnWidgetInputModel>,
        #[serde(default)]
        pub provides: Option<Vec<DescriptorConstraint>>,
        #[serde(default)]
        pub children: Option<Vec<ReturnPortInputModel>>,
    }
);

impl ReturnPortInputModel {
    pub fn validate(&mut self) -> Result<(), ValidationError> {
        for child in self.children.iter_mut().flatten() {
            child.validate()?;
        }
        if let Some(widget) = self.widget.as_mut() {
            widget.validate()?;
        }
        for descriptor in self.provides.iter().flatten() {
            descriptor.validate("provides")?;
        }
        let child_keys: Vec<String> = self.children().iter().map(|c| c.key.clone()).collect();
        let child_keys: Vec<&str> = child_keys.iter().map(String::as_str).collect();
        self.common().validate(&child_keys)?;
        if matches!(self.widget, Some(ReturnWidgetInputModel::Choice(_)))
            && self.choice_values().is_empty()
        {
            return Err(ValidationError(format!(
                "Port {}: a CHOICE return widget needs the port to declare `choices`",
                repr_str(&self.key)
            )));
        }
        Ok(())
    }
}
