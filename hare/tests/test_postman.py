from channels.testing import WebsocketCommunicator
import pytest
from arkitekt.asgi import application
from hare.consumers.postman.protocols.postman_json import *
import json
from facade import models


@pytest.mark.django_db
async def test_postman_connect(mock_jwt_authentication, reservable_node):
    assert reservable_node, "reservable_node should exist"

    communicator = WebsocketCommunicator(application, "watchi/token=")
    connected, _ = await communicator.connect(timeout=4)
    assert connected, "Could not connect to the websocket"

    x = await communicator.send_to(ReservePub(node=reservable_node.id).json())

    message = await communicator.receive_from()
    assert message, "No message received"
    message = json.loads(message)
    assert message.get("type") == PostmanMessageTypes.RESERVE_REPLY

    # Close
    await communicator.disconnect()
