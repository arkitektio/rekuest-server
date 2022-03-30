from django.db import models


class ArgsField(models.JSONField):
    pass


class KwargsField(models.JSONField):
    pass


class ReturnField(models.JSONField):
    pass


class ParamsField(models.JSONField):
    """Params Field are Describing Templates"""
