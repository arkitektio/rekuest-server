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
from kante.path import dynamicpath
from django.http import HttpRequest, JsonResponse
from health_check.views import HealthCheckView
from django.views.decorators.csrf import csrf_exempt
from facade.upkeep import upkeep_view

def jwks_view(request: HttpRequest) -> JsonResponse:
    """Publish the provenance verifying key(s) for offline verification.

    Verifiers fetch-and-cache this document and verify provenance tokens without
    calling back into Rekuest, so it is served with a public cache header.
    """
    from facade.provenance import keys

    response = JsonResponse(keys.get_jwks_document())
    response["Cache-Control"] = "public, max-age=3600"
    return response



urlpatterns = [
    dynamicpath("admin/", admin.site.urls),
    dynamicpath(".well-known/jwks.json", csrf_exempt(jwks_view), name="provenance_jwks"),
    # takt's calls only (signed with the instance key); the edge does not route ``_rekuest``.
    dynamicpath("_rekuest/upkeep/<str:job>", upkeep_view, name="upkeep"),
    dynamicpath(
        "ht",
        csrf_exempt(
            HealthCheckView.as_view(
                checks=[
                    "health_check.Database",
                    "rekuest.health.Takt",
                ]
            )
        ),
        name="health_check",
    ),
]
