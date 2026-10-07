#!/bin/sh
# Bake the embedding model's weights into the image, and check they are the release's.
# The image's part of `crates/facade/src/embeddings.rs`; the Python server has the same in
# `embeddings/scripts/bake.py`. Run by the Dockerfile, in two steps:
#
#   bake-embeddings.sh bake MODEL TARGET   downloads MODEL's weights from Hugging Face into
#                                          TARGET and writes the model's id beside them
#   bake-embeddings.sh check MODEL         fails unless MODEL is the one the source embeds
#                                          with: the model is a constant of the release, not
#                                          a build option
set -eu

case "${1:-}" in
  bake)
    model="$2"
    target="$3"
    mkdir -p "$target"
    # What a model2vec model is loaded from, as `save_pretrained` lays it out.
    for file in config.json tokenizer.json model.safetensors; do
      curl --fail --silent --show-error --location --retry 3 \
        "https://huggingface.co/$model/resolve/main/$file" --output "$target/$file"
    done
    printf '%s' "$model" > "$target/MODEL_ID"
    ;;
  check)
    model="$2"
    source="$(dirname "$0")/../crates/facade/src/embeddings.rs"
    if ! grep -qF "pub const MODEL: &str = \"$model\";" "$source"; then
      echo "the image bakes $model, but $source embeds with another model" >&2
      exit 1
    fi
    ;;
  *)
    sed -n '2,10p' "$0" >&2
    exit 2
    ;;
esac
