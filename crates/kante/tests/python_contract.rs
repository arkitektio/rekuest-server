//! kante and Python's channels_redis on one redis: each receives what the other sends.
//! Needs `AGENTD_TEST_REDIS_URL` (`eval "$(scripts/test-db.sh)"`) and the rekuest server's venv
//! (`KANTE_PYTHON`, default the server worktree's); skipped without the redis URL.

use std::process::Stdio;
use std::time::Duration;

use kante::{ChannelLayer, ChannelLayerConfig};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

const PREFIX: &str = "kante-contract";

fn redis_url() -> Option<String> {
    std::env::var("AGENTD_TEST_REDIS_URL").ok()
}

fn python() -> String {
    std::env::var("KANTE_PYTHON").unwrap_or_else(|_| {
        "/home/jhnnsrs/Code/worktrees/rekuest-server-workflows/.venv/bin/python".into()
    })
}

fn peer() -> String {
    format!("{}/tests/python/peer.py", env!("CARGO_MANIFEST_DIR"))
}

async fn layer(url: &str) -> ChannelLayer {
    ChannelLayer::new(
        redis::Client::open(url).unwrap(),
        ChannelLayerConfig {
            prefix: PREFIX.into(),
            capacity: 5000,
            ..ChannelLayerConfig::default()
        },
    )
    .await
    .unwrap()
}

/// What a kante broadcast looks like: a type and a JSON payload with every JSON kind in it.
fn message() -> Value {
    json!({
        "type": "channel.TaskEventCreatedEvent",
        "message": {
            "event": {"id": "12", "task": "7", "kind": "YIELD", "progress": 50, "returns": {"a": [1, 2.5, "x", true, null]},
                      "message": null, "level": null, "value": null, "created_at": "2026-09-30T09:44:08.960420Z"},
            "create": null,
        },
    })
}

#[tokio::test]
async fn python_receives_what_rust_group_sends() {
    let Some(url) = redis_url() else { return };
    let group = format!("contract_{}", uuid::Uuid::new_v4().simple());
    let mut child = Command::new(python())
        .args([
            peer(),
            "receive".into(),
            url.clone(),
            PREFIX.into(),
            group.clone(),
        ])
        .stdout(Stdio::piped())
        .spawn()
        .expect("the python peer starts");
    let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
    assert_eq!(lines.next_line().await.unwrap().as_deref(), Some("READY"));

    layer(&url)
        .await
        .group_send(&group, &message())
        .await
        .unwrap();

    let received = tokio::time::timeout(Duration::from_secs(20), lines.next_line())
        .await
        .expect("python received in time")
        .unwrap()
        .expect("a line");
    assert_eq!(serde_json::from_str::<Value>(&received).unwrap(), message());
    assert!(child.wait().await.unwrap().success());
}

#[tokio::test]
async fn rust_receives_what_python_group_sends() {
    let Some(url) = redis_url() else { return };
    let group = format!("contract_{}", uuid::Uuid::new_v4().simple());
    let layer = layer(&url).await;
    let (channel, mut inbox) = layer.subscribe("specific").await.unwrap();
    layer.group_add(&group, &channel).await.unwrap();

    let status = Command::new(python())
        .args([
            peer(),
            "send".into(),
            url,
            PREFIX.into(),
            group.clone(),
            message().to_string(),
        ])
        .status()
        .await
        .unwrap();
    assert!(status.success());

    let received = tokio::time::timeout(Duration::from_secs(20), inbox.recv())
        .await
        .expect("rust received in time")
        .expect("a message");
    assert_eq!(received, message());
    layer.group_discard(&group, &channel).await.unwrap();
}

#[tokio::test]
async fn a_group_reaches_every_member_once() {
    let Some(url) = redis_url() else { return };
    let group = format!("contract_{}", uuid::Uuid::new_v4().simple());
    let layer = layer(&url).await;
    let (a, mut inbox_a) = layer.subscribe("specific").await.unwrap();
    let (b, mut inbox_b) = layer.subscribe("specific").await.unwrap();
    layer.group_add(&group, &a).await.unwrap();
    layer.group_add(&group, &b).await.unwrap();

    kante::channel::broadcast(
        &layer,
        "Ping",
        &json!({"n": 1}),
        std::slice::from_ref(&group),
    )
    .await
    .unwrap();

    for inbox in [&mut inbox_a, &mut inbox_b] {
        let got = tokio::time::timeout(Duration::from_secs(10), inbox.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            kante::channel::payload_of(&got, "Ping"),
            Some(&json!({"n": 1}))
        );
    }
    assert!(
        tokio::time::timeout(Duration::from_millis(300), inbox_a.recv())
            .await
            .is_err(),
        "once each"
    );
}
