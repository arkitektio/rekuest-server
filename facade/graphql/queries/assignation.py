from balder.types import BalderQuery
from facade import types, filters
from facade.models import Assignation
import graphene
from lok import bounced
from facade import models
from facade.inputs import AssignationStatusInput


class AssignationDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query assignation", required=True)

    def resolve(root, info, id=None, parent=None, reference=None):
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

    @bounced(anonymous=True)
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
        operation = "myrequests"


class Assignations(BalderQuery):
    class Meta:
        type = types.Assignation
        list = True
        paginate = True
        filter = filters.AssignationFilter
        operation = "assignations"


class RequestsQuery(BalderQuery):
    class Arguments:
        instance_id = graphene.String(required=True)
        exclude = graphene.List(
            AssignationStatusInput, description="The excluded values", required=False
        )
        filter = graphene.List(
            AssignationStatusInput, description="The included values", required=False
        )

    @bounced(only_jwt=True)
    def resolve(root, info, exclude=None, filter=None, instance_id=None):
        creator = info.context.bounced.user
        app = info.context.bounced.app

        registry, _ = models.Registry.objects.get_or_create(user=creator, app=app)
        waiter, _ = models.Waiter.objects.get_or_create(
            registry=registry, identifier=instance_id
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
        operation = "requests"


class MyTodos(BalderQuery):
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
        operation = "mytodos"


class TodosQuery(BalderQuery):
    class Arguments:
        exclude = graphene.List(
            AssignationStatusInput, description="The excluded values", required=False
        )
        filter = graphene.List(
            AssignationStatusInput, description="The included values", required=False
        )
        identifier = graphene.String(required=False, default_value="default")

    @bounced(only_jwt=True)
    def resolve(root, info, exclude=None, filter=None, identifier="default"):
        creator = info.context.bounced.user
        app = info.context.bounced.app

        registry, _ = models.Registry.objects.get_or_create(user=creator, app=app)
        waiter, _ = models.Agent.objects.get_or_create(
            registry=registry, identifier=identifier
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
        operation = "todos"
