from balder.types.query import BalderQuery
from facade.graphql.utils import AvailableModelsEnum, guarded_models
import graphene
from django.contrib.auth.models import Group, Permission
from facade import types
from balder.registry import register_type
import graphene
from guardian.shortcuts import get_users_with_perms, get_groups_with_perms


class UserAssignment(graphene.ObjectType):
    permissions = graphene.List(graphene.String, required=True)
    user = graphene.Field(
        types.User, description="A query that returns an image path", required=True
    )


class GroupAssignment(graphene.ObjectType):
    permissions = graphene.List(graphene.String, required=True)
    group = graphene.Field(
        types.Group, description="A query that returns an image path", required=True
    )


class PermissionsFor(BalderQuery):
    class Arguments:
        model = graphene.Argument(AvailableModelsEnum, required=True)
        name = graphene.String(required=False)

    def resolve(self, info, model, name=None):

        ct = guarded_models[model]

        f = Permission.objects.filter(content_type=ct)
        if name:
            f = f.filter(name__icontains=name)

        return f.order_by("name")

    class Meta:
        type = types.Permission
        list = True
        operation = "permissionsFor"


class PermissionsOfReturn(graphene.ObjectType):
    available = graphene.List(types.Permission)
    userAssignments = graphene.List(UserAssignment)
    groupAssignments = graphene.List(GroupAssignment)


class PermissionsOf(BalderQuery):
    class Arguments:
        model = graphene.Argument(AvailableModelsEnum, required=True)
        id = graphene.ID(required=True)

    def resolve(self, info, model, id):

        ct = ct_types[model]
        f = Permission.objects.filter(content_type=ct)

        ct = ct_types[model]
        model = ct.model_class()

        instance = model.objects.get(id=id)

        users = get_users_with_perms(
            instance, attach_perms=True, with_group_users=False, with_superusers=False
        )
        groups = get_groups_with_perms(instance, attach_perms=True)

        userassignments = [
            {"type": "user", "user": key, "permissions": value}
            for key, value in users.items()
        ]
        groupassignments = [
            {"type": "group", "group": key, "permissions": value}
            for key, value in groups.items()
        ]

        return {
            "available": f.order_by("name"),
            "userAssignments": userassignments,
            "groupAssignments": groupassignments,
        }

    class Meta:
        type = PermissionsOfReturn
        list = False
        operation = "permissionsOf"
