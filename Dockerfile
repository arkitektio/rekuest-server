FROM rust:1.93-slim AS build
WORKDIR /src
COPY . .
RUN cargo build --release --bin agentd

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/agentd /usr/local/bin/agentd
ENV AGENTD_CONFIG=/workspace/config.yaml
EXPOSE 8080
CMD ["agentd"]
