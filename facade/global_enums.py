import graphene


class LogicalCondition(graphene.Enum):
    IS = "IS"
    IS_NOT = "IS_NOT"
    IN = "IN"


class EffectKind(graphene.Enum):
    HIDDEN = "HIDDEN"
    HIGHLIGHT = "HIGHLIGHT"
    WARN = "WARN"
    CRAZY = "CRAZY"
