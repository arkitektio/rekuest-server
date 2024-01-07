from lok import bounced
from balder.types import BalderSubscription
from facade import types
from facade.inputs import TemplateParamInput
import graphene


class TemplateEvent(graphene.ObjectType):
    created = graphene.Field(types.Template)
    deleted = graphene.ID()
    updated = graphene.Field(types.Template)


class TemplatesEvent(BalderSubscription):
    PARAMS_GROUP = lambda key, value: f"templates_params_{key}-{value}"

    class Arguments:
        template_params = graphene.List(
            TemplateParamInput, description="The params to filter by"
        )

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, template_params=None, level=None):
        groups = []
        if template_params:
            for i in template_params:
                groups.append(TemplatesEvent.PARAMS_GROUP(i.key, i.value))

        if not groups:
            raise Exception("You need to specify at least one param")

        return groups

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]
        if action == "created":
            return {"created": data}
        if action == "updated":
            return {"updated": data}
        if action == "deleted":
            return {"deleted": data}

    class Meta:
        type = TemplateEvent
        operation = "templates"
