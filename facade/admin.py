from django.contrib import admin
from django.contrib.auth import get_user_model
from .models import *

# Register your models here.


admin.site.register(Repository)
admin.site.register(Registry)
admin.site.register(Agent)
admin.site.register(AppRepository)
admin.site.register(MirrorRepository)

from guardian.admin import GuardedModelAdmin


class ReservationAdmin(GuardedModelAdmin):
    readonly_fields = ("params", "context")


class ProvisionsAdmin(GuardedModelAdmin):
    pass


class AssignationAdmin(admin.ModelAdmin):
    readonly_fields = ("context",)


class TemplateAdmin(GuardedModelAdmin):
    pass


admin.site.register(Node)
admin.site.register(Template, TemplateAdmin)
admin.site.register(Assignation, AssignationAdmin)
admin.site.register(Reservation, ReservationAdmin)
admin.site.register(AssignationLog)
admin.site.register(ProvisionLog)
admin.site.register(Provision, ProvisionsAdmin)
admin.site.register(Structure)
admin.site.register(Waiter)
