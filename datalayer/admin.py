from django.contrib import admin

# Register your models here.
from datalayer import models


admin.site.register(models.DatalayerStore)
admin.site.register(models.MediaStore)
