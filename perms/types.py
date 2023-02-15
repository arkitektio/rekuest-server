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


