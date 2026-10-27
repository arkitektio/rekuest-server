"""The blok language's vocabulary (``vocabulary.json``), typed.

Everything a party needs to agree on -- the reserved roots, the per-kind root matrix, the
record shapes, the functions and the diagnostic codes -- is data in ``vocabulary.json``, which
the server (rekuest_core, takt) and the renderer (orkestrator) vendor or are generated from.
This module only reads it: it imports nothing beyond the standard library, so a frozen copy
can serve a database migration.
"""

import functools
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from importlib import resources
from typing import Literal

JSON = None | bool | int | float | str | list["JSON"] | dict[str, "JSON"]
"""A JSON value, as ``json.loads`` returns it."""

VOCAB: Literal[2] = 2
"""The language version this package speaks: every v2 blok stores ``vocab: 2``."""

Level = Literal["error", "warning"]


def _object(value: JSON, where: str) -> dict[str, JSON]:
    if not isinstance(value, dict):
        raise ValueError(f"vocabulary.json: {where} is not an object")
    return value


def _list(value: JSON, where: str) -> list[JSON]:
    if not isinstance(value, list):
        raise ValueError(f"vocabulary.json: {where} is not a list")
    return value


def _string(value: JSON, where: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"vocabulary.json: {where} is not a string")
    return value


def _strings(value: JSON, where: str) -> tuple[str, ...]:
    return tuple(_string(item, where) for item in _list(value, where))


def _flag(entry: Mapping[str, JSON], key: str) -> bool:
    value = entry.get(key, False)
    return value is True


@dataclass(frozen=True)
class Param:
    """A function's parameter; positional arguments map onto params in order."""

    key: str
    kind: str
    required: bool = True


@dataclass(frozen=True)
class Function:
    """A function of the language (``str.upper``) or an effect (``ui.copy``)."""

    name: str
    params: tuple[Param, ...]
    returns: str
    description: str = ""
    null: str = ""
    lazy: bool = False
    effect: bool = False
    sees_status: bool = False
    task_operation: bool = False

    @property
    def keys(self) -> tuple[str, ...]:
        """The parameter names in positional order."""
        return tuple(param.key for param in self.params)

    @property
    def required_keys(self) -> tuple[str, ...]:
        """The parameters every call passes."""
        return tuple(param.key for param in self.params if param.required)


@dataclass(frozen=True)
class Kind:
    """A blok kind and what it may read: one row of the root matrix."""

    name: str
    roots: frozenset[str]
    locals: bool
    contexts: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class Context:
    """A drawing context: whether it draws text."""

    name: str
    text: bool


@dataclass(frozen=True)
class RecordField:
    """A field of a record (the task record, a query's record, ``viewer``)."""

    key: str
    kind: str
    nullable: bool = False
    choices: tuple[str, ...] = ()
    children: tuple["RecordField", ...] = ()


@dataclass(frozen=True)
class Record:
    """A record shape the renderer fills and a blok reads."""

    name: str
    fields: tuple[RecordField, ...]

    def field(self, key: str) -> RecordField | None:
        """The field called ``key``, or None."""
        return next((field for field in self.fields if field.key == key), None)


@dataclass(frozen=True)
class DiagnosticSpec:
    """One row of the diagnostic table."""

    code: str
    level: Level
    message: str
    runtime: bool = False
    """A record's error code the renderer gives (``deps.unbound``), never a checker's finding."""
    hint: str | None = None
    """How to fix it, a template filled like the message; None when the message says it already."""


@dataclass(frozen=True)
class Theme:
    """The design language a blok is styled in: colour roles, logo pens, custom variants."""

    colour_roles: tuple[str, ...]
    status_roles: tuple[str, ...]
    spectral_roles: tuple[str, ...]
    colour_utilities: tuple[str, ...]
    palette_colours: tuple[str, ...]
    palette_role_hints: Mapping[str, str]
    logo_pens: tuple[str, ...]
    logo_paint_props: tuple[str, ...]
    scene_paint_props: tuple[str, ...]
    custom_variants: tuple[str, ...]
    data_attributes: tuple[str, ...]


@dataclass(frozen=True)
class Limits:
    """How small a logo stays."""

    logo_max_nodes: int
    logo_max_depth: int
    logo_max_path_data: int
    logo_max_foreach_items: int
    logo_path_data_props: tuple[str, ...]


@dataclass(frozen=True)
class Vocabulary:
    """``vocabulary.json``, parsed."""

    vocab: int
    data_roots: tuple[str, ...]
    scoped_roots: tuple[str, ...]
    function_namespaces: tuple[str, ...]
    future_roots: tuple[str, ...]
    reserved: frozenset[str]
    tasks_segment: str
    assets_segment: str
    reserved_state_names: frozenset[str]
    agent_fields: tuple[str, ...]
    kinds: Mapping[str, Kind]
    contexts: Mapping[str, Context]
    blok_roots: Mapping[str, str]
    """A blok's root component -> the blok context it names (``Html`` -> ``HTML``)."""
    records: Mapping[str, Record]
    idle_task: Mapping[str, JSON]
    functions: Mapping[str, Function]
    operators: Mapping[str, str]
    explicit_id: re.Pattern[str]
    theme: Theme
    limits: Limits
    base_version: int
    catalogs: Mapping[str, int]
    """The standard catalogs by name, at the version this vocabulary speaks (``ui``: 1)."""
    bind_kinds: Mapping[str, frozenset[str]]
    """Per catalog kind of a ``bind`` prop, the port kinds of the locals it writes."""
    arg_kinds: Mapping[str, frozenset[str]]
    """Per port kind of a local, the kinds of an action's arg port that take it whole."""
    diagnostics: Mapping[str, DiagnosticSpec]

    def function(self, name: str) -> Function | None:
        """The function called ``name``, or None."""
        return self.functions.get(name)

    def kind(self, name: str) -> Kind:
        """The blok kind called ``name``.

        Raises:
            ValueError: If there is no such kind.
        """
        kind = self.kinds.get(name)
        if kind is None:
            raise ValueError(f"no blok kind {name!r} (kinds: {', '.join(sorted(self.kinds))})")
        return kind

    def is_explicit_id(self, node_id: str) -> bool:
        """Whether ``node_id`` is an author's id rather than a derived one."""
        return self.explicit_id.fullmatch(node_id) is not None


def _record_fields(raw: JSON, where: str) -> tuple[RecordField, ...]:
    fields: list[RecordField] = []
    for item in _list(raw, where):
        entry = _object(item, where)
        choices = entry.get("choices")
        children = entry.get("children")
        fields.append(
            RecordField(
                key=_string(entry.get("key"), f"{where}.key"),
                kind=_string(entry.get("kind"), f"{where}.kind"),
                nullable=_flag(entry, "nullable"),
                choices=_strings(choices, f"{where}.choices") if choices is not None else (),
                children=_record_fields(children, f"{where}.children") if children is not None else (),
            )
        )
    return tuple(fields)


def _function(name: str, raw: JSON) -> Function:
    entry = _object(raw, f"functions.{name}")
    params: list[Param] = []
    for item in _list(entry.get("params"), f"functions.{name}.params"):
        spec = _list(item, f"functions.{name}.params")
        required = spec[2] if len(spec) > 2 else True
        params.append(
            Param(
                key=_string(spec[0], f"functions.{name}.params"),
                kind=_string(spec[1], f"functions.{name}.params"),
                required=required is True,
            )
        )
    description = entry.get("description", "")
    null = entry.get("null", "")
    return Function(
        name=name,
        params=tuple(params),
        returns=_string(entry.get("returns"), f"functions.{name}.returns"),
        description=description if isinstance(description, str) else "",
        null=null if isinstance(null, str) else "",
        lazy=_flag(entry, "lazy"),
        effect=_flag(entry, "effect"),
        sees_status=_flag(entry, "sees_status"),
        task_operation=_flag(entry, "task_operation"),
    )


def parse_vocabulary(raw: JSON) -> Vocabulary:
    """A ``vocabulary.json`` payload as a :class:`Vocabulary`.

    Raises:
        ValueError: If the payload is not a vocabulary, or contradicts itself (a kind reading
            a root that does not exist, a function outside the function namespaces).
    """
    top = _object(raw, "the file")
    roots = _object(top.get("roots"), "roots")
    data_roots = tuple(_object(roots.get("data"), "roots.data"))
    scoped_roots = tuple(_object(roots.get("scoped"), "roots.scoped"))
    namespaces = _strings(roots.get("functions"), "roots.functions")
    future = _strings(roots.get("future"), "roots.future")
    reserved = frozenset(_strings(top.get("reserved"), "reserved"))
    expected = {*data_roots, *scoped_roots, *namespaces, *future}
    if reserved != frozenset(expected):
        raise ValueError(f"vocabulary.json: reserved differs from the roots by {sorted(reserved ^ expected)}")

    segments = _object(top.get("agent_segments"), "agent_segments")

    kinds: dict[str, Kind] = {}
    for name, item in _object(top.get("kinds"), "kinds").items():
        entry = _object(item, f"kinds.{name}")
        kind_roots = frozenset(_strings(entry.get("roots"), f"kinds.{name}.roots"))
        unknown = sorted(kind_roots - set(data_roots))
        if unknown:
            raise ValueError(f"vocabulary.json: kind {name} reads unknown roots {unknown}")
        description = entry.get("description", "")
        kinds[name] = Kind(
            name=name,
            roots=kind_roots,
            locals=_flag(entry, "locals"),
            contexts=_strings(entry.get("contexts"), f"kinds.{name}.contexts"),
            description=description if isinstance(description, str) else "",
        )

    contexts: dict[str, Context] = {}
    for name, item in _object(top.get("contexts"), "contexts").items():
        entry = _object(item, f"contexts.{name}")
        contexts[name] = Context(name=name, text=_flag(entry, "text"))
    roots_entry = _object(_object(top.get("blok_roots"), "blok_roots").get("roots"), "blok_roots.roots")
    blok_roots = {root: _string(context, f"blok_roots.roots.{root}") for root, context in roots_entry.items()}
    for kind in kinds.values():
        unknown = sorted(set(kind.contexts) - set(contexts))
        if unknown:
            raise ValueError(f"vocabulary.json: kind {kind.name} is drawn in unknown contexts {unknown}")

    records: dict[str, Record] = {}
    idle_task: dict[str, JSON] = {}
    for name, item in _object(top.get("records"), "records").items():
        entry = _object(item, f"records.{name}")
        records[name] = Record(name=name, fields=_record_fields(entry.get("fields"), f"records.{name}.fields"))
        if name == "task":
            idle_task = _object(entry.get("idle"), "records.task.idle")

    functions: dict[str, Function] = {}
    for name, item in _object(top.get("functions"), "functions").items():
        if name == "description":
            continue
        namespace = name.split(".", 1)[0]
        if namespace not in namespaces and name != "task.answer":
            raise ValueError(f"vocabulary.json: function {name} is in no function namespace")
        functions[name] = _function(name, item)

    operators = {
        key: value
        for key, value in _object(top.get("operators"), "operators").items()
        if isinstance(value, str) and key not in ("description", "notes")
    }

    stored = _object(top.get("stored_tree"), "stored_tree")
    theme = _object(top.get("theme"), "theme")
    hints = _object(theme.get("palette_role_hints"), "theme.palette_role_hints")
    limits = _object(top.get("limits"), "limits")
    catalogs = _object(top.get("catalogs"), "catalogs")
    standard = _object(catalogs.get("standard"), "catalogs.standard")
    base_version = catalogs.get("base")
    local_rules = _object(top.get("locals"), "locals")

    def kind_table(name: str) -> dict[str, frozenset[str]]:
        table = _object(local_rules.get(name), f"locals.{name}")
        return {key: frozenset(_strings(value, f"locals.{name}.{key}")) for key, value in table.items() if key != "description"}

    def number(value: JSON, where: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"vocabulary.json: {where} is not an integer")
        return value

    diagnostics: dict[str, DiagnosticSpec] = {}
    for code, item in _object(top.get("diagnostics"), "diagnostics").items():
        if code == "description":
            continue
        entry = _object(item, f"diagnostics.{code}")
        level = entry.get("level")
        if level not in ("error", "warning"):
            raise ValueError(f"vocabulary.json: diagnostic {code} has level {level!r}")
        diagnostics[code] = DiagnosticSpec(
            code=code,
            level="error" if level == "error" else "warning",
            message=_string(entry.get("message"), f"diagnostics.{code}.message"),
            runtime=_flag(entry, "runtime"),
            hint=_string(entry["hint"], f"diagnostics.{code}.hint") if entry.get("hint") is not None else None,
        )

    return Vocabulary(
        vocab=number(top.get("vocab"), "vocab"),
        data_roots=data_roots,
        scoped_roots=scoped_roots,
        function_namespaces=namespaces,
        future_roots=future,
        reserved=reserved,
        tasks_segment=_string(segments.get("tasks"), "agent_segments.tasks"),
        assets_segment=_string(segments.get("assets"), "agent_segments.assets"),
        reserved_state_names=frozenset(_strings(segments.get("reserved_state_names"), "agent_segments")),
        agent_fields=tuple(_object(segments.get("fields"), "agent_segments.fields")),
        kinds=kinds,
        contexts=contexts,
        blok_roots=blok_roots,
        records=records,
        idle_task=idle_task,
        functions=functions,
        operators=operators,
        explicit_id=re.compile(_string(stored.get("explicit_id"), "stored_tree.explicit_id")),
        theme=Theme(
            colour_roles=_strings(theme.get("colour_roles"), "theme.colour_roles"),
            status_roles=_strings(theme.get("status_roles"), "theme.status_roles"),
            spectral_roles=_strings(theme.get("spectral_roles"), "theme.spectral_roles"),
            colour_utilities=_strings(theme.get("colour_utilities"), "theme.colour_utilities"),
            palette_colours=_strings(theme.get("palette_colours"), "theme.palette_colours"),
            palette_role_hints={key: _string(value, "theme.palette_role_hints") for key, value in hints.items()},
            logo_pens=_strings(theme.get("logo_pens"), "theme.logo_pens"),
            logo_paint_props=_strings(theme.get("logo_paint_props"), "theme.logo_paint_props"),
            scene_paint_props=_strings(theme.get("scene_paint_props"), "theme.scene_paint_props"),
            custom_variants=_strings(theme.get("custom_variants"), "theme.custom_variants"),
            data_attributes=tuple(_object(theme.get("data_attributes"), "theme.data_attributes")),
        ),
        limits=Limits(
            logo_max_nodes=number(limits.get("logo_max_nodes"), "limits.logo_max_nodes"),
            logo_max_depth=number(limits.get("logo_max_depth"), "limits.logo_max_depth"),
            logo_max_path_data=number(limits.get("logo_max_path_data"), "limits.logo_max_path_data"),
            logo_max_foreach_items=number(limits.get("logo_max_foreach_items"), "limits.logo_max_foreach_items"),
            logo_path_data_props=_strings(limits.get("logo_path_data_props"), "limits.logo_path_data_props"),
        ),
        base_version=number(base_version, "catalogs.base"),
        catalogs={name: number(version, f"catalogs.standard.{name}") for name, version in standard.items()},
        bind_kinds=kind_table("bind_kinds"),
        arg_kinds=kind_table("arg_kinds"),
        diagnostics=diagnostics,
    )


def vocabulary_text() -> str:
    """``vocabulary.json`` as shipped, for a party that vendors it."""
    return resources.files(__package__ or "arkitekt_spec.blok").joinpath("vocabulary.json").read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def load_vocabulary() -> Vocabulary:
    """The vocabulary this package ships, parsed (cached)."""
    raw: JSON = json.loads(vocabulary_text())
    return parse_vocabulary(raw)


class _Fields(dict[str, object]):
    """Format fields where a missing one reads as ``?`` rather than raising."""

    def __missing__(self, key: str) -> str:
        return "?"


def diagnostic_message(code: str, **fields: object) -> str:
    """The message of ``code``, filled with ``fields``.

    Raises:
        KeyError: If ``code`` is not in the diagnostic table.
    """
    spec = load_vocabulary().diagnostics[code]
    return spec.message.format_map(_Fields(fields))


def diagnostic_hint(code: str, **fields: object) -> str | None:
    """How to fix ``code``, filled with ``fields``; None when its row has no hint.

    Raises:
        KeyError: If ``code`` is not in the diagnostic table.
    """
    spec = load_vocabulary().diagnostics[code]
    return spec.hint.format_map(_Fields(fields)) if spec.hint is not None else None


@dataclass(frozen=True)
class Finding:
    """A diagnostic: a row of the table, filled, and where (a node id, a declaration).

    Two findings are equal by code, level, message and path; the hint, the fields and the place
    in the source (stamped by a checker that parsed it) ride along.
    """

    code: str
    level: Level
    message: str
    path: str | None = None
    hint: str | None = field(default=None, compare=False)
    """How to fix it, filled like the message."""
    fields: Mapping[str, object] = field(default_factory=dict[str, object], compare=False, hash=False)
    """What the message was filled with (``component``, ``key``, ...)."""
    line: int | None = field(default=None, compare=False)
    """1-based, in the BSX source the tree was parsed from, when known."""
    column: int | None = field(default=None, compare=False)

    @classmethod
    def of(cls, code: str, at: str | None = None, /, **fields: object) -> "Finding":
        """The finding ``code``, its message filled with ``fields``.

        Raises:
            KeyError: If ``code`` is not in the diagnostic table.
        """
        spec = load_vocabulary().diagnostics[code]
        return cls(
            code=code,
            level=spec.level,
            message=spec.message.format_map(_Fields(fields)),
            path=at,
            hint=spec.hint.format_map(_Fields(fields)) if spec.hint is not None else None,
            fields=dict(fields),
        )

    def placed(self, line: int, column: int) -> "Finding":
        """The same finding, at ``line``/``column`` of the source."""
        return replace(self, line=line, column=column)

    def to_json(self) -> dict[str, JSON]:
        """The finding as JSON: ``{code, level, message, path?}``."""
        out: dict[str, JSON] = {"code": self.code, "level": self.level, "message": self.message}
        if self.path is not None:
            out["path"] = self.path
        return out


class BlokError(ValueError):
    """A blok refused: its code is a row of the diagnostic table, the same in every parser.

    ``line`` and ``column`` (1-based) say where in a BSX source, when the error has a place.
    """

    def __init__(self, code: str, *, line: int | None = None, column: int | None = None, **fields: object) -> None:
        """Refuse with ``code``, its message filled with ``fields``."""
        self.code = code
        self.line = line
        self.column = column
        self.fields = fields
        self.hint = diagnostic_hint(code, **fields)
        message = diagnostic_message(code, **fields)
        where = f"line {line}, column {column}: " if line is not None and column is not None else ""
        super().__init__(f"{where}{message} [{code}]")
        self.message = message

    @classmethod
    def from_finding(cls, finding: Finding) -> "BlokError":
        """Raise ``finding``: its message as written, its place in front."""
        error = cls.__new__(cls)
        ValueError.__init__(error, f"{finding.path + ': ' if finding.path else ''}{finding.message} [{finding.code}]")
        error.code = finding.code
        error.line = finding.line
        error.column = finding.column
        error.fields = dict(finding.fields)
        error.hint = finding.hint
        error.message = finding.message
        return error

    def within(self, owner: str) -> "BlokError":
        """The same refusal, named after what it refuses (``Blok 'monitor'``): code, line and column kept."""
        error = BlokError.__new__(BlokError)
        where = f"line {self.line}, column {self.column}: " if self.line is not None and self.column is not None else ""
        ValueError.__init__(error, f"{owner}: {where}{self.message} [{self.code}]")
        error.code = self.code
        error.line = self.line
        error.column = self.column
        error.fields = self.fields
        error.hint = self.hint
        error.message = f"{owner}: {self.message}"
        return error


__all__ = [
    "JSON",
    "VOCAB",
    "BlokError",
    "Context",
    "DiagnosticSpec",
    "Finding",
    "Function",
    "Kind",
    "Level",
    "Limits",
    "Param",
    "Record",
    "RecordField",
    "Theme",
    "Vocabulary",
    "diagnostic_hint",
    "diagnostic_message",
    "load_vocabulary",
    "parse_vocabulary",
    "vocabulary_text",
]
