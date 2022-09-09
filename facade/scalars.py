from graphene.types import Scalar
from graphene.types.generic import GenericScalar
from graphql.language import ast
import graphene


class QString(Scalar):
    """A q-string is a universal identifier for a node on the
    arkitekt platform, its akin to a npm package or pip package
    and follows the following syntax:

    @package/interface
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
