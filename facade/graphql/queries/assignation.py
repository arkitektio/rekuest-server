from balder.types import BalderQuery
from facade import types
from facade.models import Assignation
import graphene
from lok import bounced
from facade import models
from facade.inputs import AssignationStatusInput


class AssignationDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query assignation", required=True)

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        return Assignation.objects.get(id=id)

    class Meta:
        type = types.Assignation
        operation = "assignation"


class MyAssignations(BalderQuery):
    class Arguments:
        exclude = graphene.List(
            AssignationStatusInput, description="The excluded values", required=False
        )
        filter = graphene.List(
            AssignationStatusInput, description="The included values", required=False
        )
        limit = graphene.Int(description="The excluded values", required=False)

    @bounced(anonymous=False)
    def resolve(root, info, exclude=None, filter=None, limit=None):
        qs = Assignation.objects.filter(creator=info.context.user)
        if filter:
            qs = qs.filter(status__in=filter)
        if exclude:
            qs = qs.exclude(status__in=exclude)
        if limit:
            qs = qs[:limit]

        return qs

    class Meta:
        type = types.Assignation
        list = True
        paginate = True


class TodosQuery(BalderQuery):
    class Arguments:
        exclude = graphene.List(
            AssignationStatusInput, description="The excluded values", required=False
        )
        filter = graphene.List(
            AssignationStatusInput, description="The included values", required=False
        )
        app_group = graphene.ID(required=False, default_value="main")

    @bounced(only_jwt=True)
    def resolve(root, info, exclude=None, filter=None, app_group="main"):

        creator = info.context.bounced.user
        app = info.context.bounced.app

        registry, _ = models.Registry.objects.get_or_create(user=creator, app=app)
        waiter, _ = models.Waiter.objects.get_or_create(
            registry=registry, identifier=app_group
        )

        qs = Assignation.objects.filter(waiter=waiter)
        if filter:
            qs = qs.filter(status__in=filter)
        if exclude:
            qs = qs.exclude(status__in=exclude)

        return qs.all()

    class Meta:
        type = types.Assignation
        list = True
        operation = "todolist"
