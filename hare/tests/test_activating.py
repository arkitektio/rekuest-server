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
def test_active_message():
    user = get_user_model().objects.create(username="test", email="test_email")

    n = create_providable_node(user)
    w = create_waiter_for(user)

    params = ReserveParams(desiredInstances=1)

    res, forwards = models.Reservation.objects.schedule(
        node=n.id, waiter=w, params=params
    )
    assert res, "Reservation should be created"
    assert forwards, "Forwards should be created"
    assert len(forwards) == params.desiredInstances, "Forwards should be created"

    for prov in res.provisions.all():
        prov, forward = prov.activate()
        assert prov, "Provision should be here"
        assert forward, "Forward should be created"


@pytest.mark.django_db
def test_active_unactive_message():
    user = get_user_model().objects.create(username="test", email="test_email")

    n = create_providable_node(user)
    w = create_waiter_for(user)

    params = ReserveParams(desiredInstances=1)

    res, forwards = models.Reservation.objects.schedule(
        node=n.id, waiter=w, params=params
    )
    assert res, "Reservation should be created"
    assert forwards, "Forwards should be created"
    assert len(forwards) == params.desiredInstances, "Forwards should be created"

    for prov in res.provisions.all():
        prov, forward = prov.activate()
        assert prov, "Provision should be here"
        assert forward, "Forward should be created"

    new_res = models.Reservation.objects.get(id=res.id)
    assert new_res.status == ReservationStatus.ACTIVE, "Reservation should be active"

    for prov in res.provisions.all():
        prov, forward = prov.critical()
        assert prov, "Provision should be here"
        assert forward, "Forward should be created"


@pytest.mark.django_db
def test_active_dropped_message():
    user = get_user_model().objects.create(username="test", email="test_email")

    n = create_providable_node(user)
    w = create_waiter_for(user)

    params = ReserveParams(desiredInstances=1)

    res, forwards = models.Reservation.objects.schedule(
        node=n.id, waiter=w, params=params
    )
    assert res, "Reservation should be created"
    assert forwards, "Forwards should be created"
    assert len(forwards) == params.desiredInstances, "Forwards should be created"

    for prov in res.provisions.all():
        prov, forward = prov.drop()
        assert prov, "Provision should be here"
        assert forward, "Forward should be created"
