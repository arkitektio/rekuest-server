from .inputs import DefinitionInput
from typing import List
from .models import Protocol
from .protofuncs.predicate import is_predicate

functions = [
    is_predicate,
]


def infer_protocols(definition: DefinitionInput) -> List[Protocol]:
    """Infer the protocols of a definition"""

    protocols = []
    for func in functions:
        protocol = func(definition)
        if protocol:
            protocols.append(protocol)

    return protocols
