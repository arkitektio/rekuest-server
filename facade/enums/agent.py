from enum import Enum

import strawberry
from django.db.models import TextChoices


class AgentEventChoices(TextChoices):
    DISCONNECT = "DISCONNECT", "Disconnect (Agent disconnected)"
    CONNECT = "CONNECT", "Connect (Agent connected)"


@strawberry.enum
class AgentKind(str, Enum):
    WEBSOCKET = "WEBSOCKET"
    WEBHOOK = "WEBHOOK"
