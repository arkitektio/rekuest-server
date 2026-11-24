//! Whether a value of one catalog port fits where another is taken: arkitekt-spec's
//! `declare/catalogs/fit.py`, replayed from the corpus (`fits`).
//!
//! `ok`: it fits; `open`: an open side, nothing to say; `mismatch`: both concrete and disjoint.
//! Rules, in order: open sides; a UNION target takes a source fitting one variant, a UNION
//! source fits when every variant does; widening (INT, QUANTITY → FLOAT; ENUM, DATE → STRING;
//! MODEL → DICT; STRUCTURE → DICT is open); the same kind recurses; choices are a subset.
//! `nullable` is not compared.

use std::collections::{BTreeMap, HashSet};

use crate::enums::PortKind;
use crate::inputs::{CatalogChoiceInputModel, CatalogPortInputModel, ITEM_KEY};
use crate::pyjson::{repr, repr_str};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FitStatus {
    Ok,
    Open,
    Mismatch,
}

impl FitStatus {
    /// As the corpus spells it.
    pub fn as_str(self) -> &'static str {
        match self {
            FitStatus::Ok => "ok",
            FitStatus::Open => "open",
            FitStatus::Mismatch => "mismatch",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fit {
    pub status: FitStatus,
    pub reason: Option<String>,
}

impl Fit {
    const OK: Fit = Fit {
        status: FitStatus::Ok,
        reason: None,
    };
    const OPEN: Fit = Fit {
        status: FitStatus::Open,
        reason: None,
    };

    fn mismatch(reason: String) -> Fit {
        Fit {
            status: FitStatus::Mismatch,
            reason: Some(reason),
        }
    }

    /// Not a mismatch.
    pub fn ok(&self) -> bool {
        self.status != FitStatus::Mismatch
    }
}

fn identified(kind: Option<PortKind>) -> bool {
    matches!(
        kind,
        Some(PortKind::STRUCTURE | PortKind::MEMORY_STRUCTURE | PortKind::INTERFACE)
    )
}

fn name(port: &CatalogPortInputModel) -> String {
    match port.kind {
        kind if identified(kind) => format!(
            "{}({})",
            kind.expect("identified"),
            port.identifier.as_deref().unwrap_or_default()
        ),
        Some(kind) => kind.to_string(),
        None => "ANY".into(),
    }
}

fn widens(wanted: PortKind, kind: PortKind) -> bool {
    matches!(
        (wanted, kind),
        (PortKind::FLOAT, PortKind::INT | PortKind::QUANTITY)
            | (PortKind::STRING, PortKind::ENUM | PortKind::DATE)
            | (PortKind::DICT, PortKind::MODEL)
    )
}

/// All of them: the first mismatch, else open if any is, else ok.
fn join(fits: impl IntoIterator<Item = Fit>) -> Fit {
    let mut open = false;
    for fit in fits {
        match fit.status {
            FitStatus::Mismatch => return fit,
            FitStatus::Open => open = true,
            FitStatus::Ok => {}
        }
    }
    if open {
        Fit::OPEN
    } else {
        Fit::OK
    }
}

fn within(fit: Fit, place: &str) -> Fit {
    match fit.status {
        FitStatus::Mismatch => {
            Fit::mismatch(format!("{place}: {}", fit.reason.unwrap_or_default()))
        }
        _ => fit,
    }
}

fn choices_fit(
    source: Option<&[CatalogChoiceInputModel]>,
    target: Option<&[CatalogChoiceInputModel]>,
) -> Fit {
    let (Some(source), Some(target)) = (source, target) else {
        return Fit::OK;
    };
    let stray: Vec<String> = source
        .iter()
        .filter(|choice| !target.iter().any(|allowed| allowed.value == choice.value))
        .map(|choice| repr(&choice.value))
        .collect();
    if stray.is_empty() {
        return Fit::OK;
    }
    let allowed: Vec<String> = target.iter().map(|choice| repr(&choice.value)).collect();
    Fit::mismatch(format!(
        "{} not among {}",
        stray.join(", "),
        allowed.join(", ")
    ))
}

/// Whether a value of `source` fits where `target` is taken.
pub fn fits(source: &CatalogPortInputModel, target: &CatalogPortInputModel) -> Fit {
    let (Some(kind), Some(wanted)) = (source.kind, target.kind) else {
        return Fit::OPEN;
    };
    if wanted == PortKind::UNION {
        let results: Vec<Fit> = target
            .children
            .iter()
            .flatten()
            .map(|child| fits(source, &child.port()))
            .collect();
        if results.iter().any(|r| r.status == FitStatus::Ok) {
            return Fit::OK;
        }
        if results.iter().any(|r| r.status == FitStatus::Open) {
            return Fit::OPEN;
        }
        let variants: Vec<String> = target
            .children
            .iter()
            .flatten()
            .map(|child| name(&child.port()))
            .collect();
        return Fit::mismatch(format!(
            "{} is none of {}",
            name(source),
            variants.join(", ")
        ));
    }
    if kind == PortKind::UNION {
        return join(
            source
                .children
                .iter()
                .flatten()
                .map(|child| fits(&child.port(), target)),
        );
    }
    if kind == PortKind::STRUCTURE && wanted == PortKind::DICT {
        return Fit::OPEN;
    }
    if kind != wanted {
        if widens(wanted, kind) {
            return choices_fit(source.choices.as_deref(), target.choices.as_deref());
        }
        return Fit::mismatch(format!("a {} is not a {}", name(source), name(target)));
    }
    if identified(Some(kind)) {
        let open = [&source.identifier, &target.identifier]
            .iter()
            .any(|identifier| identifier.as_deref() == Some("*"));
        if open || source.identifier == target.identifier {
            return Fit::OK;
        }
        return Fit::mismatch(format!("a {} is not a {}", name(source), name(target)));
    }
    match kind {
        PortKind::LIST => match (item(source), item(target)) {
            (Some(given), Some(taken)) => within(fits(&given, &taken), "an item"),
            _ => Fit::OPEN,
        },
        PortKind::DICT => dict_fits(source, target),
        PortKind::MODEL => fields_fit(source, target),
        _ => choices_fit(source.choices.as_deref(), target.choices.as_deref()),
    }
}

fn item(port: &CatalogPortInputModel) -> Option<CatalogPortInputModel> {
    port.children
        .iter()
        .flatten()
        .next()
        .map(|child| child.port())
}

fn dict_fits(source: &CatalogPortInputModel, target: &CatalogPortInputModel) -> Fit {
    if let Some(taken) = target.child(ITEM_KEY) {
        let taken = taken.port();
        return join(
            source
                .children
                .iter()
                .flatten()
                .map(|child| within(fits(&child.port(), &taken), &child.key)),
        );
    }
    fields_fit(source, target)
}

fn fields_fit(source: &CatalogPortInputModel, target: &CatalogPortInputModel) -> Fit {
    let mut results = vec![];
    for field in target.children.iter().flatten() {
        match source.child(&field.key).or_else(|| source.child(ITEM_KEY)) {
            Some(given) => results.push(within(fits(&given.port(), &field.port()), &field.key)),
            None if !field.nullable => {
                results.push(Fit::mismatch(format!("no field {}", repr_str(&field.key))))
            }
            None => {}
        }
    }
    join(results)
}

/// Whether a local takes what a control writes (a BINDING's child), by the vocabulary's
/// `bind_kinds` (per kind written, the local kinds that take it).
pub fn binding_fits(
    local: &CatalogPortInputModel,
    writes: &CatalogPortInputModel,
    bind_kinds: &BTreeMap<String, HashSet<String>>,
) -> Fit {
    let (Some(kind), Some(written)) = (local.kind, writes.kind) else {
        return Fit::OPEN;
    };
    if written == PortKind::UNION {
        // The control writes one of them, by how it is set (a ToggleGroup's `multiple`).
        let results: Vec<Fit> = writes
            .children
            .iter()
            .flatten()
            .map(|child| binding_fits(local, &child.port(), bind_kinds))
            .collect();
        if results.iter().any(|r| r.status == FitStatus::Ok) {
            return Fit::OK;
        }
        if results.iter().any(|r| r.status == FitStatus::Open) {
            return Fit::OPEN;
        }
        return Fit::mismatch(format!(
            "a {} control writes none a {} local takes",
            name(writes),
            name(local)
        ));
    }
    let Some(allowed) = bind_kinds.get(written.value()) else {
        return fits(writes, local);
    };
    if !allowed.contains(kind.value()) {
        let mut names: Vec<&str> = allowed.iter().map(String::as_str).collect();
        names.sort_unstable();
        return Fit::mismatch(format!(
            "a {} control writes a {} local, not a {}",
            name(writes),
            names.join(", "),
            name(local)
        ));
    }
    if kind == written && matches!(kind, PortKind::LIST | PortKind::DICT | PortKind::MODEL) {
        return fits(writes, local);
    }
    Fit::OK
}
