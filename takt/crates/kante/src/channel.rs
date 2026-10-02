//! Typed broadcasts (`kante/channel.py`): a channel's messages are
//! `{"type": "channel.<name>", "message": <payload as JSON>}`, sent to each of its groups.

use serde::Serialize;
use serde_json::Value;

use crate::core::ChannelLayer;
use crate::errors::KanteError;

/// The message type of the channel `name` (`Channel.message_type`).
pub fn message_type(name: &str) -> String {
    format!("channel.{name}")
}

/// Send `payload` on the channel `name` to every group in `groups` (`Channel.abroadcast`).
pub async fn broadcast(
    layer: &ChannelLayer,
    name: &str,
    payload: &impl Serialize,
    groups: &[String],
) -> Result<(), KanteError> {
    let message = serde_json::json!({
        "type": message_type(name),
        "message": serde_json::to_value(payload).map_err(|e| KanteError::Serialize(e.to_string()))?,
    });
    for group in groups {
        layer.group_send(group, &message).await?;
    }
    Ok(())
}

/// The payload of a received channel message, if it is of the channel `name`.
pub fn payload_of<'a>(message: &'a Value, name: &str) -> Option<&'a Value> {
    (message.get("type")?.as_str()? == message_type(name))
        .then(|| message.get("message"))
        .flatten()
}
