//! The embedding of an action's name and description (the Python server's `embeddings` app).
//!
//! takt writes actions with SQL of its own, so no model's `save()` embeds them: it does that
//! here, with the same model the server embeds a search query with, and writes the vector in
//! the statement that writes the row. Registering an action and renaming one are thereby
//! both covered, and nothing has to come by afterwards to fill a vector in.
//!
//! The model is a constant of the release, as it is in `embeddings/engine.py`: a new one only
//! ever ships with a new image of both halves, which is why neither names it in its config.
//! The weights are baked into the image (`scripts/bake-embeddings.sh`); the two halves have
//! to produce the same vector for the same text, which the conformance suite holds them to.

use std::path::Path;
use std::sync::OnceLock;

use model2vec_rs::model::StaticModel;

/// The model2vec model this release embeds with (`embeddings.engine.MODEL`).
pub const MODEL: &str = "minishlab/potion-base-8M";
/// The width of its vectors, and of the `vector(N)` column (`embeddings.engine.DIMENSIONS`).
pub const DIMENSIONS: usize = 256;
/// Where the image holds the weights (`embeddings.engine.MODEL_PATH`).
pub const MODEL_PATH: &str = "/opt/models/embeddings";
/// The file beside the weights naming the model they are of.
pub const MODEL_ID_FILENAME: &str = "MODEL_ID";

/// The loaded model, once [`start`] ran: `None` while embeddings are off or there are no
/// weights to load.
static MODEL_LOADED: OnceLock<Option<StaticModel>> = OnceLock::new();

/// Why the weights at a path are not this release's.
#[derive(Debug, thiserror::Error)]
pub enum Refused {
    #[error("{path} holds the weights of {baked:?} but this release embeds with {MODEL:?}: rebuild the image")]
    AnotherModel { path: String, baked: String },
    #[error("the weights at {path} could not be loaded: {reason}")]
    Unloadable { path: String, reason: String },
    #[error(
        "the model at {path} produces {found}-wide vectors but the column is {DIMENSIONS} wide"
    )]
    AnotherWidth { path: String, found: usize },
}

/// Load the weights at `path`, refusing ones that are not this release's.
pub fn load(path: &Path) -> Result<StaticModel, Refused> {
    let shown = path.display().to_string();
    if let Ok(baked) = std::fs::read_to_string(path.join(MODEL_ID_FILENAME)) {
        let baked = baked.trim();
        if baked != MODEL {
            return Err(Refused::AnotherModel {
                path: shown,
                baked: baked.to_owned(),
            });
        }
    }
    let model = StaticModel::from_pretrained(path, None, None, None).map_err(|error| {
        Refused::Unloadable {
            path: shown.clone(),
            reason: error.to_string(),
        }
    })?;
    let found = model.encode_single("width").len();
    if found != DIMENSIONS {
        return Err(Refused::AnotherWidth { path: shown, found });
    }
    Ok(model)
}

/// Load the model for this process, once, at its start.
///
/// Off (`embeddings.enabled: false`), nothing is loaded and no action is embedded. Without
/// weights at `path` — a developer's machine, a test — the same, said once. Weights that are
/// another model's, or of another width, stop the start: every vector written would be wrong.
pub fn start(enabled: bool, path: &Path) -> Result<(), Refused> {
    let model = if !enabled {
        None
    } else if !path.is_dir() {
        tracing::warn!(
            "no embedding weights at {}: actions are registered without a vector",
            path.display()
        );
        None
    } else {
        let model = load(path)?;
        tracing::info!(
            "embedding model {MODEL} loaded ({DIMENSIONS} dims) from {}",
            path.display()
        );
        Some(model)
    };
    // A second start keeps the first: one process, one model.
    let _ = MODEL_LOADED.set(model);
    Ok(())
}

/// The text a row is embedded from: its source fields, each stripped, newline-joined
/// (`embeddings.engine.source_text`). `None` when they are all blank.
pub fn source_text(parts: &[Option<&str>]) -> Option<String> {
    let parts: Vec<&str> = parts
        .iter()
        .filter_map(|part| part.map(str::trim))
        .filter(|part| !part.is_empty())
        .collect();
    (!parts.is_empty()).then(|| parts.join("\n"))
}

/// The unit-length vector of `text` by `model`, as pgvector reads one (`[0.1,0.2,…]`), or
/// `None` where the model gave zeros: text made only of tokens it does not know. Cosine
/// distance to a zero vector is NaN, so such a row stores `NULL`.
pub fn vector_of(model: &StaticModel, text: &str) -> Option<String> {
    let row = model.encode_single(text);
    let norm = row.iter().map(|value| value * value).sum::<f32>().sqrt();
    if !norm.is_finite() || norm == 0.0 {
        return None;
    }
    let values: Vec<String> = row.iter().map(|value| (value / norm).to_string()).collect();
    Some(format!("[{}]", values.join(",")))
}

/// What to write into a row's `embedding` for these source fields: the vector as pgvector
/// reads it (bind it as `$n::vector`), or `None` — nothing to embed, embeddings off, or no
/// model in this process.
pub fn embedding(parts: &[Option<&str>]) -> Option<String> {
    let model = MODEL_LOADED.get()?.as_ref()?;
    vector_of(model, &source_text(parts)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_source_text_is_the_stripped_fields_on_lines_of_their_own() {
        assert_eq!(
            source_text(&[Some(" Segment "), Some("Finds nuclei\n")]).as_deref(),
            Some("Segment\nFinds nuclei")
        );
        assert_eq!(
            source_text(&[Some("Segment"), None]).as_deref(),
            Some("Segment")
        );
        assert_eq!(
            source_text(&[Some("Segment"), Some("  ")]).as_deref(),
            Some("Segment")
        );
    }

    #[test]
    fn nothing_but_blanks_is_no_text() {
        assert_eq!(source_text(&[Some("  "), None, Some("")]), None);
    }

    #[test]
    fn a_process_that_loaded_no_model_embeds_nothing() {
        // No test starts the embedder: registration then writes NULL, as with embeddings off.
        assert_eq!(embedding(&[Some("Segment"), Some("Finds nuclei")]), None);
    }

    /// Against the weights themselves, where there are some (`TAKT_EMBEDDINGS_PATH`): the
    /// image's, or a directory `scripts/bake-embeddings.sh` filled.
    #[test]
    fn the_baked_weights_give_unit_vectors_of_the_columns_width() {
        let Ok(path) = std::env::var("TAKT_EMBEDDINGS_PATH") else {
            return;
        };
        let model = load(Path::new(&path)).expect("the release's weights");
        let vector = vector_of(&model, "Segment nuclei\nFinds nuclei").expect("a vector");
        let values: Vec<f32> = vector
            .trim_matches(['[', ']'])
            .split(',')
            .map(|value| value.parse().expect("a number"))
            .collect();
        assert_eq!(values.len(), DIMENSIONS);
        let norm = values.iter().map(|value| value * value).sum::<f32>().sqrt();
        assert!((norm - 1.0).abs() < 1e-4, "norm {norm}");
    }
}
