//! Every redis key this service owns, under one configurable namespace (`facade/redis_keys.py`).
//!
//! Redis is shared infrastructure: the channel layer, the agent queues, probe state and the
//! reaper's tick token all live on one instance, next to every other arkitekt service, and
//! in some setups next to a second rekuest deployment. `REDIS_KEY_PREFIX` scopes them.

use std::fmt::Display;

use crate::settings::Settings;

/// `{prefix}:{part}:{part}…`: the only way first-party code should name a redis key.
pub fn key(settings: &Settings, parts: &[&dyn Display]) -> String {
    let prefix = if settings.redis_key_prefix.is_empty() {
        "rekuest"
    } else {
        &settings.redis_key_prefix
    };
    std::iter::once(prefix.to_owned())
        .chain(parts.iter().map(|part| part.to_string()))
        .collect::<Vec<_>>()
        .join(":")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_are_namespaced_like_pythons() {
        let settings = Settings {
            redis_key_prefix: "next:rekuest".into(),
            ..Settings::default()
        };
        assert_eq!(
            key(&settings, &[&"agent", &42, &"queue"]),
            "next:rekuest:agent:42:queue"
        );
        let empty = Settings {
            redis_key_prefix: String::new(),
            ..Settings::default()
        };
        assert_eq!(key(&empty, &[&"reaper", &"tick"]), "rekuest:reaper:tick");
    }
}
