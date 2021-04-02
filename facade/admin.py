from django.contrib import admin
from .models import *
# Register your models here.

admin.site.register(Node)
admin.site.register(Pod)
admin.site.register(BaseProvider)
admin.site.register(BaseRepository)
admin.site.register(AppRepository)
admin.site.register(AppProvider)
admin.site.register(Template)
admin.site.register(Assignation)
admin.site.register(Reservation)
admin.site.register(Provision)
admin.site.register(DataModel)
admin.site.register(DataPoint)
admin.site.register(Service)
admin.site.register(ServiceProvider)