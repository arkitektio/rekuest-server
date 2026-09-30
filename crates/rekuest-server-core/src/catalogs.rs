//! The base catalog: the pure operations every UI implements for port calls
//! (`rekuest_core/catalogs/__init__.py`).
//!
//! `base_v1.json` is the source of truth, embedded byte for byte as the Python server ships it.
//! The base catalog is virtual: never a `UICatalog` row, merged under every named catalog, and
//! UI catalogs may not redefine its operation names.

use std::collections::BTreeMap;
use std::sync::OnceLock;

use serde::Deserialize;

use crate::inputs::{CatalogOperationInputModel, ValidationError};

pub const BASE_CATALOG_NAME: &str = "base";
pub const BASE_CATALOG_VERSION: i64 = 1;
/// How the base catalog is referred to (`base@1`).
pub const BASE_CATALOG_ID: &str = "base@1";

const MANIFEST: &str = include_str!("catalogs/base_v1.json");

/// The base version a catalog name refers to: `base` → current, `base@3` → 3, other names → none.
pub fn base_version_named(name: &str) -> Option<i64> {
    if name == BASE_CATALOG_NAME {
        return Some(BASE_CATALOG_VERSION);
    }
    let digits = name.strip_prefix("base@")?;
    if !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit()) {
        digits.parse().ok()
    } else {
        None
    }
}

/// The parsed manifest (`BaseCatalogModel`).
#[derive(Debug, Deserialize)]
pub struct BaseCatalogModel {
    pub name: String,
    pub version: i64,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub operations: Vec<CatalogOperationInputModel>,
}

fn load() -> Result<BaseCatalogModel, ValidationError> {
    let catalog: BaseCatalogModel =
        serde_json::from_str(MANIFEST).map_err(|e| ValidationError(format!("base catalog: {e}")))?;
    if catalog.name != BASE_CATALOG_NAME {
        return Err(ValidationError(format!("base catalog is named {:?}", catalog.name)));
    }
    let mut seen = std::collections::HashSet::new();
    for operation in &catalog.operations {
        operation.validate()?;
        if !seen.insert(operation.name.as_str()) {
            return Err(ValidationError(format!("base catalog: duplicate name {:?}", operation.name)));
        }
    }
    Ok(catalog)
}

/// The validated manifest (loaded once; a broken manifest is a startup failure).
pub fn load_base_catalog() -> &'static BaseCatalogModel {
    static CATALOG: OnceLock<BaseCatalogModel> = OnceLock::new();
    CATALOG.get_or_init(|| load().expect("the embedded base catalog is valid"))
}

/// Base operations by name.
pub fn base_operations() -> BTreeMap<String, CatalogOperationInputModel> {
    load_base_catalog()
        .operations
        .iter()
        .map(|operation| (operation.name.clone(), operation.clone()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_embedded_manifest_loads() {
        let catalog = load_base_catalog();
        assert_eq!(catalog.version, BASE_CATALOG_VERSION);
        assert!(!catalog.operations.is_empty());
        assert_eq!(base_version_named("base"), Some(1));
        assert_eq!(base_version_named("base@3"), Some(3));
        assert_eq!(base_version_named("base@x"), None);
        assert_eq!(base_version_named("mine"), None);
    }
}
