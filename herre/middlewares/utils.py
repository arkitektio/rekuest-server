from herre.models import HerreUser, HerreApp
from django.core.exceptions import  PermissionDenied
from django.contrib.auth import get_user_model
from django.http.response import HttpResponseBadRequest
from asgiref.sync import async_to_sync, sync_to_async
from herre.token import JwtToken

import logging

logger = logging.getLogger(__name__)

import uuid


def update_or_create_herre(decoded):
    if "email" in decoded and decoded["email"] is not None:
        try:
            user = HerreUser.objects.get(email=decoded["email"])
            user.roles = decoded["roles"]
            user.save()
        except HerreUser.DoesNotExist:
            user = HerreUser(email=decoded["email"])
            user.roles = decoded["roles"]
            user.set_unusable_password()
            user.save()
            logger.warning("Created new user")
    else:
        user = None

    if "client_id" in decoded and decoded["client_id"] is not None:
        try:
            app = HerreApp.objects.get(client_id=decoded["client_id"])
        except HerreApp.DoesNotExist:
            app = HerreApp(client_id=decoded["client_id"], name=decoded["client_app"], grant_type=decoded["type"])
            app.save()
            logger.warning("Created new app")
    else:
        app = None

    return user, app


@sync_to_async
def set_request_async(request, decoded, token):
    user, app = update_or_create_herre(decoded)
    request.auth = JwtToken(decoded, user, app, token)
    request.user = user
    return request

def set_request_sync(request, decoded, token):
    user, app = update_or_create_herre(decoded)
    request.auth = JwtToken(decoded, user, app, token)
    request.user = user
    return request


@sync_to_async
def set_scope_async(scope, decoded, token):
    user, app = update_or_create_herre(decoded)
    scope["auth"] = JwtToken(decoded, user, app, token)
    scope["user"] = user
    return scope
