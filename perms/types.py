from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group as GroupModel
from balder.types.object import BalderObject
import graphene
from django.contrib.auth.models import Permission
from lok.models import LokClient as LokClientModel, LokApp as LokAppModel

class Permission(BalderObject):
    """ A Permission object

    This object represents a permission in the system. Permissions are
    used to control access to different parts of the system. Permissions
    are assigned to groups and users. A user has access to a part of the
    system if the user is a member of a group that has the permission
    assigned to it.
    """
    unique = graphene.String(description="Unique ID for this permission", required=True)

    def resolve_unique(root, info):
        return f"{root.content_type.app_label}.{root.codename}"

    class Meta:
        model = Permission


class User(BalderObject):
    color = graphene.String(description="The associated color for this user")
    name = graphene.String(description="The name of the user")
    sub = graphene.String(description="The sub of the user")

    def resolve_color(root, info):
        if hasattr(root, "meta"):
            return root.meta.color
        return "#FF0000"

    def resolve_name(root, info):
        return root.first_name + " " + root.last_name

    def resolve_sub(root, info):
        return root.sub 

        
    class Meta:
        model = get_user_model()
        description = get_user_model().__doc__


class Group(BalderObject):
    class Meta:
        model = GroupModel

