from facade.inputs import DefinitionInput
from facade.models import Protocol


def is_predicate(definition: DefinitionInput) -> Protocol:
    print(definition)

    if "returns" not in definition:
        return None

    returns = definition["returns"]

    if len(returns) != 1:
        return None

    if returns[0]["kind"] == "BOOL":
        x, _ = Protocol.objects.update_or_create(
            name="predicate", defaults=dict(description="Is this a predicate?")
        )
        return x
