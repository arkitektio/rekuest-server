from herre.enums import HerreGrantType
from django.db import models

# Create your models here.
import django.db.models.options as options
options.DEFAULT_NAMES = options.DEFAULT_NAMES + ('identifiers',)


from django.contrib.auth.models import AbstractUser


class HerreUser(AbstractUser):
    roles = models.JSONField(null=True, blank=True)


class HerreApp(models.Model):
    client_id = models.CharField(unique=True, max_length=2000)
    name = models.CharField(max_length=2000)
    grant_type = models.CharField(choices=HerreGrantType.choices, max_length=2000)