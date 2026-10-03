"""rekuest's URLs plus two stand-ins that have nothing to do with each other: a service and a hook agent.

The live server then plays three parts: rekuest (its intakes), a service
(``_rekuest/service``, :data:`housekeeping`: what exists) and a hook agent (``_rekuest/hook``,
:data:`janitor`: what can be done). They are separate instances with separate keys and separate
names, as two processes would be; tests declare on them and clear them again.
"""

from django.conf import settings

from rekuest.urls import urlpatterns as rekuest_urlpatterns
from rekuest_hook import HookAgent
from rekuest_service import Service

housekeeping = Service("housekeeping", description="A test service.", key=settings.TEST_SERVICE_KEYS["housekeeping"])
janitor = HookAgent("janitor", description="A test hook agent.", key=settings.TEST_SERVICE_KEYS["janitor"])

urlpatterns = [*rekuest_urlpatterns, *housekeeping.urls, *janitor.urls]
