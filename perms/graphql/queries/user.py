from balder.types.query.base import BalderQuery
from graphene import types
import graphene
from perms.filters import UserFilter
from perms import types, models
from lok import bounced
from django.contrib.auth import get_user_model

UserModel = get_user_model()


class Me(BalderQuery):
    @bounced()
    def resolve(root, info):
        return info.context.user

    class Meta:
        type = types.User
        operation = "me"


class User(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The user's id", required=False)
        email = graphene.String(description="The user's id", required=False)

    @bounced()
    def resolve(root, info, email=None, id=None):
        if email:
            return UserModel.objects.get(email=email)
        if id:
            return UserModel.objects.get(id=id)

        raise Exception("Provide either email or id")

    class Meta:
        type = types.User
        operation = "user"


class Users(BalderQuery):
    """Get a list of users"""

    class Meta:
        list = True
        type = types.User
        filter = UserFilter
        operation = "users"
