//! Formulas over an object's descriptors and an agent's hardware: `Fits` and the estimated
//! runtime (`rekuest_core/inputs/models.py`, the client's `arkitekt_spec/rules.py`).
//!
//! The server never evaluates a call tree. It compiles these to a PostgreSQL JSONPath over the
//! object with `$agent` as a variable, which the action query tests per live implementation.
//! Everything here must therefore mean the same as in the frontend and the agent:
//! `fits_corpus.json` holds the three to it.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::calls::{iter_util_calls, ActionArgumentInputModel, UtilCallInputModel};
use super::ports::ArgPortInputModel;
use super::ValidationError;
use crate::blok::vocabulary::vocabulary;
use crate::pyjson::dumps;

/// What a formula reads of the agent's hardware (`agent/cpu_count`).
pub const AGENT_HARDWARE: [&str; 4] = ["cpu_count", "memory_mib", "gpu_count", "gpu_memory_mib"];

const ARITHMETIC: [&str; 8] = [
    "math.add",
    "math.sub",
    "math.mul",
    "math.div",
    "math.neg",
    "math.abs",
    "math.floor",
    "math.ceil",
];
const COMPARISONS: [&str; 4] = ["cmp.gt", "cmp.gte", "cmp.lt", "cmp.lte"];
const COMBINATIONS: [&str; 2] = ["logic.all", "logic.any"];

/// A formula for how long an implementation runs, in seconds (`EstimatedRuntimeInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EstimatedRuntimeInputModel {
    pub call: UtilCallInputModel,
    #[serde(default)]
    pub source: Option<String>,
    /// A richer formula for the form, which only the frontend evaluates.
    #[serde(default)]
    pub ui_call: Option<UtilCallInputModel>,
    #[serde(default)]
    pub ui_source: Option<String>,
}

/// What an implementation can handle of one structure argument (`FitsInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FitsInputModel {
    pub port: String,
    pub call: UtilCallInputModel,
    #[serde(default)]
    pub source: Option<String>,
}

fn unescape(segment: &str) -> String {
    segment.replace("~1", "/").replace("~0", "~")
}

fn check_agent_read(segments: &[&str], path: &str, owner: &str) -> Result<(), ValidationError> {
    if segments.len() == 2 && segments[0] == "agent" && AGENT_HARDWARE.contains(&segments[1]) {
        return Ok(());
    }
    Err(ValidationError(format!(
        "{owner} reads '{path}': a formula reads descriptors and agent.{{{}}}",
        AGENT_HARDWARE.join(", ")
    )))
}

fn check_number(
    argument: &ActionArgumentInputModel,
    owner: &str,
    check_read: &mut dyn FnMut(&str) -> Result<(), ValidationError>,
) -> Result<(), ValidationError> {
    if let Some(call) = &argument.util_call {
        if !ARITHMETIC.contains(&call.operation.as_str()) {
            return Err(ValidationError(format!(
                "{owner}: {} cannot be computed when matching actions",
                call.operation
            )));
        }
        for nested in call.arguments.as_deref().unwrap_or_default() {
            check_number(nested, owner, check_read)?;
        }
        return Ok(());
    }
    if let Some(path) = &argument.value_path {
        return check_read(path);
    }
    match &argument.value_literal {
        Some(Value::Number(_)) => Ok(()),
        other => Err(ValidationError(format!(
            "{owner} uses {other:?}: it only works on numbers"
        ))),
    }
}

fn check_predicate(call: &UtilCallInputModel, owner: &str) -> Result<(), ValidationError> {
    let arguments = call.arguments.as_deref().unwrap_or_default();
    if COMPARISONS.contains(&call.operation.as_str()) {
        let mut check_read = |path: &str| {
            let segments: Vec<&str> = path.split('/').collect();
            if segments[0] == "value" {
                if segments.len() == 2 && segments[1].starts_with('@') {
                    return Ok(());
                }
                return Err(ValidationError(format!(
                    "{owner} reads '{path}': name a descriptor of this argument"
                )));
            }
            check_agent_read(&segments, path, owner)
        };
        for argument in arguments {
            check_number(argument, owner, &mut check_read)?;
        }
        return Ok(());
    }
    if COMBINATIONS.contains(&call.operation.as_str()) {
        for argument in arguments {
            for item in argument.value_list.as_deref().unwrap_or_default() {
                let Some(nested) = &item.util_call else {
                    return Err(ValidationError(format!(
                        "{owner}: and/or combine comparisons"
                    )));
                };
                check_predicate(nested, owner)?;
            }
        }
        return Ok(());
    }
    Err(ValidationError(format!(
        "{owner} must be a comparison, or several joined by and/or, not {}",
        call.operation
    )))
}

/// The `Fits` of one implementation, each on a single structure argument, one per argument.
pub fn check_fits_of(
    fits: &[FitsInputModel],
    args: &[ArgPortInputModel],
    interface: &str,
) -> Result<(), ValidationError> {
    let mut seen = Vec::new();
    for fit in fits {
        let owner = format!("The Fits of {interface} on {}", fit.port);
        let port = args.iter().find(|port| port.key == fit.port);
        if !port.is_some_and(|port| {
            port.identifier.is_some() && port.children.as_deref().unwrap_or_default().is_empty()
        }) {
            return Err(ValidationError(format!(
                "{owner}: only a single structure argument can have one"
            )));
        }
        if seen.contains(&fit.port.as_str()) {
            return Err(ValidationError(format!(
                "{owner}: an argument has one Fits; join conditions with and/or"
            )));
        }
        seen.push(fit.port.as_str());
        check_predicate(&fit.call, &owner)?;
    }
    Ok(())
}

impl EstimatedRuntimeInputModel {
    pub fn check(
        &self,
        args: &[ArgPortInputModel],
        interface: &str,
    ) -> Result<(), ValidationError> {
        let owner = format!("The estimated runtime of {interface}");
        if !ARITHMETIC.contains(&self.call.operation.as_str()) {
            return Err(ValidationError(format!(
                "{owner}: {} cannot be computed when matching actions",
                self.call.operation
            )));
        }
        let mut read: Vec<String> = Vec::new();
        let mut check_read = |path: &str| {
            let segments: Vec<&str> = path.split('/').collect();
            if segments[0] == "args" {
                let port = segments
                    .get(1)
                    .and_then(|key| args.iter().find(|port| port.key == *key));
                if segments.len() != 3
                    || !segments[2].starts_with('@')
                    || port.is_none_or(|port| port.identifier.is_none())
                {
                    return Err(ValidationError(format!(
                        "{owner} reads '{path}': it can read the descriptors of a structure argument"
                    )));
                }
                if !read.iter().any(|key| key == segments[1]) {
                    read.push(segments[1].to_owned());
                }
                if read.len() > 1 {
                    return Err(ValidationError(format!(
                        "{owner} reads {}: the action query has one object, so it reads one argument",
                        read.join(" and ")
                    )));
                }
                return Ok(());
            }
            check_agent_read(&segments, path, &owner)
        };
        for argument in self.call.arguments.as_deref().unwrap_or_default() {
            check_number(argument, &owner, &mut check_read)?;
        }
        if let Some(ui) = &self.ui_call {
            check_ui(
                ui,
                args,
                &format!("The form's estimated runtime of {interface}"),
            )?;
        }
        Ok(())
    }

    /// The one structure argument the formula reads, if any.
    pub fn port(&self) -> Option<String> {
        value_paths(self.call.arguments.as_deref())
            .into_iter()
            .find_map(|path| {
                path.strip_prefix("args/")?
                    .split('/')
                    .next()
                    .map(str::to_owned)
            })
    }

    /// The `(port, descriptor key)` pairs both formulas read, keys unescaped.
    pub fn descriptors(&self) -> Vec<(String, String)> {
        let mut paths = value_paths(self.call.arguments.as_deref());
        if let Some(ui) = &self.ui_call {
            paths.extend(value_paths(ui.arguments.as_deref()));
        }
        paths
            .into_iter()
            .filter_map(|path| {
                let mut segments = path.strip_prefix("args/")?.split('/');
                let port = segments.next()?;
                let key = segments.next().filter(|segment| segment.starts_with('@'))?;
                Some((port.to_owned(), unescape(key)))
            })
            .collect()
    }

    /// The formula as a PostgreSQL JSONPath giving a number.
    pub fn compile(&self) -> Result<String, ValidationError> {
        compile_call(&self.call)
    }
}

impl FitsInputModel {
    /// The descriptor keys it reads, unescaped.
    pub fn keys(&self) -> Vec<String> {
        fn walk(call: &UtilCallInputModel, keys: &mut Vec<String>) {
            for path in value_paths(call.arguments.as_deref()) {
                if let Some(key) = path.strip_prefix("value/") {
                    keys.push(unescape(key));
                }
            }
        }
        let mut keys = Vec::new();
        walk(&self.call, &mut keys);
        keys
    }

    /// The formula as a PostgreSQL JSONPath predicate.
    pub fn compile(&self) -> Result<String, ValidationError> {
        compile_predicate(&self.call)
    }
}

fn check_ui(
    call: &UtilCallInputModel,
    args: &[ArgPortInputModel],
    owner: &str,
) -> Result<(), ValidationError> {
    let calls = iter_util_calls(call.arguments.as_deref());
    for each in std::iter::once(call).chain(calls) {
        if vocabulary()
            .function(&each.operation)
            .is_none_or(|function| function.effect)
        {
            return Err(ValidationError(format!(
                "{owner}: {} is not a pure function of the blok language",
                each.operation
            )));
        }
    }
    for path in value_paths(call.arguments.as_deref()) {
        let segments: Vec<&str> = path.split('/').collect();
        if segments[0] == "args" && segments.len() > 1 {
            if !args.iter().any(|port| port.key == segments[1]) {
                return Err(ValidationError(format!(
                    "{owner} reads args.{}, but the action has no such argument",
                    segments[1]
                )));
            }
        } else {
            check_agent_read(&segments, path, owner)?;
        }
    }
    Ok(())
}

/// Every `value_path` under these arguments.
fn value_paths(arguments: Option<&[ActionArgumentInputModel]>) -> Vec<&str> {
    let mut paths = Vec::new();
    for argument in arguments.unwrap_or_default() {
        paths.extend(argument.value_path.as_deref());
        if let Some(call) = &argument.util_call {
            paths.extend(value_paths(call.arguments.as_deref()));
        }
        paths.extend(value_paths(argument.value_list.as_deref()));
        paths.extend(value_paths(argument.value_dict.as_deref()));
    }
    paths
}

fn argument<'a>(
    call: &'a UtilCallInputModel,
    key: &str,
) -> Result<&'a ActionArgumentInputModel, ValidationError> {
    call.arguments
        .as_deref()
        .unwrap_or_default()
        .iter()
        .find(|argument| argument.key.as_deref() == Some(key))
        .ok_or_else(|| ValidationError(format!("{} needs {key}", call.operation)))
}

fn compile_number(argument: &ActionArgumentInputModel) -> Result<String, ValidationError> {
    if let Some(call) = &argument.util_call {
        return compile_call(call);
    }
    if let Some(path) = &argument.value_path {
        let segments: Vec<&str> = path.split('/').collect();
        return Ok(match segments.as_slice() {
            ["agent", name] => format!("$agent.{name}"),
            ["value", key] | ["args", _, key] => {
                format!("$.{}", dumps(&Value::String(unescape(key)), false))
            }
            _ => return Err(ValidationError(format!("Cannot compile the path '{path}'"))),
        });
    }
    match &argument.value_literal {
        Some(number @ Value::Number(_)) => Ok(number.to_string()),
        other => Err(ValidationError(format!(
            "Cannot compile the literal {other:?}"
        ))),
    }
}

fn compile_call(call: &UtilCallInputModel) -> Result<String, ValidationError> {
    let binary = |operator: &str| -> Result<String, ValidationError> {
        Ok(format!(
            "({} {operator} {})",
            compile_number(argument(call, "a")?)?,
            compile_number(argument(call, "b")?)?
        ))
    };
    let method = |name: &str| -> Result<String, ValidationError> {
        Ok(format!(
            "({}).{name}()",
            compile_number(argument(call, "value")?)?
        ))
    };
    match call.operation.as_str() {
        "math.add" => binary("+"),
        "math.sub" => binary("-"),
        "math.mul" => binary("*"),
        "math.div" => binary("/"),
        "math.neg" => Ok(format!("(-{})", compile_number(argument(call, "a")?)?)),
        "math.abs" => method("abs"),
        "math.floor" => method("floor"),
        "math.ceil" => method("ceiling"),
        other => Err(ValidationError(format!("Cannot compile {other}"))),
    }
}

fn compile_predicate(call: &UtilCallInputModel) -> Result<String, ValidationError> {
    let comparison = |operator: &str| -> Result<String, ValidationError> {
        Ok(format!(
            "{} {operator} {}",
            compile_number(argument(call, "a")?)?,
            compile_number(argument(call, "b")?)?
        ))
    };
    let combination = |operator: &str| -> Result<String, ValidationError> {
        let parts = argument(call, "values")?
            .value_list
            .as_deref()
            .unwrap_or_default()
            .iter()
            .filter_map(|item| item.util_call.as_ref())
            .map(|nested| Ok(format!("({})", compile_predicate(nested)?)))
            .collect::<Result<Vec<String>, ValidationError>>()?;
        Ok(parts.join(operator))
    };
    match call.operation.as_str() {
        "cmp.gt" => comparison(">"),
        "cmp.gte" => comparison(">="),
        "cmp.lt" => comparison("<"),
        "cmp.lte" => comparison("<="),
        "logic.all" => combination(" && "),
        "logic.any" => combination(" || "),
        other => Err(ValidationError(format!("Cannot compile {other}"))),
    }
}
