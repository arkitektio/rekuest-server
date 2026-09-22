from kante.types import Info
from facade import types, models, inputs
import logging
import strawberry
from authentikate.vars import get_user

logger = logging.getLogger(__name__)


def create_toolbox(info: Info, input: inputs.CreateToolboxInput) -> types.Toolbox:
    user = info.context.request.user
    toolbox, _ = models.Toolbox.objects.update_or_create(
        name=input.name,
        defaults=dict(
            description=input.description,
            creator=user,
        ),
    )

    logger.debug("Toolbox created: %s", toolbox)

    return toolbox


def delete_toolbox(info: Info, input: inputs.DeleteToolboxInput) -> strawberry.ID:
    toolbox = models.Toolbox.objects.get(id=input.id)
    toolbox.delete()
    return input.id
