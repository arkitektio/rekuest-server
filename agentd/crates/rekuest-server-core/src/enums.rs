//! The enumerations of a declaration (`rekuest_core/enums.py`). Serialized as their names.

use serde::{Deserialize, Serialize};

macro_rules! wire_enum {
    ($(#[$meta:meta])* $name:ident { $($variant:ident),+ $(,)? }) => {
        $(#[$meta])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
        #[allow(non_camel_case_types, clippy::upper_case_acronyms)]
        pub enum $name { $($variant),+ }

        impl $name {
            /// The wire value (the Python enum's value).
            pub fn value(self) -> &'static str {
                match self { $(Self::$variant => stringify!($variant)),+ }
            }
        }

        impl std::fmt::Display for $name {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str(self.value())
            }
        }
    };
}

wire_enum!(
    /// The kind of action.
    ActionKind { FUNCTION, GENERATOR }
);
wire_enum!(
    /// A port's structural type: decides which of children, identifier and choices it carries.
    PortKind { INT, STRING, STRUCTURE, LIST, BOOL, DICT, FLOAT, DATE, UNION, ENUM, MODEL, MEMORY_STRUCTURE, INTERFACE, QUANTITY }
);
wire_enum!(
    /// The kind of assign widget.
    AssignWidgetKind { SEARCH, CHOICE, SLIDER, CUSTOM, STRING, STATE_CHOICE, PROXY }
);
wire_enum!(
    /// The kind of return widget.
    ReturnWidgetKind { CHOICE, CUSTOM }
);
wire_enum!(
    /// The kind of a port effect.
    EffectKind { MESSAGE, HIDE, CUSTOM }
);
wire_enum!(
    /// What running an implementation again would do to the world: informational only.
    Effects { NONE, REPEATABLE, UNKNOWN, IRREVERSIBLE }
);
wire_enum!(
    /// How an implementation runs: a WORKFLOW may call other actions and is resumed.
    Execution { PLAIN, WORKFLOW }
);
wire_enum!(
    /// The kind of action scope.
    ActionScope { GLOBAL, LOCAL, BRIDGE_GLOBAL_TO_LOCAL, BRIDGE_LOCAL_TO_GLOBAL }
);
wire_enum!(
    /// How a requires/provides descriptor compares the object's value at `key` with `value`.
    DescriptorOperator { MATCHES, EXISTS, LTE, GTE, EQUALS, CONTAINS, NOT_EQUALS, IN, NOT_IN }
);
wire_enum!(
    /// The part of a state entry a state accessor reads.
    OptionKey { LABEL, DESCRIPTION, LOGO, VALUE }
);
wire_enum!(
    /// What a catalog component prop accepts, or an operation argument carries.
    CatalogValueKind { STRING, INT, FLOAT, BOOL, DICT, LIST, ANY, CALLBACK }
);
wire_enum!(
    /// Severity of a registration finding: errors abort registration, so only WARNING is stored.
    DiagnosticLevel { WARNING }
);
wire_enum!(
    /// Aggregation computed over a tracked value within a window.
    WindowFunction { MEAN, MIN, MAX, SUM, COUNT, LAST, FIRST, STD }
);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn enums_travel_as_their_names() {
        assert_eq!(
            serde_json::to_value(PortKind::MEMORY_STRUCTURE).unwrap(),
            "MEMORY_STRUCTURE"
        );
        assert_eq!(
            serde_json::from_value::<DescriptorOperator>("NOT_IN".into()).unwrap(),
            DescriptorOperator::NOT_IN
        );
        assert_eq!(Effects::IRREVERSIBLE.value(), "IRREVERSIBLE");
    }
}
