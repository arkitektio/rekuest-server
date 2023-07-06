import pytest
from django.contrib.auth import get_user_model


@pytest.mark.django_db
def test_superuser():
    me = get_user_model().objects.get(username="AnonymousUser")
    assert me, "AnonymousUser should exist"
