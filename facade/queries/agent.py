from typing import cast

import strawberry
from kante.types import Info

from facade import models, types


def agent(
    info: Info,
    id: strawberry.ID | None = None,
    app: str | None = None,
    version: str | None = None,
    device_id: str | None = None,
) -> types.Agent:
    if id:
        return cast("types.Agent", models.Agent.objects.get(id=id))

    if app:
        agents = models.Agent.objects.filter(
            organization=info.context.request.organization,
            app__identifier=app,
        )

        if version:
            agents = agents.filter(version=version)

        if device_id:
            agents = agents.filter(client__device__device_id=device_id)

        if agents.count() == 1:
            return cast("types.Agent", agents.get())
        elif agents.count() > 1:
            raise ValueError("Multiple agents found with the provided app, version and device_id. Please provide the agent id to identify the agent.")
        else:
            raise ValueError("No agent found for {app} with version {version} and device_id {device_id}".format(app=app, version=version, device_id=device_id))

    else:
        raise ValueError("You need to provide either the agent id or the app, version and device_id to identify the agent")
