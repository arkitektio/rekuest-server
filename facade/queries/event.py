from typing import cast

import strawberry
from kante.types import Info

from facade import models, types


def event(
    info: Info,
    id: strawberry.ID,
) -> types.TaskEvent:
    return cast("types.TaskEvent", models.TaskEvent.objects.get(id=id))
