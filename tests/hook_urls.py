"""rekuest's URLs plus a ``rekuest_service.Service``'s endpoint, for the service-agent round trip.

The live server then plays both parts: rekuest (intake at ``agi/http/<id>``) and a service
(``_rekuest/hook``) whose structures and signals the tests declare on :data:`housekeeping`, and whose actions on its agent, :data:`housekeeper`.
"""

from rekuest.urls import urlpatterns as rekuest_urlpatterns
from rekuest_service import HookAgent, Service

from django.conf import settings

#: The stand-in service, with its own key (as its own process would have). Tests declare on it
#: and clear it again (``_actions`` / ``_signals`` / ``_structures``).
housekeeping = Service("housekeeping", description="A test service.", key=settings.TEST_SERVICE_KEYS["housekeeping"])
#: Its HookAgent: the actions the tests declare are the agent's, not the service's.
housekeeper = HookAgent(housekeeping)

urlpatterns = [*rekuest_urlpatterns, *housekeeping.urls]
