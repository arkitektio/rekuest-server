//! BSX, a blok written as XML, parsed into its component tree (arkitekt-spec's `bsx()`).
//!
//! A client may send a blok as its source instead of its tree: three times fewer bytes. The
//! tree this makes is the one the spec's parser makes from the same source, which
//! `tests/bsx.rs` holds it to over a corpus the spec generates (`tests/fixtures/generate_bsx.py`).
//!
//! An element is a component and its attributes its props. A prop value is:
//! 1. `$state.dep.path`, a path into state (`state.` dropped);
//! 2. `@pkg/key`, a structure identifier, kept as the string it is;
//! 3. `@expr`, a Python expression: a path (`@dep.x`), or a call to an agent (`@dep.op(...)`,
//!    `@actions.dep.op(...)`, bound with `.into(name)`) or to a util (`@utils.op(...)`);
//! 4. `#name` as a `foreach`'s `let`, the loop variable it declares;
//! 5. anything else, the string itself.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::{Number, Value};

use crate::catalogs::base_operations;
use crate::inputs::{
    ActionArgumentInputModel, AgentProbeInputModel, ComponentNodeInputModel,
    ComponentPropInputModel, DynamicValueInputModel, UtilCallInputModel,
};

/// The longest source a server parses: a blok is a page of markup, not a document.
pub const MAX_SOURCE_BYTES: usize = 256 * 1024;

const BOUND_CALL_STEP: &str = "into";
const EVENT_ROOT: &str = "event";
const EVENT_SCOPE_KEY: &str = "$event";
const FOREACH_COMPONENT: &str = "foreach";
const FOREACH_LET_PROP: &str = "let";

fn structure_identifier() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| Regex::new(r"^@[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$").expect("valid regex"))
}

/// The component tree `source` describes; why it does not parse otherwise.
pub fn parse_bsx(source: &str) -> Result<ComponentNodeInputModel, String> {
    if source.len() > MAX_SOURCE_BYTES {
        return Err(format!(
            "A blok's source is at most {MAX_SOURCE_BYTES} bytes, this one {}",
            source.len()
        ));
    }
    let document = roxmltree::Document::parse(source).map_err(|error| {
        let position = error.pos();
        format!(
            "Failed to parse BSX/XML at line {}, column {}: {error}",
            position.row, position.col
        )
    })?;
    let root = document.root_element();
    parse_element(root, tag_of(root)?)
}

fn tag_of(element: roxmltree::Node) -> Result<String, String> {
    let name = element.tag_name();
    if name.namespace().is_some() {
        return Err(format!(
            "<{}> is in an XML namespace; a blok's components have plain names",
            name.name()
        ));
    }
    Ok(name.name().to_string())
}

fn parse_element(
    element: roxmltree::Node,
    path: String,
) -> Result<ComponentNodeInputModel, String> {
    let component = tag_of(element)?;
    let node_id = element.attribute("id").map(str::to_string).unwrap_or(path);

    reject_element_text(element, &node_id)?;

    let mut props = vec![];
    for attribute in element.attributes() {
        if attribute.namespace().is_some() {
            return Err(format!(
                "<{component}> ({node_id}): attribute {} is in an XML namespace",
                attribute.name()
            ));
        }
        if attribute.name() == "id" {
            continue;
        }
        props.push(parse_prop(&component, attribute.name(), attribute.value())?);
    }

    let mut children = vec![];
    let mut seen: Vec<(String, usize)> = vec![];
    for child in element.children().filter(roxmltree::Node::is_element) {
        let tag = tag_of(child)?;
        let index = match seen.iter_mut().find(|(name, _)| *name == tag) {
            Some((_, count)) => {
                *count += 1;
                *count - 1
            }
            None => {
                seen.push((tag.clone(), 1));
                0
            }
        };
        children.push(parse_element(child, format!("{node_id}/{tag}[{index}]"))?);
    }

    Ok(ComponentNodeInputModel {
        id: node_id,
        component,
        props: (!props.is_empty()).then_some(props),
        children: (!children.is_empty()).then_some(children),
    })
}

/// Text in an element is never rendered: refused, so `<Badge>hello</Badge>` does not lose it.
fn reject_element_text(element: roxmltree::Node, node_id: &str) -> Result<(), String> {
    for child in element.children() {
        if let Some(text) = child.is_text().then(|| child.text()).flatten() {
            let text = text.trim();
            if !text.is_empty() {
                let tag = element.tag_name().name();
                return Err(format!(
                    "Text content is not rendered in <{tag}> ({node_id}): {}. Pass it as a prop instead, e.g. <{tag} text=\"{text}\" />",
                    crate::pyjson::repr_str(text)
                ));
            }
        }
    }
    Ok(())
}

fn prop(key: &str) -> ComponentPropInputModel {
    ComponentPropInputModel {
        key: key.to_string(),
        static_value: None,
        dynamic_value: None,
        declares_value: None,
        agent_call: None,
        util_call: None,
    }
}

fn parse_prop(component: &str, key: &str, value: &str) -> Result<ComponentPropInputModel, String> {
    let value = value.trim();

    if let Some(path) = value.strip_prefix('$') {
        let parts: Vec<&str> = path.split('.').collect();
        if parts.len() < 3 || parts[0] != "state" {
            return Err(format!(
                "Invalid state namespace for dynamic value prop '{key}': '{path}'. Expected format: state.dependency_key.nested_path"
            ));
        }
        return Ok(ComponentPropInputModel {
            dynamic_value: Some(dynamic(parts[1..].join("."))),
            ..prop(key)
        });
    }

    if structure_identifier().is_match(value) {
        return Ok(ComponentPropInputModel {
            static_value: Some(Value::String(value.to_string())),
            ..prop(key)
        });
    }

    if let Some(expression) = value.strip_prefix('@') {
        let parsed = Expr::parse(expression)
            .map_err(|error| format!("Failed to parse action syntax '{expression}': {error}"))?;
        return match parsed {
            Expr::Name(_) | Expr::Attribute(..) => Ok(ComponentPropInputModel {
                dynamic_value: Some(dynamic(extract_path(&parsed)?)),
                ..prop(key)
            }),
            Expr::Call { .. } => {
                if let Some((call, name)) = bound_call(&parsed, expression)? {
                    return Ok(ComponentPropInputModel {
                        agent_call: Some(call),
                        declares_value: Some(name),
                        ..prop(key)
                    });
                }
                Ok(match parse_call(&parsed)? {
                    Call::Agent(call) => ComponentPropInputModel {
                        agent_call: Some(call),
                        ..prop(key)
                    },
                    Call::Util(call) => ComponentPropInputModel {
                        util_call: Some(call),
                        ..prop(key)
                    },
                })
            }
            _ => Err(format!(
                "Dynamic expression must be a path or function call. Got: {expression}"
            )),
        };
    }

    if component.to_lowercase() == FOREACH_COMPONENT && key == FOREACH_LET_PROP {
        let declared = value.strip_prefix('#').unwrap_or("");
        if declared.is_empty() || !is_identifier(declared) {
            return Err(format!(
                "<{component}> prop '{key}' must declare a loop variable as '#name', where name is a valid identifier. Got: {}",
                crate::pyjson::repr_str(value)
            ));
        }
        return Ok(ComponentPropInputModel {
            static_value: Some(Value::String(declared.to_string())),
            declares_value: Some(declared.to_string()),
            ..prop(key)
        });
    }

    Ok(ComponentPropInputModel {
        static_value: Some(Value::String(value.to_string())),
        ..prop(key)
    })
}

fn dynamic(path: String) -> DynamicValueInputModel {
    DynamicValueInputModel {
        literal: None,
        path: Some(path),
    }
}

// --------------------------------------------------------------------------- calls

enum Call {
    Agent(AgentProbeInputModel),
    Util(UtilCallInputModel),
}

/// `dep.op(...).into(name)`: the call and the name its task is bound to.
fn bound_call(node: &Expr, source: &str) -> Result<Option<(AgentProbeInputModel, String)>, String> {
    let Expr::Call {
        func,
        args,
        keywords,
    } = node
    else {
        return Ok(None);
    };
    let Expr::Attribute(value, attr) = func.as_ref() else {
        return Ok(None);
    };
    if attr != BOUND_CALL_STEP || !matches!(value.as_ref(), Expr::Call { .. }) {
        return Ok(None);
    }
    let name = match (args.as_slice(), keywords.is_empty()) {
        ([Expr::Name(name)], true) => name.clone(),
        _ => {
            return Err(format!(
                "'.{BOUND_CALL_STEP}(...)' takes one bare name, the value the call's task is bound to, e.g. '.{BOUND_CALL_STEP}(run)'. Got: {source}"
            ))
        }
    };
    match parse_call(value)? {
        Call::Agent(call) => Ok(Some((call, name))),
        Call::Util(_) => Err(format!(
            "Only a call to an agent runs as a task that can be bound with '.{BOUND_CALL_STEP}(...)'; a utils call returns at once. Got: {source}"
        )),
    }
}

fn parse_call(node: &Expr) -> Result<Call, String> {
    let Expr::Call {
        func,
        args,
        keywords,
    } = node
    else {
        unreachable!("parse_call is only given calls")
    };
    let full_path = extract_path(func)?;
    let parts: Vec<&str> = full_path.split('.').collect();

    let mut arguments = vec![];
    for (index, argument) in args.iter().enumerate() {
        arguments.push(parse_argument(argument, Some(index.to_string()))?);
    }
    for (name, argument) in keywords {
        let Some(name) = name else {
            return Err("Call arguments cannot be unpacked with '**'".to_string());
        };
        arguments.push(parse_argument(argument, Some(name.clone()))?);
    }

    if parts[0] == "utils" {
        if parts.len() < 2 {
            return Err(format!("Invalid utils namespace: '{full_path}'."));
        }
        let operation = parts[1..].join(".");
        let arguments =
            resolve_base_arguments(&operation, arguments, &format!("utils.{operation}"))?;
        return Ok(Call::Util(UtilCallInputModel {
            operation,
            arguments: (!arguments.is_empty()).then_some(arguments),
        }));
    }
    if parts[0] == "actions" {
        if parts.len() < 3 {
            return Err(format!(
                "Invalid action namespace: '{full_path}'. Expected: actions.dependency.operation"
            ));
        }
        return Ok(Call::Agent(AgentProbeInputModel {
            dependency: parts[1].to_string(),
            operation: parts[2..].join("."),
            arguments: (!arguments.is_empty()).then_some(arguments),
        }));
    }
    if parts.len() < 2 {
        return Err(format!(
            "Action calls must begin with a dependency key or 'utils.'. Got: '{full_path}'"
        ));
    }
    Ok(Call::Agent(AgentProbeInputModel {
        dependency: parts[0].to_string(),
        operation: parts[1..].join("."),
        arguments: (!arguments.is_empty()).then_some(arguments),
    }))
}

fn argument(key: Option<String>) -> ActionArgumentInputModel {
    ActionArgumentInputModel {
        key,
        value_literal: None,
        value_path: None,
        agent_call: None,
        util_call: None,
        value_list: None,
        value_dict: None,
    }
}

fn parse_argument(node: &Expr, key: Option<String>) -> Result<ActionArgumentInputModel, String> {
    let owner = key.clone().unwrap_or_else(|| "item".to_string());
    match node {
        Expr::None => Err(format!(
            "Argument '{owner}' cannot be None: a missing literal is indistinguishable from no value. Omit the argument instead."
        )),
        Expr::Literal(value) => Ok(ActionArgumentInputModel {
            value_literal: Some(value.clone()),
            ..argument(key)
        }),
        Expr::Name(_) | Expr::Attribute(..) => {
            let path = extract_path(node)?;
            let path = match path.split_once('.') {
                Some((EVENT_ROOT, rest)) => format!("{EVENT_SCOPE_KEY}.{rest}"),
                None if path == EVENT_ROOT => EVENT_SCOPE_KEY.to_string(),
                _ => path,
            };
            Ok(ActionArgumentInputModel {
                value_path: Some(path),
                ..argument(key)
            })
        }
        Expr::Call { .. } => Ok(match parse_call(node)? {
            Call::Agent(call) => ActionArgumentInputModel {
                agent_call: Some(call),
                ..argument(key)
            },
            Call::Util(call) => ActionArgumentInputModel {
                util_call: Some(call),
                ..argument(key)
            },
        }),
        Expr::List(items) => Ok(ActionArgumentInputModel {
            value_list: Some(
                items
                    .iter()
                    .map(|item| parse_argument(item, None))
                    .collect::<Result<_, _>>()?,
            ),
            ..argument(key)
        }),
        Expr::Dict(entries) => {
            let mut parsed = vec![];
            for (entry_key, entry_value) in entries {
                match entry_key {
                    Some(Expr::Literal(Value::String(name))) if !name.is_empty() => {
                        parsed.push(parse_argument(entry_value, Some(name.clone()))?);
                    }
                    _ => return Err("Dictionary keys must be non-empty literal strings.".to_string()),
                }
            }
            Ok(ActionArgumentInputModel {
                value_dict: Some(parsed),
                ..argument(key)
            })
        }
    }
}

fn extract_path(node: &Expr) -> Result<String, String> {
    match node {
        Expr::Name(name) => Ok(name.clone()),
        Expr::Attribute(value, attr) => Ok(format!("{}.{attr}", extract_path(value)?)),
        other => Err(format!(
            "Cannot extract path from node type: {}",
            other.python_type()
        )),
    }
}

/// `resolve_base_arguments`: a base operation's positional arguments named, in its order.
fn resolve_base_arguments(
    operation: &str,
    arguments: Vec<ActionArgumentInputModel>,
    owner: &str,
) -> Result<Vec<ActionArgumentInputModel>, String> {
    let operations = base_operations();
    let Some(spec) = operations.get(operation) else {
        return Ok(arguments);
    };
    let keys: Vec<&str> = spec.arguments.iter().map(|a| a.key.as_str()).collect();
    let is_positional = |a: &ActionArgumentInputModel| {
        a.key
            .as_deref()
            .is_none_or(|k| !k.is_empty() && k.bytes().all(|b| b.is_ascii_digit()))
    };
    let mut positional: Vec<ActionArgumentInputModel> = arguments
        .iter()
        .filter(|a| is_positional(a))
        .cloned()
        .collect();
    positional.sort_by_key(|a| {
        a.key
            .as_deref()
            .and_then(|k| k.parse::<i64>().ok())
            .unwrap_or(-1)
    });
    let named: Vec<ActionArgumentInputModel> = arguments
        .into_iter()
        .filter(|a| !is_positional(a))
        .collect();

    if positional.len() > keys.len() {
        return Err(format!(
            "{owner}: {operation} takes at most {} positional arguments ({})",
            keys.len(),
            keys.join(", ")
        ));
    }
    let mut resolved: Vec<(String, ActionArgumentInputModel)> = vec![];
    for (index, mut argument) in positional.into_iter().enumerate() {
        argument.key = Some(keys[index].to_string());
        resolved.push((keys[index].to_string(), argument));
    }
    for argument in named {
        let key = argument.key.clone().unwrap_or_default();
        if !keys.contains(&key.as_str()) {
            return Err(format!(
                "{owner}: {operation} does not accept argument {} (accepts {})",
                crate::pyjson::repr_str(&key),
                keys.join(", ")
            ));
        }
        if resolved.iter().any(|(k, _)| *k == key) {
            return Err(format!(
                "{owner}: {operation} got multiple values for {}",
                crate::pyjson::repr_str(&key)
            ));
        }
        resolved.push((key, argument));
    }
    let missing: Vec<String> = spec
        .arguments
        .iter()
        .filter(|a| a.required && !resolved.iter().any(|(k, _)| *k == a.key))
        .map(|a| crate::pyjson::repr_str(&a.key))
        .collect();
    if !missing.is_empty() {
        return Err(format!(
            "{owner}: {operation} requires arguments [{}]",
            missing.join(", ")
        ));
    }
    Ok(keys
        .iter()
        .filter_map(|key| {
            resolved
                .iter()
                .find(|(k, _)| k == key)
                .map(|(_, a)| a.clone())
        })
        .collect())
}

fn is_identifier(name: &str) -> bool {
    let mut chars = name.chars();
    chars
        .next()
        .is_some_and(|first| first == '_' || first.is_alphabetic())
        && chars.all(|c| c == '_' || c.is_alphanumeric())
}

// --------------------------------------------------------------------------- expressions

/// The part of Python's expression grammar a blok prop may use.
#[derive(Debug, Clone, PartialEq)]
enum Expr {
    Name(String),
    Attribute(Box<Expr>, String),
    Call {
        func: Box<Expr>,
        args: Vec<Expr>,
        /// `None` names a `**unpacked` argument, which is refused.
        keywords: Vec<(Option<String>, Expr)>,
    },
    Literal(Value),
    None,
    List(Vec<Expr>),
    /// `None` keys are `**unpacked` entries, which are refused.
    Dict(Vec<(Option<Expr>, Expr)>),
}

impl Expr {
    fn parse(source: &str) -> Result<Expr, String> {
        // Python reads leading whitespace as indentation, which an expression has none of.
        if source.starts_with(char::is_whitespace) {
            return Err("unexpected indent".to_string());
        }
        let tokens = tokenize(source)?;
        let mut parser = Parser { tokens, at: 0 };
        let expression = parser.expression()?;
        if parser.at != parser.tokens.len() {
            return Err(format!(
                "invalid syntax at {}",
                parser.tokens[parser.at].describe()
            ));
        }
        Ok(expression)
    }

    fn python_type(&self) -> &'static str {
        match self {
            Expr::Name(_) => "Name",
            Expr::Attribute(..) => "Attribute",
            Expr::Call { .. } => "Call",
            Expr::Literal(_) | Expr::None => "Constant",
            Expr::List(_) => "List",
            Expr::Dict(_) => "Dict",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
enum Token {
    Name(String),
    Str(String),
    Int(Value),
    Float(f64),
    Punct(char),
    DoubleStar,
}

impl Token {
    fn describe(&self) -> String {
        match self {
            Token::Name(name) => format!("'{name}'"),
            Token::Str(_) => "a string".to_string(),
            Token::Int(_) | Token::Float(_) => "a number".to_string(),
            Token::Punct(c) => format!("'{c}'"),
            Token::DoubleStar => "'**'".to_string(),
        }
    }
}

const PUNCTUATION: &str = "().,[]{}:=+-";

fn tokenize(source: &str) -> Result<Vec<Token>, String> {
    let chars: Vec<char> = source.chars().collect();
    let mut tokens = vec![];
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c.is_whitespace() {
            i += 1;
            continue;
        }
        if c == '*' {
            if chars.get(i + 1) == Some(&'*') {
                tokens.push(Token::DoubleStar);
                i += 2;
                continue;
            }
            return Err("'*' is not supported".to_string());
        }
        if c.is_ascii_digit() || (c == '.' && chars.get(i + 1).is_some_and(char::is_ascii_digit)) {
            let (token, next) = number(&chars, i)?;
            tokens.push(token);
            i = next;
            continue;
        }
        if c == '_' || c.is_alphabetic() {
            let start = i;
            while i < chars.len() && (chars[i] == '_' || chars[i].is_alphanumeric()) {
                i += 1;
            }
            let word: String = chars[start..i].iter().collect();
            // A string prefix: u and r (raw) make a str, and b a bytes literal, which the
            // spec dumps as the string it spells. An f-string is an expression, refused.
            if i < chars.len() && (chars[i] == '\'' || chars[i] == '"') {
                let lower = word.to_lowercase();
                if matches!(lower.as_str(), "u" | "r" | "b" | "br" | "rb") {
                    let (text, next) = string(&chars, i, lower.contains('r'))?;
                    push_string(&mut tokens, text);
                    i = next;
                    continue;
                }
                return Err(format!("a {word}-prefixed string is not a plain literal"));
            }
            tokens.push(Token::Name(word));
            continue;
        }
        if c == '\'' || c == '"' {
            let (text, next) = string(&chars, i, false)?;
            push_string(&mut tokens, text);
            i = next;
            continue;
        }
        if PUNCTUATION.contains(c) {
            tokens.push(Token::Punct(c));
            i += 1;
            continue;
        }
        return Err(format!("invalid character '{c}'"));
    }
    Ok(tokens)
}

/// Adjacent strings are one string, as in Python.
fn push_string(tokens: &mut Vec<Token>, text: String) {
    if let Some(Token::Str(previous)) = tokens.last_mut() {
        previous.push_str(&text);
    } else {
        tokens.push(Token::Str(text));
    }
}

fn number(chars: &[char], start: usize) -> Result<(Token, usize), String> {
    let mut i = start;
    let radix = match (
        chars.get(i),
        chars.get(i + 1).map(|c| c.to_ascii_lowercase()),
    ) {
        (Some('0'), Some('x')) => Some(16),
        (Some('0'), Some('o')) => Some(8),
        (Some('0'), Some('b')) => Some(2),
        _ => None,
    };
    if let Some(radix) = radix {
        i += 2;
        let digits_start = i;
        while i < chars.len() && (chars[i].is_digit(radix) || chars[i] == '_') {
            i += 1;
        }
        let digits: String = chars[digits_start..i]
            .iter()
            .filter(|c| **c != '_')
            .collect();
        let value =
            i64::from_str_radix(&digits, radix).map_err(|_| "invalid number".to_string())?;
        return Ok((Token::Int(Value::Number(value.into())), i));
    }
    let mut is_float = false;
    while i < chars.len() {
        let c = chars[i];
        if c.is_ascii_digit() || c == '_' {
            i += 1;
        } else if c == '.' && !is_float {
            is_float = true;
            i += 1;
        } else if (c == 'e' || c == 'E')
            && chars
                .get(i + 1)
                .is_some_and(|n| n.is_ascii_digit() || *n == '+' || *n == '-')
        {
            is_float = true;
            i += 2;
        } else {
            break;
        }
    }
    let text: String = chars[start..i].iter().filter(|c| **c != '_').collect();
    if chars
        .get(i)
        .is_some_and(|c| c.is_alphanumeric() || *c == '_')
    {
        return Err("invalid number".to_string());
    }
    if is_float {
        let value: f64 = text.parse().map_err(|_| "invalid number".to_string())?;
        return Ok((Token::Float(value), i));
    }
    let value: i64 = text.parse().map_err(|_| "invalid number".to_string())?;
    Ok((Token::Int(Value::Number(value.into())), i))
}

fn string(chars: &[char], start: usize, raw: bool) -> Result<(String, usize), String> {
    let quote = chars[start];
    let triple = chars.get(start + 1) == Some(&quote) && chars.get(start + 2) == Some(&quote);
    let mut i = start + if triple { 3 } else { 1 };
    let mut text = String::new();
    loop {
        let Some(&c) = chars.get(i) else {
            return Err("unterminated string literal".to_string());
        };
        if c == quote
            && (!triple || (chars.get(i + 1) == Some(&quote) && chars.get(i + 2) == Some(&quote)))
        {
            return Ok((text, i + if triple { 3 } else { 1 }));
        }
        if c == '\n' && !triple {
            return Err("unterminated string literal".to_string());
        }
        if c == '\\' {
            let Some(&next) = chars.get(i + 1) else {
                return Err("unterminated string literal".to_string());
            };
            if raw {
                text.push('\\');
                text.push(next);
                i += 2;
                continue;
            }
            i += 2;
            match next {
                '\n' => {}
                '\\' => text.push('\\'),
                '\'' => text.push('\''),
                '"' => text.push('"'),
                'a' => text.push('\x07'),
                'b' => text.push('\x08'),
                'f' => text.push('\x0c'),
                'n' => text.push('\n'),
                'r' => text.push('\r'),
                't' => text.push('\t'),
                'v' => text.push('\x0b'),
                '0'..='7' => {
                    let mut digits = next.to_string();
                    while digits.len() < 3 && chars.get(i).is_some_and(|d| d.is_digit(8)) {
                        digits.push(chars[i]);
                        i += 1;
                    }
                    let code = u32::from_str_radix(&digits, 8).expect("octal digits");
                    text.push(char::from_u32(code).ok_or("invalid escape")?);
                }
                'x' | 'u' | 'U' => {
                    let width = match next {
                        'x' => 2,
                        'u' => 4,
                        _ => 8,
                    };
                    let hex: String = chars
                        .get(i..i + width)
                        .ok_or("truncated escape")?
                        .iter()
                        .collect();
                    let code = u32::from_str_radix(&hex, 16).map_err(|_| "truncated escape")?;
                    text.push(char::from_u32(code).ok_or("invalid escape")?);
                    i += width;
                }
                'N' => return Err("\\N{...} escapes are not supported".to_string()),
                // An unknown escape keeps its backslash, as in Python.
                other => {
                    text.push('\\');
                    text.push(other);
                }
            }
            continue;
        }
        text.push(c);
        i += 1;
    }
}

struct Parser {
    tokens: Vec<Token>,
    at: usize,
}

impl Parser {
    fn peek(&self) -> Option<&Token> {
        self.tokens.get(self.at)
    }

    fn eat(&mut self, punct: char) -> bool {
        if self.peek() == Some(&Token::Punct(punct)) {
            self.at += 1;
            true
        } else {
            false
        }
    }

    fn expect(&mut self, punct: char) -> Result<(), String> {
        if self.eat(punct) {
            Ok(())
        } else {
            Err(match self.peek() {
                Some(token) => format!("expected '{punct}', found {}", token.describe()),
                None => format!("expected '{punct}', found the end"),
            })
        }
    }

    fn expression(&mut self) -> Result<Expr, String> {
        // Unary signs on a number are the only operators: `-1`, `+2.5`.
        if self.eat('-') {
            return self.signed(true);
        }
        if self.eat('+') {
            return self.signed(false);
        }
        let mut expression = self.atom()?;
        loop {
            if self.eat('.') {
                match self.tokens.get(self.at).cloned() {
                    Some(Token::Name(name)) => {
                        self.at += 1;
                        expression = Expr::Attribute(Box::new(expression), name);
                    }
                    _ => return Err("expected a name after '.'".to_string()),
                }
            } else if self.eat('(') {
                expression = self.call(expression)?;
            } else {
                return Ok(expression);
            }
        }
    }

    fn signed(&mut self, negative: bool) -> Result<Expr, String> {
        match self.tokens.get(self.at).cloned() {
            Some(Token::Int(Value::Number(n))) => {
                self.at += 1;
                let value = n.as_i64().ok_or("invalid number")?;
                Ok(Expr::Literal(Value::Number(
                    if negative { -value } else { value }.into(),
                )))
            }
            Some(Token::Float(f)) => {
                self.at += 1;
                let value = if negative { -f } else { f };
                Ok(Expr::Literal(Value::Number(
                    Number::from_f64(value).ok_or("invalid number")?,
                )))
            }
            _ => Err("a sign only goes before a number".to_string()),
        }
    }

    fn atom(&mut self) -> Result<Expr, String> {
        let Some(token) = self.tokens.get(self.at).cloned() else {
            return Err("unexpected end of expression".to_string());
        };
        self.at += 1;
        match token {
            Token::Name(name) => Ok(match name.as_str() {
                "True" => Expr::Literal(Value::Bool(true)),
                "False" => Expr::Literal(Value::Bool(false)),
                "None" => Expr::None,
                _ if is_keyword(&name) => return Err(format!("'{name}' is a keyword")),
                _ => Expr::Name(name),
            }),
            Token::Str(text) => Ok(Expr::Literal(Value::String(text))),
            Token::Int(value) => Ok(Expr::Literal(value)),
            Token::Float(value) => Ok(Expr::Literal(Value::Number(
                Number::from_f64(value).ok_or("invalid number")?,
            ))),
            Token::Punct('(') => {
                if self.eat(')') {
                    return Ok(Expr::List(vec![]));
                }
                let first = self.expression()?;
                if self.eat(')') {
                    return Ok(first);
                }
                let mut items = vec![first];
                while self.eat(',') {
                    if self.eat(')') {
                        return Ok(Expr::List(items));
                    }
                    items.push(self.expression()?);
                }
                self.expect(')')?;
                Ok(Expr::List(items))
            }
            Token::Punct('[') => {
                let mut items = vec![];
                while !self.eat(']') {
                    items.push(self.expression()?);
                    if !self.eat(',') {
                        self.expect(']')?;
                        break;
                    }
                }
                Ok(Expr::List(items))
            }
            Token::Punct('{') => {
                let mut entries = vec![];
                while !self.eat('}') {
                    if self.peek() == Some(&Token::DoubleStar) {
                        self.at += 1;
                        entries.push((None, self.expression()?));
                    } else {
                        let key = self.expression()?;
                        self.expect(':')?;
                        entries.push((Some(key), self.expression()?));
                    }
                    if !self.eat(',') {
                        self.expect('}')?;
                        break;
                    }
                }
                Ok(Expr::Dict(entries))
            }
            other => Err(format!("invalid syntax at {}", other.describe())),
        }
    }

    fn call(&mut self, func: Expr) -> Result<Expr, String> {
        let mut args = vec![];
        let mut keywords = vec![];
        while !self.eat(')') {
            if self.peek() == Some(&Token::DoubleStar) {
                self.at += 1;
                keywords.push((None, self.expression()?));
            } else if let (Some(Token::Name(name)), Some(Token::Punct('='))) = (
                self.tokens.get(self.at).cloned(),
                self.tokens.get(self.at + 1),
            ) {
                self.at += 2;
                keywords.push((Some(name), self.expression()?));
            } else {
                if !keywords.is_empty() {
                    return Err("positional argument follows keyword argument".to_string());
                }
                args.push(self.expression()?);
            }
            if !self.eat(',') {
                self.expect(')')?;
                break;
            }
        }
        Ok(Expr::Call {
            func: Box::new(func),
            args,
            keywords,
        })
    }
}

fn is_keyword(name: &str) -> bool {
    matches!(
        name,
        "and"
            | "as"
            | "assert"
            | "async"
            | "await"
            | "break"
            | "class"
            | "continue"
            | "def"
            | "del"
            | "elif"
            | "else"
            | "except"
            | "finally"
            | "for"
            | "from"
            | "global"
            | "if"
            | "import"
            | "in"
            | "is"
            | "lambda"
            | "nonlocal"
            | "not"
            | "or"
            | "pass"
            | "raise"
            | "return"
            | "try"
            | "while"
            | "with"
            | "yield"
    )
}
