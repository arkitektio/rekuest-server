from django.contrib import admin
from .models import LokApp, LokUser
from guardian.admin import GuardedModelAdmin
# Register your models here.

class UserAdmin(GuardedModelAdmin):
    pass

admin.site.register(LokUser, UserAdmin)
admin.site.register(LokApp)