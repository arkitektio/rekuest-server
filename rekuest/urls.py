"""
URL configuration for kreature project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from kante.path import dynamicpath, re_dynamicpath
from django.http import HttpRequest, JsonResponse
from django.urls import include, path
from health_check.views import HealthCheckView
from django.views.decorators.csrf import csrf_exempt

t = "d"


def jwks_view(request: HttpRequest) -> JsonResponse:
    """Publish the provenance verifying key(s) for offline verification.

    Verifiers fetch-and-cache this document and verify provenance tokens without
    calling back into Rekuest, so it is served with a public cache header.
    """
    from facade.provenance import keys

    response = JsonResponse(keys.get_jwks_document())
    response["Cache-Control"] = "public, max-age=3600"
    return response


from facade.http_intake import hook_intake  # noqa: E402  (apps are ready when the URLconf loads)

urlpatterns = [
    dynamicpath("admin/", admin.site.urls),
    dynamicpath(".well-known/jwks.json", csrf_exempt(jwks_view), name="provenance_jwks"),
    re_dynamicpath(r"agi/http/(?P<agent_id>[^/]+)$", csrf_exempt(hook_intake), name="hook_intake"),
    dynamicpath(
        "ht",
        csrf_exempt(
            HealthCheckView.as_view(
                checks=[
                    "health_check.Database",
                    # A backend whose clock has drifted must be taken out of rotation: it would
                    # revoke healthy agents' leases in a loop. See ``facade.clock``.
                    "facade.clock.ClockSkewHealthCheck",
                ]
            )
        ),
        name="health_check",
    ),
]
