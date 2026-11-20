//! BSX v2: a blok's JSX-like source, parsed into the tree that is stored (the Rust twin of
//! arkitekt-spec's `arkitekt_spec/blok/bsx.py`, held to its corpus `tests/fixtures/bsx_v2.json`).
//!
//! - `attr="..."` is always a literal string, `attr={...}` an expression
//!   ([`crate::blok::expr`]), a bare `attr` is `true`.
//! - Text is a child like any other: each run of text between elements is stored as a `#text`
//!   node holding its `value`. What a component does with it (a `Button` makes it its label) is
//!   its renderer's to say. Several parts of one run desugar to `str.template`. Whitespace
//!   follows JSX.
//! - `className={[...]}` and `className={{...}}` desugar to `str.classes` (clsx).
//! - `<local name="x" .../>` declares `x` (stored as its `name` prop's `declares_value`, like a
//!   `let`); a name that is no plain identifier is refused (`local.attribute`).
//! - Operators desugar to `cmp.*` / `logic.*` calls; a callback holds one task call (`into
//!   name` names its record) or one effect. `queries.<name>.refetch()` and
//!   `queries.<name>.fetch_more(name: value, ...)` desugar to the effects `query.refetch` and
//!   `query.fetch_more`, the query named by a literal.
//!
//! Parsing needs no catalog: no component is looked up. What a catalog says of a tree is
//! checked by whoever draws it, never here.
//! Offsets, lines and columns count Unicode code points, as the spec does.

use std::collections::HashSet;

use regex::Regex;
use serde_json::{Map, Value};

use crate::blok::check::{FETCH_MORE_FUNCTION, QUERY_SUGAR, REFETCH_FUNCTION};
use crate::blok::expr::{parse_attribute, parse_expression, s, CallExpr, Expr, Locator, PICK_VERB};
use crate::blok::vocabulary::vocabulary;
use crate::inputs::{
    ActionArgumentInputModel, AgentProbeInputModel, ComponentNodeInputModel,
    ComponentPropInputModel, DynamicValueInputModel, UtilCallInputModel,
};
use crate::pyjson::repr_str;

pub use crate::blok::expr::BsxError;

/// The largest source a blok may be, in bytes.
pub const MAX_SOURCE_BYTES: usize = 256 * 1024;

/// The node a run of text is stored as: no element can be called this, so only text makes one.
pub const TEXT_NODE: &str = "#text";
/// The prop of a text node that holds its text: a literal, or an expression.
pub const TEXT_VALUE: &str = "value";
const CLASS_NAME: &str = "className";
const CLASSES_FUNCTION: &str = "str.classes";
const TEMPLATE_FUNCTION: &str = "str.template";
const ELSE: &str = "else";
const IF: &str = "if";
const FOREACH: &str = "foreach";
const LOCAL: &str = "local";
const LOCAL_NAME: &str = "name";

type Parsed<T> = Result<T, BsxError>;

// --------------------------------------------------------------------------- markup

#[derive(Debug, Clone)]
enum AttributeValue {
    Text(String),
    Expression(Expr),
    Flag,
}

#[derive(Debug, Clone)]
struct Attribute {
    name: String,
    value: AttributeValue,
    offset: usize,
}

#[derive(Debug, Clone)]
enum Child {
    Element(Element),
    Text { text: String, offset: usize },
    Container { expression: Expr, offset: usize },
}

#[derive(Debug, Clone)]
struct Element {
    name: String,
    offset: usize,
    attributes: Vec<Attribute>,
    children: Vec<Child>,
}

fn name_start(c: char, attribute: bool) -> bool {
    c.is_ascii_alphabetic() || c == '_' || (attribute && c == ':')
}

fn name_part(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-')
}

fn is_local(name: &str) -> bool {
    let mut chars = name.chars();
    chars
        .next()
        .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

fn entity_pattern() -> &'static Regex {
    static PATTERN: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"&(#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")
            .expect("the entity pattern compiles")
    })
}

struct Markup<'a> {
    source: &'a [char],
    locate: &'a Locator,
    index: usize,
    /// The keys of the `<dependencies>` read so far: what a later expression may read through.
    references: HashSet<String>,
}

/// What `<key>.pick(<dependency>: <agent id>)` is stored as.
pub const PICK_FUNCTION: &str = "deps.pick";
const DEPENDENCIES: &str = "dependencies";

impl Markup<'_> {
    /// A `<dependencies key="cells" />` is read through from here on (`cells.microscope`).
    fn declares(&mut self, element: &Element) {
        if element.name != DEPENDENCIES {
            return;
        }
        let key = element.attributes.iter().find(|a| a.name == "key");
        if let Some(AttributeValue::Text(key)) = key.map(|a| &a.value) {
            if is_local(key) {
                self.references.insert(key.clone());
            }
        }
    }

    fn fail(&self, code: &str, offset: Option<usize>, fields: &[(&str, Value)]) -> BsxError {
        self.locate.fail(code, offset.unwrap_or(self.index), fields)
    }

    fn at(&self, text: &str) -> bool {
        (self.index..)
            .zip(text.chars())
            .all(|(i, c)| self.source.get(i) == Some(&c))
    }

    fn find(&self, text: &str, from: usize) -> Option<usize> {
        let pattern: Vec<char> = text.chars().collect();
        (from..self.source.len()).find(|start| self.source[*start..].starts_with(&pattern))
    }

    fn skip_space(&mut self) {
        while self
            .source
            .get(self.index)
            .is_some_and(|c| matches!(c, ' ' | '\t' | '\r' | '\n'))
        {
            self.index += 1;
        }
    }

    fn skip_comment(&mut self) -> Parsed<bool> {
        if !self.at("<!--") {
            return Ok(false);
        }
        let Some(end) = self.find("-->", self.index + 4) else {
            return Err(self.fail(
                "bsx.syntax",
                None,
                &[("detail", s("a comment is never closed"))],
            ));
        };
        self.index = end + 3;
        Ok(true)
    }

    fn skip_trivia(&mut self) -> Parsed<()> {
        loop {
            self.skip_space();
            if !self.skip_comment()? {
                return Ok(());
            }
        }
    }

    fn name_at(&self, index: usize, attribute: bool) -> Option<String> {
        let first = *self.source.get(index)?;
        if !name_start(first, attribute) {
            return None;
        }
        let mut end = index + 1;
        while self.source.get(end).is_some_and(|c| name_part(*c)) {
            end += 1;
        }
        Some(self.source[index..end].iter().collect())
    }

    fn name(&mut self, attribute: bool, what: &str) -> Parsed<String> {
        let Some(name) = self.name_at(self.index, attribute) else {
            return Err(self.fail(
                "bsx.syntax",
                None,
                &[("detail", s(format!("expected {what}")))],
            ));
        };
        self.index += name.chars().count();
        Ok(name)
    }

    fn document(&mut self) -> Parsed<Element> {
        self.skip_trivia()?;
        if self.index >= self.source.len() {
            return Err(self.fail("bsx.empty", None, &[]));
        }
        if !self.at("<") {
            return Err(self.fail(
                "bsx.syntax",
                None,
                &[("detail", s("a blok starts with an element"))],
            ));
        }
        let root = self.element()?;
        self.skip_trivia()?;
        if self.index < self.source.len() {
            if self.at("<") {
                let name = self
                    .name_at(self.index + 1, false)
                    .unwrap_or_else(|| "?".into());
                return Err(self.fail("bsx.multiple_roots", None, &[("component", s(name))]));
            }
            return Err(self.fail(
                "bsx.syntax",
                None,
                &[("detail", s("text after the root element"))],
            ));
        }
        Ok(root)
    }

    fn element(&mut self) -> Parsed<Element> {
        let offset = self.index;
        self.index += 1;
        let name = self.name(false, "an element name")?;
        let mut element = Element {
            name,
            offset,
            attributes: vec![],
            children: vec![],
        };
        let mut seen: HashSet<String> = HashSet::new();
        loop {
            let had_space = self
                .source
                .get(self.index)
                .is_some_and(|c| matches!(c, ' ' | '\t' | '\r' | '\n'));
            self.skip_space();
            if self.index >= self.source.len() {
                return Err(self.fail(
                    "bsx.unclosed",
                    Some(offset),
                    &[("component", s(element.name.clone()))],
                ));
            }
            if self.at("/>") {
                self.index += 2;
                self.declares(&element);
                return Ok(element);
            }
            if self.at(">") {
                self.index += 1;
                self.declares(&element);
                self.content(&mut element)?;
                return Ok(element);
            }
            if !had_space {
                return Err(self.fail(
                    "bsx.syntax",
                    None,
                    &[("detail", s("expected a space before the attribute"))],
                ));
            }
            let attribute = self.attribute()?;
            if !seen.insert(attribute.name.clone()) {
                return Err(self.fail(
                    "bsx.duplicate_attribute",
                    Some(attribute.offset),
                    &[
                        ("component", s(element.name.clone())),
                        ("key", s(attribute.name.clone())),
                    ],
                ));
            }
            element.attributes.push(attribute);
        }
    }

    fn attribute(&mut self) -> Parsed<Attribute> {
        let offset = self.index;
        let name = self.name(true, "an attribute name")?;
        let save = self.index;
        self.skip_space();
        if !self.at("=") {
            self.index = save;
            return Ok(Attribute {
                name,
                value: AttributeValue::Flag,
                offset,
            });
        }
        self.index += 1;
        self.skip_space();
        if self.at("\"") || self.at("'") {
            let quote = self.source[self.index];
            let Some(end) = (self.index + 1..self.source.len()).find(|i| self.source[*i] == quote)
            else {
                return Err(self.fail(
                    "bsx.syntax",
                    None,
                    &[("detail", s(format!("the value of {name} is never closed")))],
                ));
            };
            let raw: String = self.source[self.index + 1..end].iter().collect();
            let value = self.decode(&raw, self.index + 1)?;
            self.index = end + 1;
            return Ok(Attribute {
                name,
                value: AttributeValue::Text(value),
                offset,
            });
        }
        if self.at("{") {
            let (start, end) = self.braces()?;
            let inner = &self.source[start..end];
            self.index = end + 1;
            if inner.iter().all(|c| c.is_whitespace()) {
                return Err(self.fail(
                    "expr.syntax",
                    Some(start),
                    &[("detail", s(format!("the value of {name} is empty")))],
                ));
            }
            let expression = parse_attribute(inner, start, self.locate, &self.references)?;
            return Ok(Attribute {
                name,
                value: AttributeValue::Expression(expression),
                offset,
            });
        }
        Err(self.fail(
            "bsx.syntax",
            None,
            &[(
                "detail",
                s(format!("{name}= takes \"a string\" or {{an expression}}")),
            )],
        ))
    }

    /// The span inside the braces opening at the cursor: strings and nested braces skipped.
    fn braces(&self) -> Parsed<(usize, usize)> {
        let source = self.source;
        let start = self.index + 1;
        let mut depth = 1;
        let mut index = start;
        while index < source.len() {
            let c = source[index];
            if c == '"' || c == '\'' {
                index += 1;
                while index < source.len() && source[index] != c {
                    if source[index] == '\\' {
                        index += 1;
                    }
                    if source.get(index).is_none_or(|n| *n == '\n') {
                        break;
                    }
                    index += 1;
                }
                index += 1;
                continue;
            }
            if c == '{' {
                depth += 1;
            } else if c == '}' {
                depth -= 1;
                if depth == 0 {
                    return Ok((start, index));
                }
            }
            index += 1;
        }
        Err(self.fail(
            "bsx.syntax",
            Some(self.index),
            &[("detail", s("an expression's braces are never closed"))],
        ))
    }

    fn content(&mut self, element: &mut Element) -> Parsed<()> {
        loop {
            if self.index >= self.source.len() {
                return Err(self.fail(
                    "bsx.unclosed",
                    Some(element.offset),
                    &[("component", s(element.name.clone()))],
                ));
            }
            if self.at("</") {
                let offset = self.index;
                self.index += 2;
                let found = self.name(false, "an element name")?;
                self.skip_space();
                if !self.at(">") {
                    return Err(self.fail(
                        "bsx.syntax",
                        None,
                        &[("detail", s(format!("expected '>' to close </{found}")))],
                    ));
                }
                self.index += 1;
                if found != element.name {
                    return Err(self.fail(
                        "bsx.mismatched_tag",
                        Some(offset),
                        &[("found", s(found)), ("expected", s(element.name.clone()))],
                    ));
                }
                return Ok(());
            }
            if self.skip_comment()? {
                continue;
            }
            if self.at("<") {
                let child = self.element()?;
                element.children.push(Child::Element(child));
                continue;
            }
            if self.at("{") {
                let offset = self.index;
                let (start, end) = self.braces()?;
                let inner = &self.source[start..end];
                self.index = end + 1;
                let text: String = inner.iter().collect();
                let stripped = text.trim();
                if stripped.is_empty() || (stripped.starts_with("/*") && stripped.ends_with("*/")) {
                    continue;
                }
                let expression = parse_expression(inner, start, self.locate, &self.references)?;
                element
                    .children
                    .push(Child::Container { expression, offset });
                continue;
            }
            if self.at("}") {
                return Err(self.fail(
                    "bsx.syntax",
                    None,
                    &[("detail", s("unexpected '}' in text; write {'}'}"))],
                ));
            }
            let offset = self.index;
            let end = (self.index..self.source.len())
                .find(|i| matches!(self.source[*i], '<' | '{' | '}'))
                .unwrap_or(self.source.len());
            let raw: String = self.source[self.index..end].iter().collect();
            self.index = end;
            element.children.push(Child::Text { text: raw, offset });
        }
    }

    fn decode(&self, raw: &str, offset: usize) -> Parsed<String> {
        let mut out = String::with_capacity(raw.len());
        let mut last = 0;
        for found in entity_pattern().captures_iter(raw) {
            let whole = found.get(0).expect("a match has its whole");
            let entity = &found[1];
            out.push_str(&raw[last..whole.start()]);
            let at = offset + raw[..whole.start()].chars().count();
            let decoded = if let Some(hex) = entity.strip_prefix("#x") {
                u32::from_str_radix(hex, 16).ok().and_then(char::from_u32)
            } else if let Some(decimal) = entity.strip_prefix('#') {
                decimal.parse::<u32>().ok().and_then(char::from_u32)
            } else {
                match entity {
                    "amp" => Some('&'),
                    "lt" => Some('<'),
                    "gt" => Some('>'),
                    "quot" => Some('"'),
                    "apos" => Some('\''),
                    "nbsp" => Some('\u{a0}'),
                    _ => None,
                }
            };
            let Some(decoded) = decoded else {
                return Err(self.fail(
                    "bsx.syntax",
                    Some(at),
                    &[("detail", s(format!("unknown entity &{entity};")))],
                ));
            };
            out.push(decoded);
            last = whole.end();
        }
        out.push_str(&raw[last..]);
        Ok(out)
    }
}

/// JSX's whitespace rule for one run of text: lines trimmed where they break, whitespace-only
/// lines dropped, the rest joined by one space; runs of spaces in a line collapse to one.
pub fn jsx_text(raw: &str) -> String {
    let normalized = raw.replace("\r\n", "\n").replace('\r', "\n");
    let lines: Vec<&str> = normalized.split('\n').collect();
    let last = lines.len() - 1;
    let mut kept: Vec<String> = vec![];
    for (index, line) in lines.iter().enumerate() {
        let mut text = line.replace('\t', " ");
        if index > 0 {
            text = text.trim_start_matches(' ').to_owned();
        }
        if index < last {
            text = text.trim_end_matches(' ').to_owned();
        }
        let mut collapsed = String::with_capacity(text.len());
        let mut previous_space = false;
        for c in text.chars() {
            if c == ' ' && previous_space {
                continue;
            }
            previous_space = c == ' ';
            collapsed.push(c);
        }
        if !collapsed.is_empty() {
            kept.push(collapsed);
        }
    }
    kept.join(" ")
}

// --------------------------------------------------------------------------- lowering

#[derive(Debug, Clone)]
enum Part {
    Text(String),
    Expression(Expr),
}

enum Run<'e> {
    Text(Vec<Part>),
    Element(&'e Element),
}

fn prop(key: &str) -> ComponentPropInputModel {
    ComponentPropInputModel {
        key: key.to_owned(),
        static_value: None,
        dynamic_value: None,
        declares_value: None,
        agent_call: None,
        util_call: None,
    }
}

fn argument(key: Option<&str>) -> ActionArgumentInputModel {
    ActionArgumentInputModel {
        key: key.map(str::to_owned),
        value_literal: None,
        value_path: None,
        agent_call: None,
        util_call: None,
        value_list: None,
        value_dict: None,
    }
}

fn some_if_any<T>(items: Vec<T>) -> Option<Vec<T>> {
    (!items.is_empty()).then_some(items)
}

fn is_agent_call(call: &CallExpr) -> bool {
    let callee = &call.callee;
    (callee[0] == "self" && callee.len() == 2) || (callee[0] == "deps" && callee.len() == 3)
}

struct Lowering<'a> {
    locate: &'a Locator,
    markup: &'a Markup<'a>,
    ids: HashSet<String>,
}

impl Lowering<'_> {
    fn fail(&self, code: &str, offset: usize, fields: &[(&str, Value)]) -> BsxError {
        self.locate.fail(code, offset, fields)
    }

    fn node(
        &mut self,
        element: &Element,
        node_id: &str,
        parent: Option<&str>,
    ) -> Parsed<ComponentNodeInputModel> {
        let vocabulary = vocabulary();
        if element.name == ELSE && parent != Some(IF) {
            return Err(self.fail("bsx.else_outside_if", element.offset, &[]));
        }
        let mut node_id = node_id.to_owned();
        if let Some(explicit) = element.attributes.iter().find(|a| a.name == "id") {
            let written = match &explicit.value {
                AttributeValue::Text(text) if vocabulary.is_explicit_id(text) => text.clone(),
                AttributeValue::Text(text) => {
                    return Err(self.fail("bsx.id", explicit.offset, &[("id", s(text.clone()))]));
                }
                _ => return Err(self.fail("bsx.id", explicit.offset, &[("id", s("{...}"))])),
            };
            if !self.ids.insert(written.clone()) {
                return Err(self.fail("bsx.duplicate_id", explicit.offset, &[("id", s(written))]));
            }
            node_id = written;
        }

        let mut props = vec![];
        for attribute in element.attributes.iter().filter(|a| a.name != "id") {
            props.push(self.prop(&element.name, attribute)?);
        }
        if element.name == FOREACH {
            self.check_foreach(element)?;
        }

        let runs = self.runs(element)?;

        let mut children = vec![];
        let mut implicit = 0;
        let mut counts: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
        let mut seen_else = false;
        for run in &runs {
            match run {
                Run::Text(parts) => {
                    if seen_else {
                        return Err(self.fail("bsx.else_order", element.offset, &[]));
                    }
                    let text = self.text_prop(TEXT_VALUE, parts, element.offset)?;
                    children.push(ComponentNodeInputModel {
                        id: format!("{node_id}/#{implicit}"),
                        component: TEXT_NODE.to_owned(),
                        props: Some(vec![text]),
                        children: None,
                    });
                    implicit += 1;
                }
                Run::Element(child) => {
                    if element.name == IF {
                        if child.name == ELSE {
                            seen_else = true;
                        } else if seen_else {
                            return Err(self.fail("bsx.else_order", child.offset, &[]));
                        }
                    }
                    let index = counts.entry(child.name.clone()).or_insert(0);
                    let child_id = format!("{node_id}/{}[{index}]", child.name);
                    *index += 1;
                    children.push(self.node(child, &child_id, Some(&element.name))?);
                }
            }
        }
        if element.name == IF {
            let elses: Vec<&Element> = element
                .children
                .iter()
                .filter_map(|c| match c {
                    Child::Element(e) if e.name == ELSE => Some(e),
                    _ => None,
                })
                .collect();
            if let Some((_, leading)) = elses.split_last() {
                for other in leading {
                    if !other.attributes.iter().any(|a| a.name == "when") {
                        return Err(self.fail("bsx.else_order", other.offset, &[]));
                    }
                }
            }
        }

        Ok(ComponentNodeInputModel {
            id: node_id,
            component: element.name.clone(),
            props: some_if_any(props),
            children: some_if_any(children),
        })
    }

    fn check_foreach(&self, element: &Element) -> Parsed<()> {
        let get = |name: &str| element.attributes.iter().find(|a| a.name == name);
        let missing: Vec<&str> = [("items", "items={...}"), ("let", "let=\"name\"")]
            .iter()
            .filter(|(name, _)| get(name).is_none())
            .map(|(_, spelled)| *spelled)
            .collect();
        if !missing.is_empty() {
            return Err(self.fail(
                "bsx.foreach",
                element.offset,
                &[("missing", s(missing.join(", ")))],
            ));
        }
        let let_ = get("let").expect("checked above");
        match &let_.value {
            AttributeValue::Text(name) if is_local(name) => {}
            AttributeValue::Text(name) => {
                return Err(self.fail("bsx.let", let_.offset, &[("let", s(name.clone()))]))
            }
            _ => return Err(self.fail("bsx.let", let_.offset, &[("let", s("{...}"))])),
        }
        // The index is optional: a name for the item's position.
        if let Some(index) = get("index") {
            match &index.value {
                AttributeValue::Text(name) if is_local(name) => {}
                AttributeValue::Text(name) => {
                    return Err(self.fail("bsx.index", index.offset, &[("index", s(name.clone()))]))
                }
                _ => return Err(self.fail("bsx.index", index.offset, &[("index", s("{...}"))])),
            }
        }
        // The key is optional (items are then keyed by their position); given, it is an expression.
        for attribute in ["items", "key"].into_iter().filter_map(get) {
            if !matches!(attribute.value, AttributeValue::Expression(_)) {
                let name = &attribute.name;
                return Err(self.fail(
                    "bsx.foreach",
                    attribute.offset,
                    &[("missing", s(format!("{name}={{...}}, an expression")))],
                ));
            }
        }
        Ok(())
    }

    /// The children as text runs (parts between elements) and elements, in order.
    fn runs<'e>(&self, element: &'e Element) -> Parsed<Vec<Run<'e>>> {
        let mut runs = vec![];
        let mut current: Vec<Part> = vec![];
        let flush = |current: &mut Vec<Part>, runs: &mut Vec<Run<'e>>| {
            let mut merged: Vec<Part> = vec![];
            for part in current.drain(..) {
                match (part, merged.last_mut()) {
                    (Part::Text(text), Some(Part::Text(previous))) => previous.push_str(&text),
                    (Part::Text(text), _) if text.is_empty() => {}
                    (part, _) => merged.push(part),
                }
            }
            if !merged.is_empty() {
                runs.push(Run::Text(merged));
            }
        };
        for child in &element.children {
            match child {
                Child::Element(inner) => {
                    flush(&mut current, &mut runs);
                    runs.push(Run::Element(inner));
                }
                Child::Text { text, offset } => {
                    let text = self.markup.decode(&jsx_text(text), *offset)?;
                    if !text.is_empty() {
                        current.push(Part::Text(text));
                    }
                }
                Child::Container { expression, offset } => match expression {
                    Expr::Literal {
                        value: Value::String(text),
                        ..
                    } => current.push(Part::Text(text.clone())),
                    other => {
                        self.check_pure(other, *offset, true)?;
                        current.push(Part::Expression(other.clone()));
                    }
                },
            }
        }
        flush(&mut current, &mut runs);
        Ok(runs)
    }

    fn text_prop(
        &self,
        key: &str,
        parts: &[Part],
        offset: usize,
    ) -> Parsed<ComponentPropInputModel> {
        if let [only] = parts {
            return match only {
                Part::Text(text) => Ok(ComponentPropInputModel {
                    static_value: Some(s(text.clone())),
                    ..prop(key)
                }),
                Part::Expression(expression) => self.value(key, expression, offset),
            };
        }
        let mut template = String::new();
        let mut values = vec![];
        for part in parts {
            match part {
                Part::Text(text) => template.push_str(&text.replace('{', "{{").replace('}', "}}")),
                Part::Expression(expression) => {
                    template.push_str(&format!("{{{}}}", values.len()));
                    values.push(self.argument(None, expression)?);
                }
            }
        }
        let call = UtilCallInputModel {
            operation: TEMPLATE_FUNCTION.to_owned(),
            arguments: Some(vec![
                ActionArgumentInputModel {
                    value_literal: Some(s(template)),
                    ..argument(Some("template"))
                },
                ActionArgumentInputModel {
                    value_list: Some(values),
                    ..argument(Some("values"))
                },
            ]),
        };
        Ok(ComponentPropInputModel {
            util_call: Some(call),
            ..prop(key)
        })
    }

    // -- props

    fn prop(&self, component: &str, attribute: &Attribute) -> Parsed<ComponentPropInputModel> {
        let key = attribute.name.as_str();
        // `<local name="x">` declares `x` to its parent's subtree, as a `let` does to its loop.
        if component == LOCAL && key == LOCAL_NAME {
            return match &attribute.value {
                AttributeValue::Text(name) if is_local(name) => Ok(ComponentPropInputModel {
                    static_value: Some(s(name.clone())),
                    declares_value: Some(name.clone()),
                    ..prop(key)
                }),
                other => {
                    let written = match other {
                        AttributeValue::Text(text) => text.clone(),
                        _ => "{...}".to_owned(),
                    };
                    Err(self.fail(
                        "local.attribute",
                        attribute.offset,
                        &[
                            ("name", s(written)),
                            ("key", s(LOCAL_NAME)),
                            (
                                "problem",
                                s("is not a name: write name=\"x\", a plain identifier"),
                            ),
                        ],
                    ))
                }
            };
        }
        match &attribute.value {
            AttributeValue::Flag => Ok(ComponentPropInputModel {
                static_value: Some(Value::Bool(true)),
                ..prop(key)
            }),
            AttributeValue::Text(text) => Ok(ComponentPropInputModel {
                static_value: Some(s(text.clone())),
                declares_value: ((key == "let" || key == "index") && component == FOREACH)
                    .then(|| text.clone()),
                ..prop(key)
            }),
            AttributeValue::Expression(_) if key == "let" && component == FOREACH => {
                Err(self.fail("bsx.let", attribute.offset, &[("let", s("{...}"))]))
            }
            AttributeValue::Expression(_) if key == "index" && component == FOREACH => {
                Err(self.fail("bsx.index", attribute.offset, &[("index", s("{...}"))]))
            }
            AttributeValue::Expression(expression) => self.value(key, expression, attribute.offset),
        }
    }

    fn value(
        &self,
        key: &str,
        expression: &Expr,
        _offset: usize,
    ) -> Parsed<ComponentPropInputModel> {
        match expression {
            Expr::Into { call, name, offset } => {
                let Some(agent) = self.agent_call(call)? else {
                    return Err(self.fail("callback.into", *offset, &[]));
                };
                Ok(ComponentPropInputModel {
                    agent_call: Some(agent),
                    declares_value: Some(name.clone()),
                    ..prop(key)
                })
            }
            Expr::Literal { value, .. } => Ok(ComponentPropInputModel {
                static_value: (!value.is_null()).then(|| value.clone()),
                ..prop(key)
            }),
            Expr::List { .. } | Expr::Object { .. } => {
                if key == CLASS_NAME {
                    let call = UtilCallInputModel {
                        operation: CLASSES_FUNCTION.to_owned(),
                        arguments: Some(vec![self.argument(Some("values"), expression)?]),
                    };
                    return Ok(ComponentPropInputModel {
                        util_call: Some(call),
                        ..prop(key)
                    });
                }
                let Some(literal) = literal(expression) else {
                    return Err(self.fail("expr.dynamic_container", expression.offset(), &[]));
                };
                Ok(ComponentPropInputModel {
                    static_value: Some(literal),
                    ..prop(key)
                })
            }
            Expr::Path { segments, .. } => Ok(ComponentPropInputModel {
                dynamic_value: Some(DynamicValueInputModel {
                    literal: None,
                    path: Some(segments.join(".")),
                }),
                ..prop(key)
            }),
            Expr::Call(call) => {
                if let Some(agent) = self.agent_call(call)? {
                    return Ok(ComponentPropInputModel {
                        agent_call: Some(agent),
                        ..prop(key)
                    });
                }
                let effect = match self.pick_effect(call)? {
                    Some(effect) => Some(effect),
                    None => self.query_effect(call)?,
                };
                if let Some(effect) = effect {
                    return Ok(ComponentPropInputModel {
                        util_call: Some(effect),
                        ..prop(key)
                    });
                }
                Ok(ComponentPropInputModel {
                    util_call: Some(self.util_call(call, true)?),
                    ..prop(key)
                })
            }
            other => Ok(ComponentPropInputModel {
                util_call: Some(self.operator(other)?),
                ..prop(key)
            }),
        }
    }

    // -- calls

    /// A task call (`self.<action>(...)`, `deps.<key>.<action>(...)`), or none.
    fn agent_call(&self, call: &CallExpr) -> Parsed<Option<AgentProbeInputModel>> {
        let callee = &call.callee;
        let (dependency, operation) = if callee[0] == "self" && callee.len() == 2 {
            ("self".to_owned(), callee[1].clone())
        } else if callee[0] == "deps" && callee.len() == 3 {
            (callee[1].clone(), callee[2].clone())
        } else {
            if call.given.is_some() {
                return Err(self.fail("callback.with", call.offset, &[]));
            }
            return Ok(None);
        };
        if call.given.is_some() && dependency != "self" {
            return Err(self.fail("callback.with", call.offset, &[]));
        }
        let mut arguments = vec![];
        let mut seen = HashSet::new();
        for given in &call.arguments {
            let Some(name) = &given.name else {
                return Err(self.fail(
                    "callback.positional",
                    given.offset,
                    &[("call", s(call.name()))],
                ));
            };
            if !seen.insert(name.clone()) {
                return Err(self.fail(
                    "expr.duplicate_key",
                    given.offset,
                    &[("key", s(name.clone()))],
                ));
            }
            arguments.push(self.argument(Some(name), &given.value)?);
        }
        Ok(Some(AgentProbeInputModel {
            dependency,
            operation,
            arguments: some_if_any(arguments),
            given: call.given.clone(),
        }))
    }

    /// `cells.pick(microscope: id, ...)` as its effect (`deps.pick`), or none.
    fn pick_effect(&self, call: &CallExpr) -> Parsed<Option<UtilCallInputModel>> {
        let callee = &call.callee;
        if callee.len() != 2
            || callee[1] != PICK_VERB
            || !self.markup.references.contains(&callee[0])
        {
            return Ok(None);
        }
        let mut picks = vec![];
        let mut seen = HashSet::new();
        for given in &call.arguments {
            let Some(name) = &given.name else {
                return Err(self.fail(
                    "callback.positional",
                    given.offset,
                    &[("call", s(call.name()))],
                ));
            };
            if !seen.insert(name.clone()) {
                return Err(self.fail(
                    "expr.duplicate_key",
                    given.offset,
                    &[("key", s(name.clone()))],
                ));
            }
            picks.push(self.argument(Some(name), &given.value)?);
        }
        if picks.is_empty() {
            return Err(self.fail(
                "expr.arguments",
                call.offset,
                &[
                    ("function", s(call.name())),
                    (
                        "detail",
                        s("pick names the dependency to bind: pick(<dependency>: <agent id>)"),
                    ),
                ],
            ));
        }
        let named = ActionArgumentInputModel {
            value_literal: Some(s(callee[0].clone())),
            ..argument(Some("key"))
        };
        let given = ActionArgumentInputModel {
            value_dict: Some(picks),
            ..argument(Some("picks"))
        };
        Ok(Some(UtilCallInputModel {
            operation: PICK_FUNCTION.to_owned(),
            arguments: Some(vec![named, given]),
        }))
    }

    /// `queries.<name>.refetch()` / `.fetch_more(k: v, ...)` as its effect, or none.
    fn query_effect(&self, call: &CallExpr) -> Parsed<Option<UtilCallInputModel>> {
        let callee = &call.callee;
        if callee.len() != 3 || !is_query_verb(&callee[2]) {
            return Ok(None);
        }
        if callee[0] == "subscriptions" {
            return Err(self.fail("subscription.refetch", call.offset, &[]));
        }
        if callee[0] != "queries" {
            return Ok(None);
        }
        let named = ActionArgumentInputModel {
            value_literal: Some(s(callee[1].clone())),
            ..argument(Some("query"))
        };
        if callee[2] == "refetch" {
            if !call.arguments.is_empty() {
                return Err(self.fail(
                    "expr.arguments",
                    call.offset,
                    &[
                        ("function", s(call.name())),
                        ("detail", s("refetch takes no arguments")),
                    ],
                ));
            }
            return Ok(Some(UtilCallInputModel {
                operation: REFETCH_FUNCTION.into(),
                arguments: Some(vec![named]),
            }));
        }
        let mut variables = vec![];
        let mut seen = HashSet::new();
        for given in &call.arguments {
            let Some(name) = &given.name else {
                return Err(self.fail(
                    "callback.positional",
                    given.offset,
                    &[("call", s(call.name()))],
                ));
            };
            if !seen.insert(name.clone()) {
                return Err(self.fail(
                    "expr.duplicate_key",
                    given.offset,
                    &[("key", s(name.clone()))],
                ));
            }
            variables.push(self.argument(Some(name), &given.value)?);
        }
        let mut arguments = vec![named];
        if !variables.is_empty() {
            arguments.push(ActionArgumentInputModel {
                value_dict: Some(variables),
                ..argument(Some("variables"))
            });
        }
        Ok(Some(UtilCallInputModel {
            operation: FETCH_MORE_FUNCTION.into(),
            arguments: Some(arguments),
        }))
    }

    fn util_call(&self, call: &CallExpr, effect_allowed: bool) -> Parsed<UtilCallInputModel> {
        let name = call.name();
        let vocabulary = vocabulary();
        let function = vocabulary.functions.get(&name);
        let named_fields =
            |detail: String| vec![("function", s(name.clone())), ("detail", s(detail))];
        match function {
            None => {
                if !vocabulary.function_namespaces.contains(&call.callee[0])
                    || call.callee.len() < 2
                {
                    return Err(self.fail(
                        "expr.unknown_callee",
                        call.offset,
                        &[("callee", s(name.clone()))],
                    ));
                }
            }
            Some(function) if function.effect && !effect_allowed => {
                return Err(self.fail(
                    "callback.nested",
                    call.offset,
                    &[("what", s(name.clone()))],
                ));
            }
            Some(_) => {}
        }
        let positional: Vec<&crate::blok::expr::Argument> =
            call.arguments.iter().filter(|a| a.name.is_none()).collect();
        let named: Vec<&crate::blok::expr::Argument> =
            call.arguments.iter().filter(|a| a.name.is_some()).collect();
        let first_named = call.arguments.iter().position(|a| a.name.is_some());
        let last_positional = call.arguments.iter().rposition(|a| a.name.is_none());
        if let (Some(first_named), Some(last_positional)) = (first_named, last_positional) {
            if first_named < last_positional {
                return Err(self.fail(
                    "expr.arguments",
                    call.offset,
                    &named_fields("a positional argument follows a named one".into()),
                ));
            }
        }
        let Some(function) = function else {
            let mut arguments = vec![];
            for (index, given) in positional.iter().enumerate() {
                arguments.push(self.argument(Some(&index.to_string()), &given.value)?);
            }
            for given in &named {
                arguments.push(self.argument(given.name.as_deref(), &given.value)?);
            }
            return Ok(UtilCallInputModel {
                operation: name.clone(),
                arguments: some_if_any(arguments),
            });
        };
        let keys = function.keys();
        let mut resolved: Vec<(String, ActionArgumentInputModel)> = vec![];
        if keys == ["values"] && positional.len() > 1 {
            let mut values = vec![];
            for given in &positional {
                values.push(self.argument(None, &given.value)?);
            }
            resolved.push((
                "values".into(),
                ActionArgumentInputModel {
                    value_list: Some(values),
                    ..argument(Some("values"))
                },
            ));
        } else {
            if positional.len() > keys.len() {
                return Err(self.fail(
                    "expr.arguments",
                    call.offset,
                    &named_fields(format!(
                        "takes at most {} arguments ({})",
                        keys.len(),
                        keys.join(", ")
                    )),
                ));
            }
            for (key, given) in keys.iter().zip(&positional) {
                resolved.push(((*key).to_owned(), self.argument(Some(key), &given.value)?));
            }
        }
        for given in &named {
            let argument_name = given.name.as_deref().unwrap_or_default();
            if !keys.contains(&argument_name) {
                return Err(self.fail(
                    "expr.arguments",
                    given.offset,
                    &named_fields(format!(
                        "has no argument {} ({})",
                        repr_str(argument_name),
                        keys.join(", ")
                    )),
                ));
            }
            if resolved.iter().any(|(key, _)| key == argument_name) {
                return Err(self.fail(
                    "expr.arguments",
                    given.offset,
                    &named_fields(format!("{} is given twice", repr_str(argument_name))),
                ));
            }
            resolved.push((
                argument_name.to_owned(),
                self.argument(Some(argument_name), &given.value)?,
            ));
        }
        let missing: Vec<&str> = function
            .required_keys()
            .into_iter()
            .filter(|key| !resolved.iter().any(|(k, _)| k == key))
            .collect();
        if !missing.is_empty() {
            return Err(self.fail(
                "expr.arguments",
                call.offset,
                &named_fields(format!("needs {}", missing.join(", "))),
            ));
        }
        let mut ordered = vec![];
        for key in &keys {
            if let Some(index) = resolved.iter().position(|(k, _)| k == key) {
                ordered.push(resolved.remove(index).1);
            }
        }
        Ok(UtilCallInputModel {
            operation: name,
            arguments: some_if_any(ordered),
        })
    }

    fn operator(&self, expression: &Expr) -> Parsed<UtilCallInputModel> {
        let operators = &vocabulary().operators;
        let operation = |symbol: &str| {
            operators
                .get(symbol)
                .cloned()
                .unwrap_or_else(|| symbol.to_owned())
        };
        Ok(match expression {
            Expr::Not { operand, .. } => UtilCallInputModel {
                operation: operation("!"),
                arguments: Some(vec![self.argument(Some("a"), operand)?]),
            },
            Expr::Compare {
                operator,
                left,
                right,
                ..
            } => UtilCallInputModel {
                operation: operation(operator),
                arguments: Some(vec![
                    self.argument(Some("a"), left)?,
                    self.argument(Some("b"), right)?,
                ]),
            },
            Expr::Logical {
                operator, operands, ..
            } => {
                let mut values = vec![];
                for operand in operands {
                    values.push(self.argument(None, operand)?);
                }
                UtilCallInputModel {
                    operation: operation(operator),
                    arguments: Some(vec![ActionArgumentInputModel {
                        value_list: Some(values),
                        ..argument(Some("values"))
                    }]),
                }
            }
            Expr::Ternary {
                cond,
                then,
                otherwise,
                ..
            } => UtilCallInputModel {
                operation: operation("?:"),
                arguments: Some(vec![
                    self.argument(Some("cond"), cond)?,
                    self.argument(Some("then"), then)?,
                    self.argument(Some("otherwise"), otherwise)?,
                ]),
            },
            other => {
                return Err(self.fail(
                    "expr.syntax",
                    other.offset(),
                    &[("detail", s("expected an operator"))],
                ));
            }
        })
    }

    /// An expression as a call's argument; task calls, refetches and effects are refused.
    fn argument(&self, key: Option<&str>, expression: &Expr) -> Parsed<ActionArgumentInputModel> {
        Ok(match expression {
            Expr::Literal { value, .. } => ActionArgumentInputModel {
                value_literal: (!value.is_null()).then(|| value.clone()),
                ..argument(key)
            },
            Expr::Path { segments, .. } => ActionArgumentInputModel {
                value_path: Some(segments.join(".")),
                ..argument(key)
            },
            Expr::List { items, .. } => {
                let mut values = vec![];
                for item in items {
                    values.push(self.argument(None, item)?);
                }
                ActionArgumentInputModel {
                    value_list: Some(values),
                    ..argument(key)
                }
            }
            Expr::Object { entries, .. } => {
                let mut values = vec![];
                for (name, item) in entries {
                    values.push(self.argument(Some(name), item)?);
                }
                ActionArgumentInputModel {
                    value_dict: Some(values),
                    ..argument(key)
                }
            }
            Expr::Into { offset, .. } => return Err(self.fail("callback.into", *offset, &[])),
            Expr::Call(call) => {
                self.check_pure(expression, call.offset, false)?;
                ActionArgumentInputModel {
                    util_call: Some(self.util_call(call, false)?),
                    ..argument(key)
                }
            }
            other => ActionArgumentInputModel {
                util_call: Some(self.operator(other)?),
                ..argument(key)
            },
        })
    }

    /// Refuse a task call or an effect (a query's refetch or fetch_more too) where only a value
    /// may be.
    fn check_pure(&self, expression: &Expr, offset: usize, text: bool) -> Parsed<()> {
        match expression {
            Expr::Call(call) => {
                let effect = vocabulary()
                    .functions
                    .get(&call.name())
                    .is_some_and(|f| f.effect);
                let sugar = call.callee.len() == 3
                    && (call.callee[0] == "queries" || call.callee[0] == "subscriptions")
                    && is_query_verb(&call.callee[2]);
                let command = is_agent_call(call) || sugar;
                if command || effect {
                    if text {
                        return Err(self.fail("bsx.text_callback", offset, &[]));
                    }
                    let what = if command {
                        format!("{}(...)", call.name())
                    } else {
                        call.name()
                    };
                    return Err(self.fail("callback.nested", call.offset, &[("what", s(what))]));
                }
                for given in &call.arguments {
                    self.check_pure(&given.value, given.offset, text)?;
                }
                Ok(())
            }
            Expr::Into { call, .. } => self.check_pure(&Expr::Call(call.clone()), offset, text),
            other => {
                for inner in other.children() {
                    self.check_pure(inner, offset, text)?;
                }
                Ok(())
            }
        }
    }
}

/// Whether `verb` is one of the query sugar's (`refetch`, `fetch_more`).
fn is_query_verb(verb: &str) -> bool {
    QUERY_SUGAR.iter().any(|(sugar, _)| *sugar == verb)
}

/// The JSON value an all-literal expression is, or none when it holds an expression.
fn literal(expression: &Expr) -> Option<Value> {
    match expression {
        Expr::Literal { value, .. } => Some(value.clone()),
        Expr::List { items, .. } => items
            .iter()
            .map(literal)
            .collect::<Option<Vec<_>>>()
            .map(Value::Array),
        Expr::Object { entries, .. } => entries
            .iter()
            .map(|(name, item)| literal(item).map(|value| (name.clone(), value)))
            .collect::<Option<Map<String, Value>>>()
            .map(Value::Object),
        _ => None,
    }
}

/// Parse a v2 BSX source into the tree a blok stores. No catalog is read: text is stored as
/// `#text` nodes, and what a component does with it is its renderer's to say.
pub fn parse_bsx(source: &str) -> Result<ComponentNodeInputModel, BsxError> {
    if source.len() > MAX_SOURCE_BYTES {
        return Err(BsxError::new(
            "bsx.syntax",
            None,
            &[(
                "detail",
                s(format!(
                    "a blok's source is at most {MAX_SOURCE_BYTES} bytes"
                )),
            )],
        ));
    }
    let chars: Vec<char> = source.chars().collect();
    let locate = Locator::new(&chars);
    let mut markup = Markup {
        source: &chars,
        locate: &locate,
        index: 0,
        references: HashSet::new(),
    };
    let root = markup.document()?;
    let mut lowering = Lowering {
        locate: &locate,
        markup: &markup,
        ids: HashSet::new(),
    };
    lowering.node(&root, &format!("{}[0]", root.name), None)
}

/// [`parse_bsx`], under the name it had while a catalog placed the text.
pub fn parse_bsx_default(source: &str) -> Result<ComponentNodeInputModel, BsxError> {
    parse_bsx(source)
}

/// An expression (`args.slide.id`) as a call argument is stored: a query's variable.
pub fn parse_argument(key: Option<&str>, text: &str) -> Result<ActionArgumentInputModel, BsxError> {
    let chars: Vec<char> = text.chars().collect();
    let locate = Locator::new(&chars);
    let expression = parse_expression(&chars, 0, &locate, &HashSet::new())?;
    let markup = Markup {
        source: &chars,
        locate: &locate,
        index: 0,
        references: HashSet::new(),
    };
    let lowering = Lowering {
        locate: &locate,
        markup: &markup,
        ids: HashSet::new(),
    };
    lowering.argument(key, &expression)
}
