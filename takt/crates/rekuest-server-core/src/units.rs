//! QUANTITY-port unit semantics (`rekuest_core/units.py`): the dimensionality of a unit or
//! dimension expression, as pint's default registry computes it, rendered canonically.
//!
//! This reproduces what the Python server gets from `pint.UnitRegistry().get_dimensionality`
//! (pint 0.25.3), for dimensionality only (no scale factors):
//!
//! * the registry is pint's own `default_en.txt` and `constants_en.txt` (embedded, BSD licence in
//!   `units/PINT_LICENSE`): prefixes, base and derived units, derived dimensions, `@group`
//!   blocks and `@alias`; `@context`, `@system` and `@defaults` blocks carry no definitions;
//!   offset units get their `delta_` twins;
//! * an expression goes through pint's `string_preprocessor`, a Python-style tokenizer and
//!   pint's evaluation tree (`pint_eval`), exactly as `ParserHelper.from_string` does;
//! * unit names resolve like `PlainRegistry.get_name`: direct lookup, else every prefix and
//!   plural `s`, preferring the prefixed reading; a prefix on an offset or logarithmic unit is
//!   refused;
//! * a result with units whose numeric scale is not 1 is refused, because pint's
//!   dimensionality cache hashes it and a scaled `ParserHelper` is not hashable (so `2 m` is
//!   an error while `2` is dimensionless).

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, LazyLock};

use regex::Regex;

/// The sentinel of an empty dimensionality.
pub const DIMENSIONLESS: &str = "dimensionless";

const DEFAULT_EN: &str = include_str!("units/default_en.txt");
const CONSTANTS_EN: &str = include_str!("units/constants_en.txt");

/// A unit or dimension expression the registry cannot resolve.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("Unknown or unparseable unit '{expression}': {reason}")]
pub struct UnitError {
    pub expression: String,
    pub reason: String,
}

/// Names (units or `[dimensions]`) with their exponents.
type Container = BTreeMap<String, f64>;

struct UnitDefinition {
    /// The canonical name.
    name: String,
    /// What the unit is defined as: units, or dimensions for a base unit.
    reference: Container,
    multiplicative: bool,
}

enum Dimension {
    Base,
    Derived(Container),
}

/// pint's default unit registry, reduced to what dimensionality needs (`get_unit_registry`).
pub struct UnitRegistry {
    units: HashMap<String, Arc<UnitDefinition>>,
    /// Every prefix key (names, symbols, aliases), in pint's insertion order, with its name.
    prefixes: Vec<(String, String)>,
    prefix_index: HashMap<String, usize>,
    dimensions: HashMap<String, Dimension>,
}

/// The process-wide registry, built on first use.
pub fn get_unit_registry() -> &'static UnitRegistry {
    static REGISTRY: LazyLock<UnitRegistry> = LazyLock::new(UnitRegistry::default_en);
    &REGISTRY
}

/// Canonical dimensionality string for a unit or dimension expression (`dimensionality_of`).
///
/// Accepts unit names ("mV", "volt"), dimensionality expressions ("[mass] * [length] ** 2 /
/// [time] ** 3 / [current]") and the "dimensionless" sentinel, and returns the identical
/// canonical string for equal dimensions: terms sorted alphabetically, positive exponents as
/// the numerator, negative ones appended as divisions.
pub fn dimensionality_of(expression: &str) -> Result<String, UnitError> {
    if expression.trim() == DIMENSIONLESS {
        return Ok(DIMENSIONLESS.to_owned());
    }
    let dims = get_unit_registry()
        .get_dimensionality(expression)
        .map_err(|reason| UnitError {
            expression: expression.to_owned(),
            reason,
        })?;
    Ok(render_dimensionality(&dims))
}

/// `_render_dimensionality`: sorted terms, positive exponents first, negatives as divisions.
fn render_dimensionality(dims: &Container) -> String {
    let positive: Vec<_> = dims.iter().filter(|(_, e)| **e > 0.0).collect();
    let negative: Vec<_> = dims.iter().filter(|(_, e)| **e < 0.0).collect();
    if positive.is_empty() && negative.is_empty() {
        return DIMENSIONLESS.to_owned();
    }
    let term = |dim: &str, exp: f64| {
        let exp = exp.abs();
        if exp == 1.0 {
            dim.to_owned()
        } else {
            format!("{dim} ** {}", python_g(exp))
        }
    };
    let mut rendered = if positive.is_empty() {
        "1".to_owned()
    } else {
        positive
            .iter()
            .map(|(dim, exp)| term(dim, **exp))
            .collect::<Vec<_>>()
            .join(" * ")
    };
    for (dim, exp) in negative {
        rendered.push_str(" / ");
        rendered.push_str(&term(dim, *exp));
    }
    rendered
}

/// Python's `format(x, "g")`: six significant digits, trailing zeros dropped, scientific
/// notation below 1e-4 and from 1e6.
fn python_g(value: f64) -> String {
    if value == 0.0 {
        return "0".into();
    }
    if !value.is_finite() {
        return if value.is_nan() {
            "nan".into()
        } else if value > 0.0 {
            "inf".into()
        } else {
            "-inf".into()
        };
    }
    let scientific = format!("{value:.5e}");
    let (mantissa, exponent) = scientific.split_once('e').expect("scientific notation");
    let exponent: i32 = exponent.parse().expect("an exponent");
    let trim = |s: &str| {
        if s.contains('.') {
            s.trim_end_matches('0').trim_end_matches('.').to_owned()
        } else {
            s.to_owned()
        }
    };
    if !(-4..6).contains(&exponent) {
        let sign = if exponent < 0 { '-' } else { '+' };
        format!("{}e{sign}{:02}", trim(mantissa), exponent.abs())
    } else {
        let decimals = (5 - exponent).max(0) as usize;
        trim(&format!("{value:.decimals$}"))
    }
}

// --------------------------------------------------------------------------------------------
// Expressions: pint's string_preprocessor, Python's tokenizer, pint_eval's tree
// --------------------------------------------------------------------------------------------

/// `pint.util.string_preprocessor`.
fn string_preprocessor(input: &str) -> String {
    static MERGE_SPACES: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"([\w\.\-\+\*\\\^])\s+").unwrap());
    static SQUARED: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"([_a-zA-Z][_a-zA-Z0-9]*) squared").unwrap());
    static CUBED: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"([_a-zA-Z][_a-zA-Z0-9]*) cubed").unwrap());
    static CUBIC: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"cubic ([_a-zA-Z][_a-zA-Z0-9]*)").unwrap());
    static SQUARE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"square ([_a-zA-Z][_a-zA-Z0-9]*)").unwrap());
    static SQ: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"sq ([_a-zA-Z][_a-zA-Z0-9]*)").unwrap());
    static PRETTY_EXP: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(⁻?[⁰¹²³⁴⁵⁶⁷⁸⁹]+(?:\.[⁰¹²³⁴⁵⁶⁷⁸⁹]*)?)").unwrap());

    let mut s = input.replace(',', "").replace(" per ", "/");
    s = s.replace('\u{00B0}', "degree");
    s = MERGE_SPACES.replace_all(&s, "$1 ").into_owned();
    s = SQUARED.replace_all(&s, "$1**2").into_owned();
    s = CUBED.replace_all(&s, "$1**3").into_owned();
    s = CUBIC.replace_all(&s, "$1**3").into_owned();
    s = SQUARE.replace_all(&s, "$1**2").into_owned();
    s = SQ.replace_all(&s, "$1**2").into_owned();
    s = number_letter(&s);
    s = space_multiplication(&s);
    s = PRETTY_EXP.replace_all(&s, "**($1)").into_owned();
    s = s
        .chars()
        .map(|c| match c {
            '⁰' => '0',
            '¹' => '1',
            '²' => '2',
            '³' => '3',
            '⁴' => '4',
            '⁵' => '5',
            '⁶' => '6',
            '⁷' => '7',
            '⁸' => '8',
            '⁹' => '9',
            '·' => '*',
            '⁻' => '-',
            other => other,
        })
        .collect();
    s.replace('^', "**")
}

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// `\b([0-9]+\.?[0-9]*)(?=[e|E][a-zA-Z]|[a-df-zA-DF-Z])` → `\1*`, with the regex's backtracking.
fn number_letter(s: &str) -> String {
    let chars: Vec<char> = s.chars().collect();
    let mut out = String::with_capacity(s.len() + 4);
    let mut i = 0;
    while i < chars.len() {
        let boundary = i == 0 || !is_word(chars[i - 1]);
        if boundary && chars[i].is_ascii_digit() {
            let mut d1 = i;
            while d1 < chars.len() && chars[d1].is_ascii_digit() {
                d1 += 1;
            }
            let dot = d1 < chars.len() && chars[d1] == '.';
            let mut d2 = if dot { d1 + 1 } else { d1 };
            while dot && d2 < chars.len() && chars[d2].is_ascii_digit() {
                d2 += 1;
            }
            // Candidate ends in the order the regex backtracks: longest first.
            let mut ends: Vec<usize> = vec![];
            if dot {
                ends.extend((d1 + 1..=d2).rev());
            }
            ends.extend((i + 1..=d1).rev());
            let lookahead_ok = |end: usize| {
                let next = chars.get(end).copied();
                let after = chars.get(end + 1).copied();
                match next {
                    Some('e' | 'E' | '|') => after.is_some_and(|a| a.is_ascii_alphabetic()),
                    Some(c) => c.is_ascii_alphabetic() && !matches!(c, 'e' | 'E'),
                    None => false,
                }
            };
            if let Some(end) = ends.into_iter().find(|end| lookahead_ok(*end)) {
                out.extend(&chars[i..end]);
                out.push('*');
                i = end;
                continue;
            }
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

/// `([\w\.\)])\s+(?=[\w\(])` → `\1*`.
fn space_multiplication(s: &str) -> String {
    let chars: Vec<char> = s.chars().collect();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if is_word(c) || c == '.' || c == ')' {
            let mut j = i + 1;
            while j < chars.len() && chars[j].is_whitespace() {
                j += 1;
            }
            if j > i + 1 && j < chars.len() && (is_word(chars[j]) || chars[j] == '(') {
                out.push(c);
                out.push('*');
                i = j;
                continue;
            }
        }
        out.push(c);
        i += 1;
    }
    out
}

#[derive(Debug, Clone, PartialEq)]
enum Token {
    Number(f64),
    Name(String),
    Op(String),
    End,
}

/// Python's tokenizer treats any non-ASCII character as part of a name (it validates names
/// only when compiling), so `R_∞` and `‰` are names.
fn name_start(c: char) -> bool {
    c == '_' || c.is_ascii_alphabetic() || !c.is_ascii()
}

fn name_continue(c: char) -> bool {
    name_start(c) || c.is_ascii_digit()
}

/// The tokens Python's `tokenize` yields for an expression (what `plain_tokenizer` sees).
/// ASCII characters that are no token of their own (`$`, `?`, `!`, …) come out as operators
/// the evaluation tree passes over; quotes and backslashes are errors, `#` starts a comment.
fn tokenize(s: &str) -> Result<Vec<Token>, String> {
    const OPS: &[&str] = &[
        "**=", "//=", ">>=", "<<=", "...", "**", "//", "->", "==", "!=", "<=", ">=", "<<", ">>",
        ":=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "@=",
    ];
    let chars: Vec<char> = s.chars().collect();
    let mut tokens = vec![];
    let mut depth = 0i32;
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c == ' ' || c == '\t' || c == '\x0c' {
            i += 1;
            continue;
        }
        if c == '#' {
            break;
        }
        if c.is_ascii_digit() || (c == '.' && chars.get(i + 1).is_some_and(|d| d.is_ascii_digit()))
        {
            let (value, end) = number(&chars, i)?;
            tokens.push(Token::Number(value));
            i = end;
            continue;
        }
        if name_start(c) {
            let start = i;
            while i < chars.len() && name_continue(chars[i]) {
                i += 1;
            }
            tokens.push(Token::Name(chars[start..i].iter().collect()));
            continue;
        }
        if matches!(c, '\'' | '"') {
            return Err("unterminated string literal".into());
        }
        if c == '\\' {
            return Err("unexpected character after line continuation character".into());
        }
        let rest: String = chars[i..chars.len().min(i + 3)].iter().collect();
        let op = OPS
            .iter()
            .find(|op| rest.starts_with(**op))
            .map(|op| (*op).to_owned())
            .unwrap_or_else(|| c.to_string());
        match op.as_str() {
            "(" | "[" | "{" => depth += 1,
            ")" | "]" | "}" => {
                depth -= 1;
                if depth < 0 {
                    return Err(format!("unmatched '{op}'"));
                }
            }
            _ => {}
        }
        i += op.chars().count();
        tokens.push(Token::Op(op));
    }
    if depth > 0 {
        return Err("EOF in multi-line statement".into());
    }
    tokens.push(Token::End);
    Ok(tokens)
}

/// A Python numeric literal starting at `start`: its value (as pint's `eval_token` reads it,
/// with `int()` then `float()`) and where it ends.
fn number(chars: &[char], start: usize) -> Result<(f64, usize), String> {
    // Digits with single underscores between them, as Python's tokenizer accepts them.
    let digits = |from: usize| -> Result<usize, String> {
        let mut end = from;
        while end < chars.len() {
            let separator = chars[end] == '_'
                && end > from
                && chars.get(end + 1).is_some_and(char::is_ascii_digit);
            if chars[end].is_ascii_digit() || separator {
                end += 1;
            } else if chars[end] == '_' {
                return Err("invalid decimal literal".into());
            } else {
                break;
            }
        }
        Ok(end)
    };
    if chars[start] == '0'
        && chars
            .get(start + 1)
            .is_some_and(|c| matches!(c, 'x' | 'X' | 'o' | 'O' | 'b' | 'B'))
    {
        // Tokenized as a number, but neither int() nor float() reads it.
        return Err("invalid literal for int() with base 10".into());
    }
    let mut end = digits(start)?;
    if end < chars.len() && chars[end] == '.' {
        end = digits(end + 1)?;
    }
    if end < chars.len() && matches!(chars[end], 'e' | 'E') {
        let mut probe = end + 1;
        if probe < chars.len() && matches!(chars[probe], '+' | '-') {
            probe += 1;
        }
        if probe < chars.len() && chars[probe].is_ascii_digit() {
            end = digits(probe)?;
        }
    }
    if end < chars.len() && matches!(chars[end], 'j' | 'J') {
        return Err("could not convert string to float".into());
    }
    let text: String = chars[start..end].iter().filter(|c| **c != '_').collect();
    text.parse::<f64>()
        .map(|v| (v, end))
        .map_err(|e| e.to_string())
}

/// A node of pint's evaluation tree.
enum Node {
    Leaf(Token),
    Unary(String, Box<Node>),
    Binary(Box<Node>, String, Box<Node>),
}

fn priority(op: &str) -> Option<i32> {
    Some(match op {
        "**" | "^" => 3,
        "unary" => 2,
        "*" | "" | "//" | "/" | "%" => 1,
        "+" | "-" => 0,
        _ => return None,
    })
}

/// `pint_eval._build_eval_tree`, token for token.
fn build(tokens: &[Token], mut index: usize, prev_op: &str) -> Result<(Node, usize), String> {
    let mut result: Option<Node> = None;
    let prev_priority = priority(prev_op).unwrap_or(-1);
    loop {
        match &tokens[index] {
            Token::Op(op) if op == ")" => {
                if prev_op == "<none>" {
                    return Err("unopened parentheses in tokens".into());
                }
                let result = result.ok_or("empty parentheses")?;
                return Ok((result, if prev_op == "(" { index } else { index - 1 }));
            }
            Token::Op(op) if op == "(" => {
                let (right, at) = build(tokens, index + 1, "(")?;
                index = at;
                if tokens.get(index) != Some(&Token::Op(")".into())) {
                    return Err("weird exit from parentheses".into());
                }
                result = Some(match result {
                    Some(left) => Node::Binary(Box::new(left), String::new(), Box::new(right)),
                    None => right,
                });
            }
            Token::Op(op) if priority(op).is_some() => match result.take() {
                Some(left) => {
                    if priority(op).unwrap() <= prev_priority && op != "**" && op != "^" {
                        return Ok((left, index - 1));
                    }
                    let (right, at) = build(tokens, index + 1, op)?;
                    index = at;
                    result = Some(Node::Binary(Box::new(left), op.clone(), Box::new(right)));
                }
                None => {
                    let (right, at) = build(tokens, index + 1, "unary")?;
                    index = at;
                    result = Some(Node::Unary(op.clone(), Box::new(right)));
                }
            },
            token @ (Token::Number(_) | Token::Name(_)) => match result.take() {
                Some(left) => {
                    if priority("").unwrap() <= prev_priority {
                        return Ok((left, index - 1));
                    }
                    let (right, at) = build(tokens, index, "")?;
                    index = at;
                    result = Some(Node::Binary(Box::new(left), String::new(), Box::new(right)));
                }
                None => result = Some(Node::Leaf(token.clone())),
            },
            // Other operators (",", ".", "@", …) are passed over, as pint does.
            _ => {}
        }
        if tokens[index] == Token::End {
            if prev_op == "(" {
                return Err("unclosed parentheses in tokens".into());
            }
            let result = result.ok_or("empty expression")?;
            return Ok((result, index));
        }
        if index + 1 >= tokens.len() {
            return Err("unexpected end to tokens".into());
        }
        index += 1;
    }
}

/// A value while evaluating: a number, or names with exponents and a scale (`ParserHelper`).
#[derive(Debug, Clone)]
enum Value {
    Number(f64),
    Units { scale: f64, names: Container },
}

fn merge(mut left: Container, right: &Container, sign: f64) -> Container {
    for (name, exp) in right {
        let value = left.get(name).copied().unwrap_or(0.0) + sign * exp;
        if value == 0.0 {
            left.remove(name);
        } else {
            left.insert(name.clone(), value);
        }
    }
    left
}

fn apply(op: &str, left: Value, right: Value) -> Result<Value, String> {
    use Value::{Number as N, Units as U};
    let nonzero = |d: f64| {
        if d == 0.0 {
            Err("division by zero".to_owned())
        } else {
            Ok(d)
        }
    };
    Ok(match (op, left, right) {
        ("*" | "", N(a), N(b)) => N(a * b),
        ("*" | "", N(a), U { scale, names }) | ("*" | "", U { scale, names }, N(a)) => U {
            scale: scale * a,
            names,
        },
        ("*" | "", U { scale: a, names: x }, U { scale: b, names: y }) => U {
            scale: a * b,
            names: merge(x, &y, 1.0),
        },
        ("/", N(a), N(b)) => N(a / nonzero(b)?),
        ("/" | "//", U { scale, names }, N(b)) => U {
            scale: scale / nonzero(b)?,
            names,
        },
        ("/", N(a), U { scale, names }) => U {
            scale: a / scale,
            names: names.into_iter().map(|(k, v)| (k, -v)).collect(),
        },
        ("/" | "//", U { scale: a, names: x }, U { scale: b, names: y }) => U {
            scale: a / b,
            names: merge(x, &y, -1.0),
        },
        ("//", N(a), N(b)) => N((a / nonzero(b)?).floor()),
        ("%", N(a), N(b)) => {
            let b = nonzero(b)?;
            N(a - b * (a / b).floor())
        }
        ("+", N(a), N(b)) => N(a + b),
        ("-", N(a), N(b)) => N(a - b),
        ("**" | "^", N(a), N(b)) => {
            if a == 0.0 && b < 0.0 {
                return Err("0.0 cannot be raised to a negative power".into());
            }
            N(a.powf(b))
        }
        ("**" | "^", U { scale, names }, N(b)) => U {
            scale: scale.powf(b),
            names: names
                .into_iter()
                .filter_map(|(k, v)| (v * b != 0.0).then_some((k, v * b)))
                .collect(),
        },
        (op, _, _) => return Err(format!("unsupported operand for {op:?}")),
    })
}

fn evaluate(node: &Node) -> Result<Value, String> {
    match node {
        Node::Leaf(Token::Number(n)) => Ok(Value::Number(*n)),
        Node::Leaf(Token::Name(name)) => Ok(Value::Units {
            scale: 1.0,
            names: BTreeMap::from([(name.clone(), 1.0)]),
        }),
        Node::Leaf(_) => Err("unknown token type".into()),
        Node::Unary(op, operand) => {
            let value = evaluate(operand)?;
            match op.as_str() {
                "+" => Ok(value),
                "-" => apply("*", value, Value::Number(-1.0)),
                other => Err(format!("{other:?} is not a unary operator")),
            }
        }
        Node::Binary(left, op, right) => apply(op, evaluate(left)?, evaluate(right)?),
    }
}

/// `ParserHelper.from_string`: the names of an expression with their exponents, and its scale.
fn parse_expression(input: &str) -> Result<(f64, Container), String> {
    if input.is_empty() {
        return Ok((1.0, Container::new()));
    }
    let mut s = string_preprocessor(input);
    let brackets = s.contains('[');
    if brackets {
        s = s.replace('[', "__obra__").replace(']', "__cbra__");
    }
    let tokens = tokenize(&s)?;
    let (tree, _) = build(&tokens, 0, "<none>")?;
    let (mut scale, names) = match evaluate(&tree)? {
        Value::Number(n) => return Ok((n, Container::new())),
        Value::Units { scale, names } => (scale, names),
    };
    let mut container = Container::new();
    for (name, exp) in names {
        let name = if brackets {
            name.replace("__obra__", "[").replace("__cbra__", "]")
        } else {
            name
        };
        if name.to_lowercase() == "nan" {
            scale = f64::NAN;
            continue;
        }
        container.insert(name, exp);
    }
    Ok((scale, container))
}

fn is_dim(name: &str) -> bool {
    name.starts_with('[') && name.ends_with(']')
}

// --------------------------------------------------------------------------------------------
// The registry
// --------------------------------------------------------------------------------------------

impl UnitRegistry {
    fn empty() -> Self {
        Self {
            units: HashMap::new(),
            prefixes: vec![(String::new(), String::new())],
            prefix_index: HashMap::from([(String::new(), 0)]),
            dimensions: HashMap::new(),
        }
    }

    /// pint's default registry: `default_en.txt`, which imports `constants_en.txt`.
    fn default_en() -> Self {
        let mut registry = Self::empty();
        registry.load(DEFAULT_EN);
        registry
    }

    fn load(&mut self, text: &str) {
        let mut skipping = false;
        for raw in text.lines() {
            let line = raw.split('#').next().unwrap_or("").trim();
            if line.is_empty() {
                continue;
            }
            if skipping {
                if line == "@end" {
                    skipping = false;
                }
                continue;
            }
            if let Some(directive) = line.strip_prefix('@') {
                let word = directive
                    .split(|c: char| c.is_whitespace() || c == '(')
                    .next()
                    .unwrap_or("");
                match word {
                    "import" if directive.contains("constants_en.txt") => self.load(CONSTANTS_EN),
                    "defaults" | "system" | "context" => skipping = true,
                    "alias" => self.add_alias(directive.trim_start_matches("alias").trim()),
                    // `@group NAME [using …]` holds definitions; its `@end` closes nothing to skip.
                    _ => {}
                }
                continue;
            }
            self.define(line);
        }
    }

    fn add_alias(&mut self, definition: &str) {
        let mut parts = definition.split('=').map(str::trim);
        let Some(target) = parts.next().and_then(|name| self.units.get(name).cloned()) else {
            return;
        };
        for alias in parts {
            self.units.insert(alias.to_owned(), target.clone());
        }
    }

    fn add_prefix_key(&mut self, key: &str, name: &str) {
        match self.prefix_index.get(key) {
            Some(at) => self.prefixes[*at].1 = name.to_owned(),
            None => {
                self.prefix_index
                    .insert(key.to_owned(), self.prefixes.len());
                self.prefixes.push((key.to_owned(), name.to_owned()));
            }
        }
    }

    fn add_dimension_if_missing(&mut self, name: &str) {
        self.dimensions
            .entry(name.to_owned())
            .or_insert(Dimension::Base);
    }

    fn define(&mut self, line: &str) {
        let parts: Vec<&str> = line.split('=').map(str::trim).collect();
        let name = parts[0];
        let symbol = parts.get(2).copied().filter(|s| *s != "_");
        let aliases = parts.iter().skip(3).copied();

        if let Some(prefix) = name.strip_suffix('-') {
            self.add_prefix_key(prefix, prefix);
            if let Some(symbol) = symbol {
                self.add_prefix_key(symbol.trim_end_matches('-'), prefix);
            }
            for alias in aliases {
                self.add_prefix_key(alias.trim_end_matches('-'), prefix);
            }
            return;
        }

        let Some(value) = parts.get(1) else { return };
        if is_dim(name) {
            let Ok((_, reference)) = parse_expression(value) else {
                return;
            };
            for dim in reference.keys() {
                self.add_dimension_if_missing(dim);
            }
            self.dimensions
                .insert(name.to_owned(), Dimension::Derived(reference));
            return;
        }

        let mut pieces = value.split(';').map(str::trim);
        let relation = pieces.next().unwrap_or("");
        let mut multiplicative = true;
        let mut offset = false;
        for modifier in pieces {
            let Some((key, amount)) = modifier.split_once(':') else {
                continue;
            };
            match key.trim() {
                "offset" => {
                    let zero = matches!(parse_expression(amount.trim()), Ok((v, names)) if names.is_empty() && v == 0.0);
                    if !zero {
                        multiplicative = false;
                        offset = true;
                    }
                }
                "logbase" | "logfactor" => multiplicative = false,
                _ => {}
            }
        }
        let Ok((_, reference)) = parse_expression(relation) else {
            return;
        };
        if !reference.is_empty() && reference.keys().all(|k| is_dim(k)) {
            for dim in reference.keys() {
                self.add_dimension_if_missing(dim);
            }
        }
        let definition = Arc::new(UnitDefinition {
            name: name.to_owned(),
            reference: reference.clone(),
            multiplicative,
        });
        let aliases: Vec<&str> = aliases.collect();
        self.units.insert(name.to_owned(), definition.clone());
        if let Some(symbol) = symbol {
            self.units.insert(symbol.to_owned(), definition.clone());
        }
        for alias in &aliases {
            self.units.insert((*alias).to_owned(), definition.clone());
        }

        // An offset unit gets its difference twin (`NonMultiplicativeRegistry._add_unit`).
        if offset {
            let delta = Arc::new(UnitDefinition {
                name: format!("delta_{name}"),
                reference,
                multiplicative: true,
            });
            self.units.insert(delta.name.clone(), delta.clone());
            if let Some(symbol) = symbol {
                self.units.insert(format!("Δ{symbol}"), delta.clone());
            }
            for alias in &aliases {
                self.units.insert(format!("Δ{alias}"), delta.clone());
            }
            for alias in &aliases {
                self.units.insert(format!("delta_{alias}"), delta.clone());
            }
        }
    }

    /// `PlainRegistry.get_name`, returning the definition: direct lookup, else every
    /// (prefix, name, plural) reading, the prefixed one preferred.
    fn resolve(&self, key: &str) -> Result<Arc<UnitDefinition>, String> {
        if key == DIMENSIONLESS {
            return Err("'' is not a unit".into());
        }
        if let Some(definition) = self.units.get(key) {
            return Ok(definition.clone());
        }
        let mut candidates: Vec<(String, Arc<UnitDefinition>)> = vec![];
        for suffix in ["", "s"] {
            for (prefix, prefix_name) in &self.prefixes {
                if !(key.starts_with(prefix.as_str()) && key.ends_with(suffix)) {
                    continue;
                }
                let rest = &key[prefix.len()..];
                let name = if suffix.is_empty() {
                    rest
                } else {
                    let trimmed = rest.get(..rest.len().saturating_sub(1)).unwrap_or("");
                    if trimmed.chars().count() == 1 || rest.is_empty() {
                        continue;
                    }
                    trimmed
                };
                if let Some(definition) = self.units.get(name) {
                    let candidate = (prefix_name.clone(), definition.clone());
                    if !candidates
                        .iter()
                        .any(|(p, d)| *p == candidate.0 && d.name == candidate.1.name)
                    {
                        candidates.push(candidate);
                    }
                }
            }
        }
        // `_dedup_candidates`: ("kilo", "gram") wins over ("", "kilogram").
        let prefixed: Vec<String> = candidates
            .iter()
            .filter(|(p, _)| !p.is_empty())
            .map(|(p, d)| format!("{p}{}", d.name))
            .collect();
        candidates.retain(|(p, d)| !(p.is_empty() && prefixed.contains(&d.name)));
        let (prefix, definition) = candidates
            .into_iter()
            .next()
            .ok_or_else(|| format!("'{key}' is not defined in the unit registry"))?;
        if !prefix.is_empty() && !definition.multiplicative {
            return Err("Prefixing a unit requires multiplying the unit.".into());
        }
        Ok(definition)
    }

    fn recurse(
        &self,
        reference: &Container,
        exp: f64,
        accumulator: &mut Container,
        depth: usize,
    ) -> Result<(), String> {
        if depth > 64 {
            return Err("definitions recurse too deeply".into());
        }
        for (key, value) in reference {
            let exp2 = exp * value;
            if is_dim(key) {
                match self.dimensions.get(key) {
                    Some(Dimension::Derived(reference)) => {
                        self.recurse(reference, exp2, accumulator, depth + 1)?
                    }
                    Some(Dimension::Base) => *accumulator.entry(key.clone()).or_insert(0.0) += exp2,
                    None => {
                        return Err(format!(
                            "{key} is not defined as dimension in the pint UnitRegistry"
                        ))
                    }
                }
            } else {
                let definition = self.resolve(key)?;
                self.recurse(&definition.reference, exp2, accumulator, depth + 1)?;
            }
        }
        Ok(())
    }

    /// `get_dimensionality` for an expression: its plain dimensions and their exponents.
    fn get_dimensionality(&self, expression: &str) -> Result<Container, String> {
        let (scale, container) = parse_expression(expression)?;
        if container.is_empty() {
            return Ok(Container::new());
        }
        if scale != 1.0 {
            return Err("Only scale 1 ParserHelper instance should be considered hashable".into());
        }
        let mut accumulator = Container::new();
        self.recurse(&container, 1.0, &mut accumulator, 0)?;
        accumulator.remove("[]");
        accumulator.retain(|_, exp| *exp != 0.0);
        Ok(accumulator)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn g_formatting_is_pythons() {
        assert_eq!(python_g(2.0), "2");
        assert_eq!(python_g(0.5), "0.5");
        assert_eq!(python_g(1.0 / 3.0), "0.333333");
        assert_eq!(python_g(1.5e-7), "1.5e-07");
        assert_eq!(python_g(1234567.0), "1.23457e+06");
        assert_eq!(python_g(100000.0), "100000");
    }

    #[test]
    fn the_common_units() {
        assert_eq!(
            dimensionality_of("mV").unwrap(),
            "[length] ** 2 * [mass] / [current] / [time] ** 3"
        );
        assert_eq!(
            dimensionality_of("pF").unwrap(),
            "[current] ** 2 * [time] ** 4 / [length] ** 2 / [mass]"
        );
        assert_eq!(dimensionality_of("dimensionless").unwrap(), "dimensionless");
        assert!(dimensionality_of("2 m").is_err());
        assert!(dimensionality_of("meterz").is_err());
    }
}
