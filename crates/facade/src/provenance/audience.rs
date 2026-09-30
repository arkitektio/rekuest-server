//! The services an implementation's provenance token is scoped to, derived from its action's
//! structure ports (`facade/provenance/audience.py`): `@mikro/image` → `mikro`.

use serde_json::Value;

/// The owning service of a structure identifier (`service_from_identifier`).
pub fn service_from_identifier(identifier: &str) -> Option<String> {
    let s = identifier.trim();
    let s = s.strip_prefix('@').unwrap_or(s);
    let s = s.split_once('/').map_or(s, |(service, _)| service);
    Some(s.to_owned()).filter(|s| !s.is_empty())
}

fn collect_identifiers<'a>(ports: &'a Value, acc: &mut Vec<&'a str>) {
    for port in ports.as_array().into_iter().flatten() {
        let Some(port) = port.as_object() else {
            continue;
        };
        if let Some(identifier) = port
            .get("identifier")
            .and_then(Value::as_str)
            .filter(|i| !i.is_empty())
        {
            acc.push(identifier);
        }
        if let Some(children) = port.get("children") {
            collect_identifiers(children, acc);
        }
    }
}

/// The de-duplicated services the port lists reference, in first-seen order (`services_from_ports`).
pub fn services_from_ports(port_lists: &[&Value]) -> Vec<String> {
    let mut identifiers = vec![];
    for ports in port_lists {
        collect_identifiers(ports, &mut identifiers);
    }
    let mut services: Vec<String> = vec![];
    for identifier in identifiers {
        if let Some(service) = service_from_identifier(identifier) {
            if !services.contains(&service) {
                services.push(service);
            }
        }
    }
    services
}

/// The audience of an action: the services of its arg and return ports (`derive_from_action`).
pub fn derive_from_action(args: &Value, returns: &Value) -> Vec<String> {
    services_from_ports(&[args, returns])
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn services_come_from_nested_identifiers() {
        let args =
            json!([{"identifier": "@mikro/image"}, {"children": [{"identifier": "@kabinet/pod"}]}]);
        let returns = json!([{"identifier": "@mikro/roi"}, {"identifier": null}]);
        assert_eq!(
            derive_from_action(&args, &returns),
            vec!["mikro", "kabinet"]
        );
        assert_eq!(service_from_identifier(" @x "), Some("x".into()));
        assert_eq!(service_from_identifier("@/y"), None);
    }
}
