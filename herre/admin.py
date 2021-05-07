from django.contrib import admin
from herre.models import HerreApp, HerreUser
# Register your models here.

admin.site.register(HerreUser)
admin.site.register(HerreApp)