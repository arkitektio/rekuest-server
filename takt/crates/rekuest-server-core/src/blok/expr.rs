//! The blok expression language: a small JavaScript subset, parsed by hand
//! (`arkitekt_spec/blok/expr.py`). The same grammar is implemented in Python and TypeScript
//! and held to one corpus:
//!
//! ```text
//! callback   := expression ('into' name)?
//! expression := ternary
//! ternary    := coalesce ('?' expression ':' expression)?
//! coalesce   := or ('??' or)*
//! or         := and ('||' and)*
//! and        := equality ('&&' equality)*
//! equality   := relation (('==' | '!=') relation)?
//! relation   := unary (('>' | '>=' | '<' | '<=') unary)?
//! unary      := '!' unary | '-' number | primary
//! primary    := literal | path | call | list | object | '(' expression ')'
//! ```
//!
//! Every offset is a Unicode code point into the whole source, as the spec counts them.

use serde_json::{Number, Value};

use super::vocabulary::{diagnostic_message, Fields};
use crate::pyjson::repr_str;

/// A source refused: a row of the diagnostic table, and where (1-based line and column, in code
/// points) when the error has a place.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BsxError {
    pub code: String,
    pub line: Option<usize>,
    pub column: Option<usize>,
    pub message: String,
}

impl BsxError {
    pub fn new(code: &str, place: Option<(usize, usize)>, fields: Fields<'_>) -> BsxError {
        BsxError {
            code: code.to_owned(),
            line: place.map(|(line, _)| line),
            column: place.map(|(_, column)| column),
            message: diagnostic_message(code, fields),
        }
    }
}

impl std::fmt::Display for BsxError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if let (Some(line), Some(column)) = (self.line, self.column) {
            write!(f, "line {line}, column {column}: ")?;
        }
        write!(f, "{} [{}]", self.message, self.code)
    }
}

impl std::error::Error for BsxError {}

pub(crate) fn s(value: impl Into<String>) -> Value {
    Value::String(value.into())
}

/// Turns an offset into the whole source into a (line, column), both 1-based.
#[derive(Debug, Clone)]
pub struct Locator {
    starts: Vec<usize>,
}

impl Locator {
    pub fn new(source: &[char]) -> Locator {
        let mut starts = vec![0];
        for (index, c) in source.iter().enumerate() {
            if *c == '\n' {
                starts.push(index + 1);
            }
        }
        Locator { starts }
    }

    pub fn locate(&self, offset: usize) -> (usize, usize) {
        let line = self.starts.partition_point(|start| *start <= offset).max(1) - 1;
        (line + 1, offset - self.starts[line] + 1)
    }

    pub fn fail(&self, code: &str, offset: usize, fields: Fields<'_>) -> BsxError {
        BsxError::new(code, Some(self.locate(offset)), fields)
    }
}

/// A call's argument: `name: value` or positional (`name` none).
#[derive(Debug, Clone, PartialEq)]
pub struct Argument {
    pub name: Option<String>,
    pub value: Expr,
    pub offset: usize,
}

/// `str.upper(x)`, `self.move(to: 1)`, `queries.positions.refetch()`.
#[derive(Debug, Clone, PartialEq)]
pub struct CallExpr {
    pub callee: Vec<String>,
    pub arguments: Vec<Argument>,
    pub offset: usize,
}

impl CallExpr {
    /// The callee as written.
    pub fn name(&self) -> String {
        self.callee.join(".")
    }
}

/// An expression.
#[derive(Debug, Clone, PartialEq)]
pub enum Expr {
    /// A string, number, boolean or `null`.
    Literal {
        value: Value,
        offset: usize,
    },
    List {
        items: Vec<Expr>,
        offset: usize,
    },
    Object {
        entries: Vec<(String, Expr)>,
        offset: usize,
    },
    Path {
        segments: Vec<String>,
        offset: usize,
    },
    Call(CallExpr),
    Not {
        operand: Box<Expr>,
        offset: usize,
    },
    Compare {
        operator: String,
        left: Box<Expr>,
        right: Box<Expr>,
        offset: usize,
    },
    /// `a && b && c`, `a || b` or `a ?? b`: one operator over every operand.
    Logical {
        operator: String,
        operands: Vec<Expr>,
        offset: usize,
    },
    Ternary {
        cond: Box<Expr>,
        then: Box<Expr>,
        otherwise: Box<Expr>,
        offset: usize,
    },
    /// `<call> into <name>`: a task call whose record is named.
    Into {
        call: CallExpr,
        name: String,
        offset: usize,
    },
}

impl Expr {
    pub fn offset(&self) -> usize {
        match self {
            Expr::Literal { offset, .. }
            | Expr::List { offset, .. }
            | Expr::Object { offset, .. }
            | Expr::Path { offset, .. }
            | Expr::Not { offset, .. }
            | Expr::Compare { offset, .. }
            | Expr::Logical { offset, .. }
            | Expr::Ternary { offset, .. }
            | Expr::Into { offset, .. } => *offset,
            Expr::Call(call) => call.offset,
        }
    }

    /// The expressions directly inside this one.
    pub fn children(&self) -> Vec<&Expr> {
        match self {
            Expr::List { items, .. } => items.iter().collect(),
            Expr::Object { entries, .. } => entries.iter().map(|(_, e)| e).collect(),
            Expr::Not { operand, .. } => vec![operand],
            Expr::Compare { left, right, .. } => vec![left, right],
            Expr::Logical { operands, .. } => operands.iter().collect(),
            Expr::Ternary {
                cond,
                then,
                otherwise,
                ..
            } => vec![cond, then, otherwise],
            Expr::Call(call) => call.arguments.iter().map(|a| &a.value).collect(),
            Expr::Literal { .. } | Expr::Path { .. } | Expr::Into { .. } => vec![],
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TokenKind {
    Name,
    Number,
    Str,
    Op,
    End,
}

#[derive(Debug, Clone)]
struct Token {
    kind: TokenKind,
    value: String,
    offset: usize,
    literal: Value,
}

const OPERATORS: [&str; 37] = [
    "===", "!==", "=>", "==", "!=", ">=", "<=", "&&", "||", "??", "?.", "**", "(", ")", "[", "]",
    "{", "}", ",", ":", ".", "?", "!", ">", "<", "=", ";", "+", "-", "*", "/", "%", "&", "|", "^",
    "~", "`",
];

fn arithmetic(operator: &str) -> Option<&'static str> {
    Some(match operator {
        "+" => "add",
        "-" => "sub",
        "*" => "mul",
        "/" => "div",
        "%" => "mod",
        "**" => "pow",
        _ => return None,
    })
}

fn is_name_start(c: char) -> bool {
    c.is_ascii_alphabetic() || c == '_'
}

fn is_name_part(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

fn starts_with(text: &[char], index: usize, pattern: &str) -> bool {
    let mut i = index;
    for p in pattern.chars() {
        if text.get(i) != Some(&p) {
            return false;
        }
        i += 1;
    }
    true
}

struct Lexer<'a> {
    text: &'a [char],
    base: usize,
    locate: &'a Locator,
}

impl Lexer<'_> {
    fn fail(&self, code: &str, offset: usize, fields: Fields<'_>) -> BsxError {
        self.locate.fail(code, self.base + offset, fields)
    }

    fn tokens(&self) -> Result<Vec<Token>, BsxError> {
        let text = self.text;
        let mut out = vec![];
        let mut index = 0;
        while index < text.len() {
            let c = text[index];
            if matches!(c, ' ' | '\t' | '\r' | '\n') {
                index += 1;
                continue;
            }
            if is_name_start(c) {
                let mut end = index + 1;
                while end < text.len() && is_name_part(text[end]) {
                    end += 1;
                }
                out.push(Token {
                    kind: TokenKind::Name,
                    value: text[index..end].iter().collect(),
                    offset: index,
                    literal: Value::Null,
                });
                index = end;
                continue;
            }
            if c.is_ascii_digit() {
                let (end, value) = self.number(index)?;
                out.push(Token {
                    kind: TokenKind::Number,
                    value: text[index..end].iter().collect(),
                    offset: index,
                    literal: value,
                });
                index = end;
                continue;
            }
            if c == '"' || c == '\'' {
                let (end, value) = self.string(index)?;
                out.push(Token {
                    kind: TokenKind::Str,
                    value: text[index..end].iter().collect(),
                    offset: index,
                    literal: Value::String(value),
                });
                index = end;
                continue;
            }
            let operator = OPERATORS.iter().find(|op| starts_with(text, index, op));
            let Some(operator) = operator else {
                return Err(self.fail(
                    "expr.syntax",
                    index,
                    &[(
                        "detail",
                        s(format!("unexpected {}", repr_str(&c.to_string()))),
                    )],
                ));
            };
            if *operator == "`" {
                return Err(self.fail(
                    "expr.syntax",
                    index,
                    &[("detail", s("template literals are not supported; use text children or str.template"))],
                ));
            }
            out.push(Token {
                kind: TokenKind::Op,
                value: (*operator).to_owned(),
                offset: index,
                literal: Value::Null,
            });
            index += operator.chars().count();
        }
        out.push(Token {
            kind: TokenKind::End,
            value: String::new(),
            offset: text.len(),
            literal: Value::Null,
        });
        for token in &mut out {
            token.offset += self.base;
        }
        Ok(out)
    }

    fn number(&self, start: usize) -> Result<(usize, Value), BsxError> {
        let text = self.text;
        let digit = |i: usize| text.get(i).is_some_and(char::is_ascii_digit);
        let mut index = start;
        while digit(index) {
            index += 1;
        }
        if text[start] == '0' && index - start > 1 {
            return Err(self.fail(
                "expr.syntax",
                start,
                &[("detail", s("a number has no leading zeros"))],
            ));
        }
        let mut is_float = false;
        if text.get(index) == Some(&'.') && digit(index + 1) {
            is_float = true;
            index += 1;
            while digit(index) {
                index += 1;
            }
        }
        if matches!(text.get(index), Some('e' | 'E')) {
            let mut exponent = index + 1;
            if matches!(text.get(exponent), Some('+' | '-')) {
                exponent += 1;
            }
            if digit(exponent) {
                is_float = true;
                index = exponent;
                while digit(index) {
                    index += 1;
                }
            }
        }
        if text.get(index).is_some_and(|c| is_name_part(*c)) {
            let written: String = text[start..=index].iter().collect();
            return Err(self.fail(
                "expr.syntax",
                start,
                &[(
                    "detail",
                    s(format!("{} is not a number", repr_str(&written))),
                )],
            ));
        }
        let written: String = text[start..index].iter().collect();
        let value = if is_float {
            written
                .parse::<f64>()
                .ok()
                .and_then(Number::from_f64)
                .map(Value::Number)
        } else if let Ok(int) = written.parse::<i64>() {
            Some(Value::from(int))
        } else if let Ok(int) = written.parse::<u64>() {
            Some(Value::from(int))
        } else {
            written
                .parse::<f64>()
                .ok()
                .and_then(Number::from_f64)
                .map(Value::Number)
        };
        let value = value.ok_or_else(|| {
            self.fail(
                "expr.syntax",
                start,
                &[(
                    "detail",
                    s(format!("{} is not a number", repr_str(&written))),
                )],
            )
        })?;
        Ok((index, value))
    }

    fn string(&self, start: usize) -> Result<(usize, String), BsxError> {
        let text = self.text;
        let quote = text[start];
        let mut index = start + 1;
        let mut out = String::new();
        loop {
            let Some(&c) = text.get(index) else {
                return Err(self.fail(
                    "expr.syntax",
                    start,
                    &[("detail", s("a string is never closed"))],
                ));
            };
            if c == quote {
                return Ok((index + 1, out));
            }
            if c == '\n' {
                return Err(self.fail(
                    "expr.syntax",
                    start,
                    &[("detail", s("a string is never closed on its line"))],
                ));
            }
            if c == '\\' {
                let escape = text.get(index + 1).copied();
                if escape == Some('u') {
                    let digits: String = text
                        .get(index + 2..(index + 6).min(text.len()))
                        .unwrap_or_default()
                        .iter()
                        .collect();
                    let code = (digits.chars().count() == 4
                        && digits.chars().all(|d| d.is_ascii_hexdigit()))
                    .then(|| u32::from_str_radix(&digits, 16).ok())
                    .flatten();
                    let Some(code) = code else {
                        return Err(self.fail(
                            "expr.syntax",
                            index,
                            &[("detail", s("\\u takes four hex digits"))],
                        ));
                    };
                    // A lone surrogate is no character Rust can hold; the spec keeps it as one.
                    out.push(char::from_u32(code).unwrap_or('\u{fffd}'));
                    index += 6;
                    continue;
                }
                let replaced = match escape {
                    Some('\\') => '\\',
                    Some('"') => '"',
                    Some('\'') => '\'',
                    Some('n') => '\n',
                    Some('t') => '\t',
                    Some('r') => '\r',
                    Some('/') => '/',
                    Some('b') => '\u{8}',
                    Some('f') => '\u{c}',
                    other => {
                        let written = other.map(String::from).unwrap_or_default();
                        return Err(self.fail(
                            "expr.syntax",
                            index,
                            &[("detail", s(format!("unknown escape \\{written}")))],
                        ));
                    }
                };
                out.push(replaced);
                index += 2;
                continue;
            }
            out.push(c);
            index += 1;
        }
    }
}

struct Parser<'a> {
    tokens: Vec<Token>,
    index: usize,
    locate: &'a Locator,
}

type Parsed<T> = Result<T, BsxError>;

impl<'a> Parser<'a> {
    fn new(text: &'a [char], base: usize, locate: &'a Locator) -> Parsed<Parser<'a>> {
        let tokens = Lexer { text, base, locate }.tokens()?;
        Ok(Parser {
            tokens,
            index: 0,
            locate,
        })
    }

    fn token(&self) -> &Token {
        &self.tokens[self.index]
    }

    fn peek(&self) -> &Token {
        &self.tokens[(self.index + 1).min(self.tokens.len() - 1)]
    }

    fn fail(&self, code: &str, offset: Option<usize>, fields: Fields<'_>) -> BsxError {
        self.locate
            .fail(code, offset.unwrap_or(self.token().offset), fields)
    }

    fn advance(&mut self) -> Token {
        let token = self.tokens[self.index].clone();
        self.index += 1;
        token
    }

    fn is_op(&self, values: &[&str]) -> bool {
        self.token().kind == TokenKind::Op && values.contains(&self.token().value.as_str())
    }

    fn is_name(&self, value: &str) -> bool {
        self.token().kind == TokenKind::Name && self.token().value == value
    }

    fn expect(&mut self, value: &str) -> Parsed<Token> {
        if !self.is_op(&[value]) {
            return Err(self.unexpected(&format!("expected {}", repr_str(value))));
        }
        Ok(self.advance())
    }

    fn unexpected(&self, expected: &str) -> BsxError {
        let token = self.token();
        if token.kind == TokenKind::End {
            return self.fail(
                "expr.syntax",
                None,
                &[("detail", s(format!("{expected}, but the expression ends")))],
            );
        }
        if token.kind == TokenKind::Op {
            if let Some(refused) = self.refused(token) {
                return refused;
            }
        }
        self.fail(
            "expr.syntax",
            None,
            &[(
                "detail",
                s(format!("{expected}, not {}", repr_str(&token.value))),
            )],
        )
    }

    fn refused(&self, token: &Token) -> Option<BsxError> {
        let value = token.value.as_str();
        if let Some(function) = arithmetic(value) {
            return Some(self.fail(
                "expr.arithmetic",
                None,
                &[("operator", s(value)), ("function", s(function))],
            ));
        }
        Some(match value {
            "=>" => self.fail("expr.lambda", None, &[]),
            "=" => self.fail("callback.assignment", None, &[]),
            ";" => self.fail("callback.sequence", None, &[]),
            "[" => self.fail("expr.subscript", None, &[]),
            "===" | "!==" => self.fail(
                "expr.syntax",
                None,
                &[(
                    "detail",
                    s(format!("write {}: equality is always deep", &value[..2])),
                )],
            ),
            "?." => self.fail(
                "expr.syntax",
                None,
                &[(
                    "detail",
                    s("?. is not supported; missing paths read as null"),
                )],
            ),
            _ => return None,
        })
    }

    fn callback(&mut self) -> Parsed<Expr> {
        if self.is_name("await") {
            return Err(self.fail("callback.await", None, &[]));
        }
        let expression = self.expression()?;
        if self.is_name("into") {
            let into = self.advance();
            let Expr::Call(call) = expression else {
                return Err(self.fail("callback.into", Some(into.offset), &[]));
            };
            if self.token().kind != TokenKind::Name || self.peek().kind != TokenKind::End {
                return Err(self.fail("callback.into", Some(into.offset), &[]));
            }
            let name = self.advance();
            return Ok(Expr::Into {
                call,
                name: name.value,
                offset: into.offset,
            });
        }
        self.finish()?;
        Ok(expression)
    }

    fn finish(&self) -> Parsed<()> {
        if self.token().kind != TokenKind::End {
            return Err(self.unexpected("expected the end of the expression"));
        }
        Ok(())
    }

    fn expression(&mut self) -> Parsed<Expr> {
        self.ternary()
    }

    fn ternary(&mut self) -> Parsed<Expr> {
        let cond = self.logical("??")?;
        if !self.is_op(&["?"]) {
            return Ok(cond);
        }
        let offset = self.advance().offset;
        let then = self.expression()?;
        self.expect(":")?;
        let otherwise = self.expression()?;
        Ok(Expr::Ternary {
            cond: Box::new(cond),
            then: Box::new(then),
            otherwise: Box::new(otherwise),
            offset,
        })
    }

    fn logical(&mut self, operator: &str) -> Parsed<Expr> {
        let inner = |parser: &mut Self| match operator {
            "??" => parser.logical("||"),
            "||" => parser.logical("&&"),
            _ => parser.equality(),
        };
        let first = inner(self)?;
        let offset = first.offset();
        let mut operands = vec![first];
        while self.is_op(&[operator]) {
            self.advance();
            operands.push(inner(self)?);
        }
        if operands.len() == 1 {
            return Ok(operands.remove(0));
        }
        Ok(Expr::Logical {
            operator: operator.to_owned(),
            operands,
            offset,
        })
    }

    fn equality(&mut self) -> Parsed<Expr> {
        let left = self.relation()?;
        if !self.is_op(&["==", "!="]) {
            return Ok(left);
        }
        let operator = self.advance();
        let right = self.relation()?;
        if self.is_op(&["==", "!=", ">", ">=", "<", "<="]) {
            return Err(self.fail("expr.chained_comparison", None, &[]));
        }
        Ok(Expr::Compare {
            operator: operator.value,
            left: Box::new(left),
            right: Box::new(right),
            offset: operator.offset,
        })
    }

    fn relation(&mut self) -> Parsed<Expr> {
        let left = self.unary()?;
        if !self.is_op(&[">", ">=", "<", "<="]) {
            return Ok(left);
        }
        let operator = self.advance();
        let right = self.unary()?;
        if self.is_op(&[">", ">=", "<", "<="]) {
            return Err(self.fail("expr.chained_comparison", None, &[]));
        }
        Ok(Expr::Compare {
            operator: operator.value,
            left: Box::new(left),
            right: Box::new(right),
            offset: operator.offset,
        })
    }

    fn unary(&mut self) -> Parsed<Expr> {
        if self.is_op(&["!"]) {
            let offset = self.advance().offset;
            let operand = self.unary()?;
            return Ok(Expr::Not {
                operand: Box::new(operand),
                offset,
            });
        }
        if self.is_op(&["-"]) {
            let offset = self.advance().offset;
            if self.token().kind != TokenKind::Number {
                return Err(self.fail(
                    "expr.arithmetic",
                    Some(offset),
                    &[("operator", s("-")), ("function", s("neg"))],
                ));
            }
            let number = self.advance();
            let value = match &number.literal {
                Value::Number(n) if n.is_f64() => n
                    .as_f64()
                    .map(|f| -f)
                    .and_then(Number::from_f64)
                    .map(Value::Number),
                Value::Number(n) => match n.as_i64() {
                    Some(i) => Some(Value::from(-i)),
                    None => n
                        .as_f64()
                        .map(|f| -f)
                        .and_then(Number::from_f64)
                        .map(Value::Number),
                },
                _ => None,
            };
            return Ok(Expr::Literal {
                value: value.unwrap_or(Value::Null),
                offset,
            });
        }
        let primary = self.primary()?;
        self.postfix(primary)
    }

    fn postfix(&self, expression: Expr) -> Parsed<Expr> {
        if self.is_op(&["["]) {
            return Err(self.fail("expr.subscript", None, &[]));
        }
        let is_call = matches!(expression, Expr::Call(_));
        if self.is_op(&["("]) && !is_call {
            return Err(self.fail(
                "expr.syntax",
                None,
                &[("detail", s("only a path can be called"))],
            ));
        }
        if is_call && self.is_op(&["."]) {
            return Err(self.fail(
                "expr.syntax",
                None,
                &[(
                    "detail",
                    s("a call's result has no fields; read them from its record"),
                )],
            ));
        }
        Ok(expression)
    }

    fn primary(&mut self) -> Parsed<Expr> {
        let token = self.token().clone();
        match token.kind {
            TokenKind::Number | TokenKind::Str => {
                self.advance();
                Ok(Expr::Literal {
                    value: token.literal,
                    offset: token.offset,
                })
            }
            TokenKind::Name => match token.value.as_str() {
                "true" | "false" => {
                    self.advance();
                    Ok(Expr::Literal {
                        value: Value::Bool(token.value == "true"),
                        offset: token.offset,
                    })
                }
                "null" => {
                    self.advance();
                    Ok(Expr::Literal {
                        value: Value::Null,
                        offset: token.offset,
                    })
                }
                "undefined" | "function" | "new" | "await" | "async" | "typeof" | "this"
                | "let" | "const" | "var" => Err(self.fail(
                    "expr.syntax",
                    None,
                    &[(
                        "detail",
                        s(format!(
                            "{} is not part of the blok language",
                            repr_str(&token.value)
                        )),
                    )],
                )),
                _ => self.path_or_call(),
            },
            TokenKind::Op if token.value == "(" => {
                self.advance();
                let inner = self.expression()?;
                self.expect(")")?;
                if self.is_op(&["=>"]) {
                    return Err(self.fail("expr.lambda", None, &[]));
                }
                Ok(inner)
            }
            TokenKind::Op if token.value == "[" => self.list_literal(),
            TokenKind::Op if token.value == "{" => self.object_literal(),
            _ => Err(self.unexpected("expected a value")),
        }
    }

    fn path_or_call(&mut self) -> Parsed<Expr> {
        let first = self.advance();
        let mut segments = vec![first.value.clone()];
        while self.is_op(&["."]) {
            self.advance();
            if self.token().kind != TokenKind::Name {
                return Err(self.unexpected("expected a name after '.'"));
            }
            segments.push(self.advance().value);
        }
        if self.is_op(&["=>"]) {
            return Err(self.fail("expr.lambda", None, &[]));
        }
        if !self.is_op(&["("]) {
            return Ok(Expr::Path {
                segments,
                offset: first.offset,
            });
        }
        self.advance();
        let mut arguments = vec![];
        while !self.is_op(&[")"]) {
            arguments.push(self.argument()?);
            if !self.is_op(&[","]) {
                break;
            }
            self.advance();
        }
        self.expect(")")?;
        Ok(Expr::Call(CallExpr {
            callee: segments,
            arguments,
            offset: first.offset,
        }))
    }

    fn argument(&mut self) -> Parsed<Argument> {
        let token = self.token().clone();
        let next = self.peek().clone();
        if token.kind == TokenKind::Name && next.kind == TokenKind::Op && next.value == ":" {
            self.advance();
            self.advance();
            let value = self.expression()?;
            return Ok(Argument {
                name: Some(token.value),
                value,
                offset: token.offset,
            });
        }
        if token.kind == TokenKind::Name && next.kind == TokenKind::Op && next.value == "=" {
            return Err(self.fail(
                "expr.syntax",
                Some(next.offset),
                &[(
                    "detail",
                    s(format!("name an argument with ':' ({}: ...)", token.value)),
                )],
            ));
        }
        let value = self.expression()?;
        Ok(Argument {
            name: None,
            value,
            offset: token.offset,
        })
    }

    fn list_literal(&mut self) -> Parsed<Expr> {
        let offset = self.advance().offset;
        let mut items = vec![];
        while !self.is_op(&["]"]) {
            items.push(self.expression()?);
            if !self.is_op(&[","]) {
                break;
            }
            self.advance();
        }
        self.expect("]")?;
        Ok(Expr::List { items, offset })
    }

    fn object_literal(&mut self) -> Parsed<Expr> {
        let offset = self.advance().offset;
        let mut entries: Vec<(String, Expr)> = vec![];
        while !self.is_op(&["}"]) {
            let key = self.token().clone();
            let name = match (&key.kind, &key.literal) {
                (TokenKind::Name, _) => key.value.clone(),
                (TokenKind::Str, Value::String(literal)) if !literal.is_empty() => literal.clone(),
                _ => return Err(self.unexpected("expected a key (a name or a non-empty string)")),
            };
            self.advance();
            if entries.iter().any(|(seen, _)| *seen == name) {
                return Err(self.fail("expr.duplicate_key", Some(key.offset), &[("key", s(name))]));
            }
            self.expect(":")?;
            let value = self.expression()?;
            entries.push((name, value));
            if !self.is_op(&[","]) {
                break;
            }
            self.advance();
        }
        self.expect("}")?;
        Ok(Expr::Object { entries, offset })
    }
}

/// Parse a value expression (no `into`); `base` is where `text` starts in the source `locate`
/// reads.
pub fn parse_expression(text: &[char], base: usize, locate: &Locator) -> Parsed<Expr> {
    let mut parser = Parser::new(text, base, locate)?;
    if parser.token().kind == TokenKind::End {
        return Err(parser.fail(
            "expr.syntax",
            None,
            &[("detail", s("the expression is empty"))],
        ));
    }
    let expression = parser.expression()?;
    parser.finish()?;
    Ok(expression)
}

/// Parse an attribute's expression: a value, or a callback (`<call> into <name>`).
pub fn parse_attribute(text: &[char], base: usize, locate: &Locator) -> Parsed<Expr> {
    let mut parser = Parser::new(text, base, locate)?;
    if parser.token().kind == TokenKind::End {
        return Err(parser.fail(
            "expr.syntax",
            None,
            &[("detail", s("the expression is empty"))],
        ));
    }
    parser.callback()
}
