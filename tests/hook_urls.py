"""rekuest's URLs plus a ``rekuest_service.Service``'s endpoint, for the service-agent round trip.

The live server then plays both parts: rekuest (intake at ``agi/http/<id>``) and a service
(``_rekuest/hook``) whose actions and signals the tests declare on :data:`housekeeping`.
"""

from rekuest.urls import urlpatterns as rekuest_urlpatterns
from rekuest_service import Service

from django.conf import settings

#: The stand-in service, with its own key (as its own process would have). Tests declare on it
#: and clear it again (``_actions`` / ``_signals``).
housekeeping = Service("housekeeping", description="A test service.", key=settings.TEST_SERVICE_KEYS["housekeeping"])

urlpatterns = [*rekuest_urlpatterns, *housekeeping.urls]
