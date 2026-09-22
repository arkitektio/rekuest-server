from kante.types import Info
import strawberry
from facade import types, models, inputs
import logging

logger = logging.getLogger(__name__)


def create_shortcut(info: Info, input: inputs.CreateShortcutInput) -> types.Shortcut:
    toolbox = (
        models.Toolbox.objects.get(id=input.toolbox)
        if input.toolbox
        else models.Toolbox.objects.get_or_create(
            name="default",
            defaults=dict(
                description="Default toolbox",
                creator=info.context.request.user,
                client=info.context.request.client,
                organization=info.context.request.organization,
            ),
        )[0]
    )

    action = models.Action.objects.get(id=input.action)

    args = [arg for arg in action.args if arg["key"] not in input.args]
    returns = [arg for arg in action.returns if arg["key"] not in []]

    shortcut = models.Shortcut.objects.create(
        name=input.name,
        description=input.description,
        action_id=input.action,
        creator=info.context.request.user,
        saved_args=input.args,
        toolbox=toolbox,
        args=args,
        returns=returns,
        allow_quick=input.allow_quick,
        use_returns=input.use_returns,
        bind_number=input.bind_number,
    )

    logger.debug("Shortcut created: %s", shortcut)

    return shortcut


def delete_shortcut(info: Info, input: inputs.DeleteShortcutInput) -> strawberry.ID:
    shortcut = models.Shortcut.objects.get(id=input.id)
    shortcut.delete()
    return input.id
