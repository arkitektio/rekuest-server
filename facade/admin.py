from django.contrib import admin
from .models import *
# Register your models here.

admin.site.register(Node)
admin.site.register(Pod)
admin.site.register(Provider)
admin.site.register(Template)
admin.site.register(Assignation)
admin.site.register(Provision)