from django.contrib import admin
from .models import *

# Register your models here.


admin.site.register(Repository)
admin.site.register(Registry)
admin.site.register(Agent)
admin.site.register(AppRepository)
admin.site.register(MirrorRepository)


class ReservationAdmin(admin.ModelAdmin):
    # readonly_fields = ("params", "context")
    pass


admin.site.register(Node)
admin.site.register(Template)
admin.site.register(Assignation)
admin.site.register(Reservation, ReservationAdmin)
admin.site.register(Provision)
admin.site.register(Structure)
