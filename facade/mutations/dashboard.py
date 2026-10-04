import logging
from typing import cast

from kante.types import Info

from facade import inputs, models, types

logger = logging.getLogger(__name__)


def create_dashboard(info: Info, input: inputs.CreateDashboardInput) -> types.Dashboard:
    dashboard = models.Dashboard.objects.create(
        name=input.name,
        organization=info.context.request.organization,
    )

    return cast("types.Dashboard", dashboard)


def delete_dashboard(info: Info, input: inputs.DeleteDashboardInput) -> bool:
    try:
        dashboard = models.Dashboard.objects.get(id=input.id, organization=info.context.request.organization)
        dashboard.delete()
        return True
    except models.Dashboard.DoesNotExist:
        logger.warning(f"Dashboard with id {input.id} does not exist.")
        return False


def update_dashboard(info: Info, input: inputs.UpdateDashboardInput) -> types.Dashboard:
    try:
        dashboard = models.Dashboard.objects.get(id=input.id, organization=info.context.request.organization)
    except models.Dashboard.DoesNotExist:
        logger.warning(f"Dashboard with id {input.id} does not exist.")
        raise ValueError(f"Dashboard with id {input.id} does not exist.")

    if input.name is not None:
        dashboard.name = input.name
    if input.bloks is not None:
        for placement in dashboard.placements.all():
            placement.delete()

        for blok_id in input.bloks:
            blok = models.MaterializedBlok.objects.get(id=blok_id, blok__organization=info.context.request.organization)
            placement = models.DashboardPlacement.objects.create(dashboard=dashboard, blok=blok)

    dashboard.save()

    return cast("types.Dashboard", dashboard)
