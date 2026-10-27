"""The catalogs a v2 blok renders with, as the spec owns them (vendored from arkitekt-spec's
``arkitekt_spec/blok``), and the view a blok is checked against.

A *catalog* is a spec-owned, versioned set a blok declares (``catalogs: ["ui@1"]``); a *module*
is an app's own unversioned extras, registered as a ``Module``, which a blok names as its
``module``.

- ``catalogs/base@2.json``: the language every renderer implements (``if``, ``else``,
  ``foreach``, ``match``, ``suspense``, ``ask``, ...). Its functions are the vocabulary's.
- The standard catalogs, versioned: ``catalogs/ui@1.json``, ``scene@1``, ``logo@1``,
  ``structure@1`` and ``html@1``.
- ``modules/orkestrator.json``: the ``orkestrator`` module, what only the Orkestrator draws.

A view is base, every standard catalog and the orkestrator module, plus what the blok's registered
``Module`` adds. A name two of them define differently is drawn as :class:`Shadow` says: a
module's definition beats a standard catalog's (the app draws its own ``Slider``), base's is never
shadowed, and between two standard catalogs the first is kept; the checker warns
``catalog.shadow`` where a tree uses it. The Orkestrator's own registration (``orkestrator``) only
adds what the vendored module lacks: the vendored file is the spec's, so it keeps every name it
defines, with no shadow.

Port validators and effects are not blok trees: they stay on base@1 (``rekuest_core.catalogs``).
"""

import functools
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources

from pydantic import BaseModel, Field

from rekuest_core.inputs.models import CatalogComponentInputModel, CatalogOperationInputModel, ComponentNodeInputModel
from rekuest_core.vocabulary.vocabulary import Function, Param, load_vocabulary

BASE_NAME = "base"
ORKESTRATOR_MODULE = "orkestrator"
"""The module of what only the Orkestrator draws, beside the standard catalogs."""
ORKESTRATOR_MODULE_FILE = "modules/orkestrator.json"
"""Where the Orkestrator's module ships, relative to this package."""
STANDARD_CATALOGS = ("ui", "scene", "logo", "structure", "html")


class _CatalogFile(BaseModel):
    """A spec catalog file."""

    name: str
    version: int | None = None
    description: str | None = None
    components: list[CatalogComponentInputModel] = Field(default_factory=list)


def _read(file: str) -> _CatalogFile:
    text = resources.files(__package__ or "rekuest_core.vocabulary").joinpath(file).read_text(encoding="utf-8")
    catalog = _CatalogFile.model_validate(json.loads(text))
    label = f"{catalog.name}@{catalog.version}" if catalog.version is not None else catalog.name
    catalog.components = [component.model_copy(update={"catalog": label}) for component in catalog.components]
    return catalog


def catalog_files() -> tuple[str, ...]:
    """Every spec-owned catalog file, base first, and the Orkestrator's module, relative to this package."""
    vocabulary = load_vocabulary()
    return (
        f"catalogs/{BASE_NAME}@{vocabulary.base_version}.json",
        *(f"catalogs/{name}@{version}.json" for name, version in vocabulary.catalogs.items()),
        ORKESTRATOR_MODULE_FILE,
    )


@functools.lru_cache(maxsize=1)
def spec_catalogs() -> tuple[_CatalogFile, ...]:
    """base@2, the standard catalogs and the Orkestrator's module, as vendored."""
    return tuple(_read(file) for file in catalog_files())


@functools.lru_cache(maxsize=1)
def base_v2_component_names() -> frozenset[str]:
    """The language components (``if``, ``foreach``, ``suspense``, ...): no catalog redefines them."""
    return frozenset(component.name for component in spec_catalogs()[0].components)


def _same(first: CatalogComponentInputModel, second: CatalogComponentInputModel) -> bool:
    """Whether two catalogs define a component alike; which catalog it came from does not count."""
    return first.model_dump(exclude={"catalog"}) == second.model_dump(exclude={"catalog"})


@dataclass(frozen=True)
class Shadow:
    """A component name two sources define: which one is drawn."""

    component: str
    catalog: str
    """The source that defines it first: base or a standard catalog (``ui@1``)."""
    module: str
    """The source that defines it again: a module, or a later standard catalog."""
    winner: str
    """The one of the two whose definition is used."""


@dataclass(frozen=True)
class BlokView:
    """What a v2 blok is checked against: components by name and functions by name."""

    components: Mapping[str, CatalogComponentInputModel]
    functions: Mapping[str, Function]
    label: str
    complete: bool = True
    """Whether the components are every one the renderer draws, so an unknown one is a finding."""
    shadows: Mapping[str, Shadow] = field(default_factory=dict[str, Shadow])
    """Per component name two sources define, which one is used (``catalog.shadow``)."""

    def component(self, name: str) -> CatalogComponentInputModel | None:
        """The component called ``name``, or None."""
        return self.components.get(name)


def _operation_function(operation: CatalogOperationInputModel) -> Function:
    return Function(
        name=operation.name,
        params=tuple(Param(key=argument.key, kind=argument.kind.value, required=argument.required) for argument in operation.arguments),
        returns=operation.returns.value,
        description=operation.description or "",
    )


Registered = tuple[str, Sequence[CatalogComponentInputModel], Sequence[CatalogOperationInputModel]]
"""A module as a ``Module`` registered it: its name, components and operations."""


def blok_view(modules: Sequence[Registered] = (), *, complete: bool = True) -> BlokView:
    """The view base, the standard catalogs, the Orkestrator's module and ``modules`` give.

    A name two sources define differently is recorded as a :class:`Shadow`: a module (the
    vendored ``orkestrator`` one included) beats a standard catalog, base is never shadowed,
    between two standard catalogs the first is kept. A registered ``orkestrator`` only adds the
    names the vendored module lacks. A module's operations join the vocabulary's functions only
    under a function namespace (``fmt.quantity``): a new namespace is a new root, which ships
    with a new vocab.
    """
    vocabulary = load_vocabulary()
    components: dict[str, CatalogComponentInputModel] = {}
    shadows: dict[str, Shadow] = {}
    labels: list[str] = []
    base_label = ""

    def add(component: CatalogComponentInputModel, source: str, module: bool) -> None:
        previous = components.get(component.name)
        if previous is None:
            components[component.name] = component
            return
        if _same(previous, component):
            return
        kept = previous.catalog or "?"
        other = component.catalog or source
        wins = module and kept != base_label
        if wins:
            components[component.name] = component
        shadows[component.name] = Shadow(component=component.name, catalog=kept, module=other, winner=other if wins else kept)

    for catalog in spec_catalogs():
        label = f"{catalog.name}@{catalog.version}" if catalog.version is not None else catalog.name
        labels.append(label)
        if catalog.name == BASE_NAME:
            base_label = label
        for component in catalog.components:
            add(component, label, catalog.version is None)
    for name, registered, _operations in modules:
        labels.append(name)
        for component in registered:
            stamped = component.model_copy(update={"catalog": component.catalog or name})
            if name == ORKESTRATOR_MODULE:
                components.setdefault(stamped.name, stamped)
            else:
                add(stamped, name, True)
    functions: dict[str, Function] = dict(vocabulary.functions)
    for _name, _components, operations in modules:
        for operation in operations:
            namespace = operation.name.split(".", 1)[0]
            if operation.name in functions or namespace not in vocabulary.function_namespaces:
                continue
            functions[operation.name] = _operation_function(operation)
    return BlokView(components=components, functions=functions, label=" + ".join(labels), complete=complete, shadows=shadows)


@functools.lru_cache(maxsize=1)
def default_view() -> BlokView:
    """The Orkestrator's view: base, every standard catalog and its module."""
    return blok_view()


def catalogs_of(components: Sequence[ComponentNodeInputModel], view: BlokView) -> tuple[str, ...]:
    """The versioned catalogs (``ui@1``) whose components a tree uses; base is every renderer's."""
    found: set[str] = set()
    stack = list(components)
    while stack:
        node = stack.pop()
        spec = view.component(node.component)
        if spec is not None and spec.catalog is not None and "@" in spec.catalog and not spec.catalog.startswith(f"{BASE_NAME}@"):
            found.add(spec.catalog)
        stack.extend(node.children or ())
    return tuple(sorted(found))


__all__ = [
    "BASE_NAME",
    "ORKESTRATOR_MODULE",
    "ORKESTRATOR_MODULE_FILE",
    "STANDARD_CATALOGS",
    "BlokView",
    "Registered",
    "Shadow",
    "base_v2_component_names",
    "blok_view",
    "catalog_files",
    "default_view",
    "catalogs_of",
    "spec_catalogs",
]
