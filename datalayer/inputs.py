from datalayer import base_models
from strawberry.experimental import pydantic


@pydantic.input(model=base_models.RequestMediaUploadInput, all_fields=True)
class RequestMediaUploadInput:
    """
    Docstring for RequestMediaUploadInput
    """

    pass


@pydantic.input(model=base_models.FinishMediaUploadInput, all_fields=True)
class FinishMediaUploadInput:
    """
    Docstring for FinishMediaUploadInput
    """

    pass


@pydantic.input(model=base_models.RequestMediaAccessInput, all_fields=True)
class RequestMediaAccessInput:
    """
    Docstring for RequestMediaAccessInput
    """

    pass


@pydantic.input(model=base_models.RequestGeneralMediaAccessInput, all_fields=True)
class RequestGeneralMediaAccessInput:
    """
    Docstring for RequestGeneralMediaAccessInput
    """

    pass


