from rekuest_core.inputs.types import widget_input_types
from rekuest_core.objects import types


interface_types = [
    types.SliderAssignWidget,
    types.ChoiceAssignWidget,
    types.SearchAssignWidget,
    types.StateChoiceAssignWidget,
    types.CustomReturnWidget,
    types.ChoiceReturnWidget,
    types.StringAssignWidget,
    types.CustomAssignWidget,
    types.BlokAssignWidget,
    types.CustomEffect,
    types.MessageEffect,
    types.ProxyWidget,
    types.HideEffect,
]

# Widget input union members: referenced by no field, so schemas must list them explicitly.
input_union_types = list(widget_input_types)
