"""Dump one example of every agent-protocol message: the contract other implementations match.

Run: ``DJANGO_SETTINGS_MODULE=rekuest.settings_test python scripts/dump_wire_examples.py``.
Writes ``tests/fixtures/agent_wire_examples.json``: for each direction and each ``type``, a frame
with every field set (optional ones included), exactly as the server serializes it
(``model_dump(mode="json")``), and validated back through the same discriminated union the server
parses with. A second implementation of the protocol (rekuest-agentd, arkirust) must parse each
frame and serialize it back to the same JSON.
"""

from __future__ import annotations

import enum
import json
import os
import sys
import types
import typing
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rekuest.settings_test")
django.setup()

from pydantic import BaseModel, TypeAdapter  # noqa: E402

from facade import messages  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "agent_wire_examples.json"


def example(annotation: typing.Any, name: str, depth: int = 0) -> typing.Any:  # noqa: ANN401, PLR0911 -- one arm per kind
    """A value of ``annotation``, recognisable by ``name`` where it is a string."""
    if isinstance(annotation, typing.ForwardRef):
        annotation = resolve(annotation.__forward_arg__)
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Literal:
        value = args[0]
        return value.value if isinstance(value, enum.Enum) else value
    if origin in (typing.Union, types.UnionType):
        options = [a for a in args if a is not type(None)]
        return example(options[0], name, depth)
    if origin is typing.Annotated:
        return example(args[0], name, depth)
    if origin in (list, tuple, typing.Sequence):
        return [example(args[0], name, depth)] if args and depth < MAX_DEPTH else []
    if origin is dict:
        return {"key": example(args[1], name, depth) if len(args) == 2 else 1}
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return fill(annotation, depth + 1)
        if issubclass(annotation, enum.Enum):
            return next(iter(annotation)).value
        if issubclass(annotation, bool):
            return True
        if issubclass(annotation, int):
            return 7
        if issubclass(annotation, float):
            return 1.5
        if issubclass(annotation, str):
            return f"{name}-1"
    if annotation is dict:
        return {"key": 1}
    if annotation is list:
        return [1]
    if annotation is typing.Any or annotation is object:
        return {"any": [1, "two"]}
    raise TypeError(f"no example for {name}: {annotation!r}")


def resolve(name: str) -> type:
    """A forward-referenced class, looked up in the modules the messages import from."""
    for module in list(sys.modules.values()):
        found = getattr(module, name, None) if module and getattr(module, "__name__", "").startswith(("facade", "rekuest_core")) else None
        if isinstance(found, type):
            return found
    raise TypeError(f"cannot resolve forward reference {name!r}")


# Input models nest recursively (a util call's args hold util calls): below this depth only the
# required fields are filled, and lists stay empty.
MAX_DEPTH = 2


def fill(model: type[BaseModel], depth: int = 0) -> dict[str, typing.Any]:
    """Every field of ``model`` set, optional ones included (above ``MAX_DEPTH``)."""
    values: dict[str, typing.Any] = {}
    for name, field in model.model_fields.items():
        if name == "type":
            values[name] = field.default.value if isinstance(field.default, enum.Enum) else field.default
            continue
        if depth >= MAX_DEPTH and not field.is_required():
            continue
        values[field.alias or name] = example(field.annotation, name, depth)
    return values


# Frames whose example needs a particular value to mean anything.
OVERRIDES: dict[str, dict[str, typing.Any]] = {
    # A declaration with nothing in it: its models are the registration's, not the wire's.
    "REGISTER": {"implementations": [], "states": [], "locks": [], "bloks": []},
    "EFFECT": {"effect": "NOW", "value": 1790715513.22, "key": "NOW:1"},
    "STATE_PATCH": {"op": "replace", "path": "/barcode", "value": "B1", "old_value": None},
}


def dump(union: typing.Any) -> dict[str, typing.Any]:  # noqa: ANN401
    adapter = TypeAdapter(typing.Annotated[union, messages.Field(discriminator="type")])
    frames: dict[str, typing.Any] = {}
    for model in typing.get_args(union):
        raw = {**fill(model), **OVERRIDES.get(model.model_fields["type"].default.value, {})}
        parsed = adapter.validate_python(raw)
        frame = parsed.model_dump(mode="json")
        assert adapter.validate_python(frame).model_dump(mode="json") == frame, model.__name__
        frames[frame["type"]] = frame
    return dict(sorted(frames.items()))


def main() -> None:
    doc = {
        "_comment": "Generated by scripts/dump_wire_examples.py from facade/messages.py; do not edit. "
        "Every frame of the agent protocol with every field set, as the server serializes it.",
        "from_agent": dump(messages.FromAgentMessage),
        "to_agent": dump(messages.ToAgentMessage),
    }
    OUT.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n")
    print(f"wrote {OUT} ({len(doc['from_agent'])} from_agent, {len(doc['to_agent'])} to_agent)")


if __name__ == "__main__":
    main()
