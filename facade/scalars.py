from graphene.types import Scalar
from graphene.types.generic import GenericScalar
from graphql.language import ast
import re

callback_re = re.compile(r"(?P<protocol>ws|gql|record):(?P<uuid>.*)")


class CallbackContainer:
    def __init__(self, protocol="ws", uuid="") -> None:
        self.protocol = "ws"
        self.uuid = uuid

    @classmethod
    def from_value(cls, value):
        m = callback_re.match(value)
        if m:
            return cls(protocol=m["protocol"], uuid=m["uuid"])

        raise NotImplementedError(
            f"{value} is not a valid Callback (needs to follow protocol:uuid) For a full list of supported protocols , consult the API Documentation"
        )


class Callback(Scalar):
    """A Representation of a Django File"""

    @staticmethod
    def serialize(dt):
        return dt

    @staticmethod
    def parse_literal(node):
        if isinstance(node, ast.StringValue):
            return CallbackContainer.from_value(node.value)

    @staticmethod
    def parse_value(value):
        return CallbackContainer.from_value(value)


class QString(Scalar):
    """A Representation of a Django File"""

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
