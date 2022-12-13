from django.contrib.auth import get_user_model
from django.contrib.auth.models import User

class ImitationError(PermissionError):
    pass


def get_imitiate(user: User, imitater: str):
    
    return get_user_model().objects.get(id=imitater)