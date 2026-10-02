//! Derive a provenance audience from an action's ports (`facade/provenance/audience.py`).
//!
//! A token's `aud` is the downstream services an implementation touches: the structure
//! identifiers of its arg and return ports (`@mikro/image` → `mikro`). Resolved once, at
//! registration, and stored as `Implementation.provenance_audience`; minting only reads it.

use serde_json::Value;

/// The owning service of a structure identifier like `@mikro/image` (`service_from_identifier`).
pub fn service_from_identifier(identifier: &str) -> Option<String> {
    let s = identifier.trim();
    let s = s.strip_prefix('@').unwrap_or(s);
    let s = s.split_once('/').map_or(s, |(service, _)| service);
    (!s.is_empty()).then(|| s.to_owned())
}

fn collect_identifiers<'a>(ports: &'a [Value], acc: &mut Vec<&'a str>) {
    for port in ports.iter().filter_map(Value::as_object) {
        if let Some(identifier) = port
            .get("identifier")
            .and_then(Value::as_str)
            .filter(|i| !i.is_empty())
        {
            acc.push(identifier);
        }
        if let Some(children) = port.get("children").and_then(Value::as_array) {
            collect_identifiers(children, acc);
        }
    }
}

/// The de-duplicated services the given port lists reference, in order (`services_from_ports`).
pub fn services_from_ports(port_lists: &[&[Value]]) -> Vec<String> {
    let mut identifiers = vec![];
    for ports in port_lists {
        collect_identifiers(ports, &mut identifiers);
    }
    let mut services: Vec<String> = vec![];
    for service in identifiers.into_iter().filter_map(service_from_identifier) {
        if !services.contains(&service) {
            services.push(service);
        }
    }
    services
}

/// The audience of an action: its arg and return structure ports (`derive_from_action`).
pub fn derive_from_action(args: &[Value], returns: &[Value]) -> Vec<String> {
    services_from_ports(&[args, returns])
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn services_come_from_nested_identifiers_once() {
        let args = json!([
            {"key": "image", "identifier": "@mikro/image"},
            {"key": "list", "children": [{"key": "0", "identifier": "@kabinet/pod"}]},
        ]);
        let returns = json!([{"key": "return0", "identifier": "mikro/table"}]);
        assert_eq!(
            derive_from_action(args.as_array().unwrap(), returns.as_array().unwrap()),
            vec!["mikro", "kabinet"]
        );
        assert_eq!(service_from_identifier("@"), None);
    }
}
