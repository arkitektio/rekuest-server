from django.contrib import admin
from django.http import HttpRequest

from facade import models


class ReadOnlyAdmin(admin.ModelAdmin):
    """Rows agentd writes: the admin shows them and changes none.

    agentd is their one writer (it holds the agents' leases, orders task transitions and
    publishes the feeds); a save or delete from here would bypass all of that.
    """

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False


for model in (models.Action, models.Implementation, models.TaskEvent, models.Agent, models.Task):
    admin.site.register(model, ReadOnlyAdmin)
admin.site.register(models.Caller)
