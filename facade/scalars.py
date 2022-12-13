from graphene.types import Scalar
from graphene.types.generic import GenericScalar
from graphql.language import ast
import graphene


class QString(Scalar):
    """A q-string is a universal identifier for a node on the
    arkitekt platform, its a hash of the node's name and the
    and its functional signature.
    """

    @staticmethod
    def serialize(dt):
        return dt

    @staticmethod
    def parse_literal(node):
        if isinstance(node, ast.StringValue):
            return node.value

    @staticmethod
    def parse_value(value):
        return value


class Any(GenericScalar):
    """Any any field"""


class AnyInput(GenericScalar):
    """Any any field"""


class SearchQuery(graphene.String):
    """Search query"""


class Identifier(graphene.String):
    """A unique Structure identifier"""

