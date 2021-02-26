
from django.db import models

class InPortsField(models.JSONField):
    pass


class OutPortsField(models.JSONField):
    pass


class ParamsField(models.JSONField):
    """Params Field are Describing Templates"""


class PodChannel(models.CharField):
    """The Pods channels and where it will listen to"""


class InputsField(models.JSONField):
    """ The inputs for a Node"""


class OutputsField(models.JSONField):
    """ The outputs for a Node"""