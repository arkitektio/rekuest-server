from atexit import register
from channels.testing import WebsocketCommunicator
import pytest
from arkitekt.asgi import application
from hare.consumers.postman.protocols.postman_json import *
import json
from facade import models
from lok.models import LokApp, LokUser
from django.contrib.auth import get_user_model
from hare.carrots import (
    HareMessage,
    ProvideHareMessage,
    ReserveHareMessage,
    UnreserveHareMessage,
    UnprovideHareMessage,
)
from guardian.shortcuts import assign_perm


def create_providable_node(
    user: LokUser,
    number_template=1,
    number_agents_per_template=1,
    extra_providable_users=[],
):
    """Create a providable node give a user and a postman app"""
    agentapp = LokApp.objects.create(client_id="agent", name="test")

    agent_r = models.Registry.objects.create(app=agentapp, user=user)
    # Node
    x = models.Node.objects.create(
        name="test", interface="test", package="test", args=[], kwargs=[], returns=[]
    )
    for i in range(number_template):
        t = models.Template.objects.create(name="test", node=x, registry=agent_r)
        for user in extra_providable_users:
            assign_perm("providable", user, t)
        for j in range(number_agents_per_template):
            a = models.Agent.objects.create(registry=agent_r, identifier="test")
            for user in extra_providable_users:
                assign_perm("can_provide_on", user, a)

    return x


def create_waiter_for(user: LokUser):
    """Create a waiter for a user"""
    postman_app, cr = LokApp.objects.get_or_create(client_id="postman", name="test")
    postman_r, cr = models.Registry.objects.get_or_create(app=postman_app, user=user)

    w, cr = models.Waiter.objects.get_or_create(registry=postman_r, identifier="test")
    return w


@pytest.mark.django_db
def test_schedule_one():
    user = get_user_model().objects.create(username="test", email="test_email")

    n = create_providable_node(user)
    w = create_waiter_for(user)

    params = ReserveParams(desiredInstances=1)

    res = models.Reservation.objects.create(node=n, waiter=w, params=params.dict())

    res, forwards = res.schedule()
    assert res, "Reservation should be created"
    assert forwards, "Forwards should be created"
    assert len(forwards) == params.desiredInstances, "Forwards should be created"


@pytest.mark.django_db
def test_schedule_idempotent():
    user = get_user_model().objects.create(username="test", email="test_email")

    n = create_providable_node(user)
    w = create_waiter_for(user)

    params = ReserveParams(desiredInstances=1)

    res = models.Reservation.objects.create(node=n, waiter=w, params=params.dict())

    res, forwards = res.schedule()

    assert res, "Reservation should be created"
    assert forwards, "Forwards should be created"
    assert len(forwards) == params.desiredInstances, "Forwards should be created"
    for i in forwards:
        assert isinstance(i, ProvideHareMessage), "Needs to be a ProvideHareMessage"

    params = ReserveParams(desiredInstances=1)

    newres, forwards = models.Reservation.objects.schedule(
        node=n.id, waiter=w, params=params
    )
    assert (
        res.id == newres.id
    ), "Schedule should be idempotent (not considering actions)"
    assert len(forwards) == 0, "Should not have forwards"


@pytest.mark.django_db
def test_schedule_one_and_new():
    user = get_user_model().objects.create(username="test", email="test_email")

    n = create_providable_node(user)
    w = create_waiter_for(user)

    params = ReserveParams(desiredInstances=1)

    res = models.Reservation.objects.create(node=n, waiter=w, params=params.dict())

    res, forwards = res.schedule()

    assert res, "Reservation should be created"
    assert forwards, "Forwards should be created"
    assert len(forwards) == params.desiredInstances, "Forwards should be created"
    for i in forwards:
        assert isinstance(i, ProvideHareMessage), "Needs to be a ProvideHareMessage"

    newparams = ReserveParams(desiredInstances=2)

    newres, forwards = models.Reservation.objects.schedule(
        node=n.id, waiter=w, params=newparams
    )
    assert res.id != newres.id, "Should create a new type of reservation"
    assert len(forwards) == newparams.desiredInstances, "Should not have forwards"
    assert isinstance(
        forwards[0], ReserveHareMessage
    ), "Needs to be a ProvideHareMessage"

    assert isinstance(
        forwards[1], ProvideHareMessage
    ), "Needs to be a ProvideHareMessage"


@pytest.mark.django_db
def test_schedule_and_unreserve():
    user = get_user_model().objects.create(username="test", email="test_email")

    n = create_providable_node(user)
    w = create_waiter_for(user)

    params = ReserveParams(desiredInstances=1)

    res = models.Reservation.objects.create(node=n, waiter=w, params=params.dict())

    res, forwards = res.schedule()
    assert res, "Reservation should be created"
    assert forwards, "Forwards should be created"
    assert len(forwards) == params.desiredInstances, "Forwards should be created"
    for i in forwards:
        assert isinstance(i, ProvideHareMessage), "Needs to be a ProvideHareMessage"

    res, forwards = res.unreserve()
    assert len(forwards) == params.desiredInstances, "Forwards should be created"
    for i in forwards:
        assert isinstance(
            i, UnprovideHareMessage
        ), "Needs to be a Unprovide message as there is no other reservation for this provision"  #


@pytest.mark.django_db
def test_schedule_non_permited():
    user = get_user_model().objects.create(username="test", email="test_email")
    user2 = get_user_model().objects.create(username="test2", email="test_email2")

    n = create_providable_node(user)
    w = create_waiter_for(user2)  # this user is not allowed to schedule for this node

    params = ReserveParams(desiredInstances=1)

    res = models.Reservation.objects.create(node=n, waiter=w, params=params.dict())

    res, forwards = res.schedule()

    assert (
        res.viable == False
    ), "Should not be viables as no link should be created for this user..."
    assert res, "Reservation should be created"
    assert (
        len(forwards) == 0
    ), "No Forwards should be created as reservation should be empty"


@pytest.mark.django_db
def test_schedule_permitted():
    user = get_user_model().objects.create(username="test", email="test_email")
    user2 = get_user_model().objects.create(username="test2", email="test_email2")

    n = create_providable_node(user, extra_providable_users=[user2])
    w_permitted = create_waiter_for(user)
    w_extra = create_waiter_for(
        user2
    )  # this user is not allowed to schedule for this node

    params = ReserveParams(desiredInstances=1)

    res = models.Reservation.objects.create(
        node=n, waiter=w_permitted, params=params.dict()
    )

    res, forwards = res.schedule()

    permitted_res = models.Reservation.objects.create(
        node=n, waiter=w_extra, params=params.dict()
    )

    permitted_res, forwards = permitted_res.schedule()

    assert (
        permitted_res.viable == True
    ), "Should be viable as this user should be able to schedule for this node"
    assert res, "Reservation should be created"
    for i in forwards:
        assert isinstance(
            i, ProvideHareMessage
        ), "Needs to be a ProvideHareMessage as we are not allowing to link to another provision"


@pytest.mark.django_db
def test_schedule_permitted():
    user = get_user_model().objects.create(username="test", email="test_email")
    user2 = get_user_model().objects.create(username="test2", email="test_email2")

    n = create_providable_node(user, extra_providable_users=[user2])
    w_permitted = create_waiter_for(user)
    w_extra = create_waiter_for(
        user2
    )  # this user is not allowed to schedule for this node

    params = ReserveParams(desiredInstances=1)

    res = models.Reservation.objects.create(
        node=n, waiter=w_permitted, params=params.dict()
    )

    res, forwards = res.schedule()

    for prov in res.provisions.all():
        assign_perm("can_link_to", user2, prov)

    permitted_res = models.Reservation.objects.create(
        node=n, waiter=w_extra, params=params.dict()
    )

    permitted_res, forwards = permitted_res.schedule()

    assert (
        permitted_res.viable == True
    ), "Should be viable as this user should be able to schedule for this node"
    assert res, "Reservation should be created"
    assert forwards, "Forwards should be created"
