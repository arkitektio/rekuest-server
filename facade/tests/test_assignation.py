import pytest


@pytest.mark.django_db
def test_superuser():
    me = User.objects.get(username="admin")
    assert me.is_superuser
