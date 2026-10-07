# The embedding model's weights (`crates/facade/src/embeddings.rs`): takt embeds the actions it
# registers with the model the server embeds a search with, so both images carry the same
# weights. Fetched in a stage of their own, so nothing but the model's name re-downloads them.
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS weights
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*
ARG EMBEDDINGS_MODEL=minishlab/potion-base-8M
COPY scripts/bake-embeddings.sh /bake-embeddings.sh
RUN /bake-embeddings.sh bake "$EMBEDDINGS_MODEL" /opt/models/embeddings

# The build stage runs on the builder's own architecture and cross-compiles for the target.
# Compiling Rust under emulation kept the arm64 leg of a release busy for the better part of
# an hour, and the image is only published once every platform is built.
FROM --platform=$BUILDPLATFORM rust:1.93-slim AS build
ARG TARGETARCH
WORKDIR /src
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) triple=x86_64-unknown-linux-gnu; gnu=x86_64-linux-gnu; packages="gcc-x86-64-linux-gnu libc6-dev-amd64-cross" ;; \
      arm64) triple=aarch64-unknown-linux-gnu; gnu=aarch64-linux-gnu; packages="gcc-aarch64-linux-gnu libc6-dev-arm64-cross" ;; \
      *) echo "takt is not built for $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    echo "$triple" > /triple; \
    if [ "$(rustc -vV | sed -n 's/^host: //p')" != "$triple" ]; then \
      rustup target add "$triple"; \
      apt-get update; \
      apt-get install -y --no-install-recommends $packages; \
      rm -rf /var/lib/apt/lists/*; \
      printf '[target.%s]\nlinker = "%s-gcc"\n' "$triple" "$gnu" >> "$CARGO_HOME/config.toml"; \
    fi
COPY . .
ARG EMBEDDINGS_MODEL=minishlab/potion-base-8M
RUN scripts/bake-embeddings.sh check "$EMBEDDINGS_MODEL"
RUN cargo build --release --bin takt --target "$(cat /triple)" \
    && cp "target/$(cat /triple)/release/takt" /takt

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /takt /usr/local/bin/takt
COPY --from=weights /opt/models /opt/models
ENV TAKT_CONFIG=/workspace/config.yaml
EXPOSE 8080
# Healthy once it serves: it waits for the rekuest server's migrations before that.
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=24 CMD ["takt", "healthcheck"]
CMD ["takt"]
