from django.contrib import admin
from facade import models

# Register your models here.


admin.site.register(models.Action)
admin.site.register(models.Implementation)
admin.site.register(models.Caller)
admin.site.register(models.TaskEvent)
admin.site.register(models.Agent)
admin.site.register(models.Task)
