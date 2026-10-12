"""What a GLB must be to enter the model store, checked on its bytes.

A model is identified by the SHA-256 of its exact bytes, so the reference is checked here too:
no storage backend is trusted to have enforced it. Everything else is read from the 12-byte
header and the JSON chunk; the binary chunk is only measured, never decoded, which is why
texture sizes are left to the clients that decode them.

This module imports nothing from Django so the same rules can be run by authoring tools.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass, field
from typing import Any

GLB_MAGIC = 0x46546C67  # "glTF"
CHUNK_JSON = 0x4E4F534A  # "JSON"
CHUNK_BIN = 0x004E4942  # "BIN\0"

REF_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
NODE_NAME_PATTERN = re.compile(r"[a-z0-9_]+")

JOINT_TYPES = frozenset({"revolute", "continuous", "prismatic"})

_COMPONENT_BYTES = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
_TYPE_COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}

DEFAULT_EXTENSIONS = frozenset(
    {
        "KHR_lights_punctual",
        "KHR_materials_emissive_strength",
        "KHR_materials_variants",
        "KHR_texture_transform",
        "KHR_mesh_quantization",
        "EXT_meshopt_compression",
        "KHR_draco_mesh_compression",
    }
)


@dataclass(frozen=True)
class ModelPolicy:
    """The limits a model is held to when it is created."""

    max_size: int = 50 * 1024 * 1024
    max_json: int = 4 * 1024 * 1024
    max_nodes: int = 10_000
    max_triangles: int = 2_000_000
    max_texture_size: int = 4096
    max_texture_memory: int = 256 * 1024 * 1024
    max_animations: int = 64
    extensions: frozenset[str] = DEFAULT_EXTENSIONS


class ModelRefused(ValueError):
    """A model that may not enter the store, with the code clients switch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ModelStats:
    nodes: int
    triangles: int
    textures: int
    animations: int


@dataclass(frozen=True)
class ModelReport:
    """What validation learned about an accepted model."""

    ref: str
    size: int
    nodes: list[str]
    stats: ModelStats
    warnings: list[str] = field(default_factory=list)


def model_ref(data: bytes) -> str:
    """The reference of these exact bytes."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def check_ref_format(ref: str) -> None:
    if not REF_PATTERN.fullmatch(ref):
        raise ModelRefused("bad_reference", f"a model reference is 'sha256:' and 64 lowercase hex characters, not {ref!r}")


def validate_model(data: bytes, ref: str, policy: ModelPolicy | None = None) -> ModelReport:
    """Check ``data`` against ``ref`` and ``policy``; return what it contains or raise ``ModelRefused``."""
    policy = policy or ModelPolicy()
    check_ref_format(ref)
    if len(data) > policy.max_size:
        raise ModelRefused("too_large", f"the model is {len(data)} bytes, over the limit of {policy.max_size}")
    actual = model_ref(data)
    if actual != ref:
        raise ModelRefused("hash_mismatch", f"the stored bytes are {actual}, not {ref}")

    document, binary_length = _read_chunks(data, policy)
    warnings: list[str] = []
    _check_self_contained(document)
    _check_extensions(document, policy, warnings)
    _check_bounds(document, binary_length)
    node_names = _node_names(document, warnings)
    _check_extras(document, node_names)
    stats = _stats(document)
    _check_counts(stats, policy)
    return ModelReport(ref=ref, size=len(data), nodes=sorted(node_names), stats=stats, warnings=warnings)


def _read_chunks(data: bytes, policy: ModelPolicy) -> tuple[dict[str, Any], int]:
    """The parsed JSON chunk and the length of the binary chunk (0 when there is none)."""
    if len(data) < 20:
        raise ModelRefused("bad_header", "the file is shorter than a GLB header and one chunk header")
    magic, version, declared = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC:
        raise ModelRefused("bad_header", "the file does not start with the glTF magic; only binary glTF (.glb) is stored")
    if version != 2:
        raise ModelRefused("bad_header", f"the container version is {version}, only 2 is supported")
    if declared != len(data):
        raise ModelRefused("bad_header", f"the header declares {declared} bytes but the file has {len(data)}")

    json_length, json_type = struct.unpack_from("<II", data, 12)
    if json_type != CHUNK_JSON:
        raise ModelRefused("bad_structure", "the first chunk is not the JSON chunk")
    if json_length > policy.max_json:
        raise ModelRefused("bad_structure", f"the JSON chunk is {json_length} bytes, over the limit of {policy.max_json}")
    json_end = 20 + json_length
    if json_end > len(data):
        raise ModelRefused("bad_structure", "the JSON chunk runs past the end of the file")
    try:
        document = json.loads(data[20:json_end].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ModelRefused("bad_structure", f"the JSON chunk does not parse: {error}") from error
    if not isinstance(document, dict):
        raise ModelRefused("bad_structure", "the JSON chunk is not an object")
    asset = document.get("asset")
    if not isinstance(asset, dict) or asset.get("version") != "2.0":
        raise ModelRefused("bad_structure", "asset.version must be '2.0'")

    binary_length = 0
    if json_end < len(data):
        if json_end + 8 > len(data):
            raise ModelRefused("bad_structure", "a chunk header is cut off after the JSON chunk")
        binary_length, binary_type = struct.unpack_from("<II", data, json_end)
        if binary_type != CHUNK_BIN:
            raise ModelRefused("bad_structure", "the chunk after the JSON chunk is not the binary chunk")
        if json_end + 8 + binary_length > len(data):
            raise ModelRefused("bad_structure", "the binary chunk runs past the end of the file")
    return document, binary_length


def _items(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = document.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ModelRefused("bad_structure", f"'{key}' must be a list of objects")
    return value


def _check_self_contained(document: dict[str, Any]) -> None:
    for key in ("buffers", "images"):
        for index, item in enumerate(_items(document, key)):
            if "uri" in item:
                raise ModelRefused("external_reference", f"{key}[{index}] has a uri; a model carries all its data in its binary chunk")


def _check_extensions(document: dict[str, Any], policy: ModelPolicy, warnings: list[str]) -> None:
    required = document.get("extensionsRequired", [])
    used = document.get("extensionsUsed", [])
    if not isinstance(required, list) or not isinstance(used, list):
        raise ModelRefused("bad_structure", "extensionsRequired and extensionsUsed must be lists")
    refused = sorted(set(required) - policy.extensions)
    if refused:
        raise ModelRefused("unsupported_extension", f"the model requires extensions that are not allowed: {refused}")
    ignored = sorted(set(used) - set(required) - policy.extensions)
    if ignored:
        warnings.append(f"extensions used but not supported are ignored by viewers: {ignored}")


def _is_fallback_buffer(buffer: dict[str, Any]) -> bool:
    """A meshopt fallback buffer has a length but no data in the file."""
    extension = buffer.get("extensions", {}).get("EXT_meshopt_compression")
    return isinstance(extension, dict) and extension.get("fallback") is True


def _span(item: dict[str, Any], what: str) -> tuple[int, int]:
    offset, length = item.get("byteOffset", 0), item.get("byteLength")
    if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length < 0:
        raise ModelRefused("bad_structure", f"{what} needs a non-negative integer byteOffset and byteLength")
    return offset, length


def _check_bounds(document: dict[str, Any], binary_length: int) -> None:
    buffers = _items(document, "buffers")
    views = _items(document, "bufferViews")
    for index, buffer in enumerate(buffers):
        if _is_fallback_buffer(buffer):
            continue
        if index != 0:
            raise ModelRefused("bad_structure", f"buffers[{index}] has no data: only the first buffer is the binary chunk")
        _, length = _span(buffer, "buffers[0]")
        if length > binary_length:
            raise ModelRefused("bad_structure", f"buffers[0] declares {length} bytes but the binary chunk has {binary_length}")

    def check_view(what: str, source: dict[str, Any]) -> None:
        buffer_index = source.get("buffer")
        if not isinstance(buffer_index, int) or not 0 <= buffer_index < len(buffers):
            raise ModelRefused("bad_structure", f"{what} names a buffer that does not exist")
        offset, length = _span(source, what)
        if offset + length > _span(buffers[buffer_index], f"buffers[{buffer_index}]")[1]:
            raise ModelRefused("bad_structure", f"{what} runs past the end of buffers[{buffer_index}]")

    for index, view in enumerate(views):
        check_view(f"bufferViews[{index}]", view)
        compressed = view.get("extensions", {}).get("EXT_meshopt_compression")
        if isinstance(compressed, dict):
            check_view(f"bufferViews[{index}] (compressed)", compressed)

    for index, accessor in enumerate(_items(document, "accessors")):
        count = accessor.get("count")
        if not isinstance(count, int) or count < 0:
            raise ModelRefused("bad_structure", f"accessors[{index}] needs a non-negative integer count")
        view_index = accessor.get("bufferView")
        if view_index is None:
            continue
        if not isinstance(view_index, int) or not 0 <= view_index < len(views):
            raise ModelRefused("bad_structure", f"accessors[{index}] names a bufferView that does not exist")
        component = _COMPONENT_BYTES.get(accessor.get("componentType"))
        components = _TYPE_COMPONENTS.get(accessor.get("type"))
        if component is None or components is None:
            raise ModelRefused("bad_structure", f"accessors[{index}] has an unknown componentType or type")
        element = component * components
        view = views[view_index]
        stride = view.get("byteStride", element)
        offset = accessor.get("byteOffset", 0)
        if not isinstance(stride, int) or not isinstance(offset, int) or stride < 0 or offset < 0:
            raise ModelRefused("bad_structure", f"accessors[{index}] has an invalid byteOffset or byteStride")
        needed = offset + (stride * (count - 1) + element if count else 0)
        if needed > _span(view, f"bufferViews[{view_index}]")[1]:
            raise ModelRefused("bad_structure", f"accessors[{index}] runs past the end of bufferViews[{view_index}]")


def _node_names(document: dict[str, Any], warnings: list[str]) -> set[str]:
    """The names bindings can address. Unnamed nodes are fine; a repeated name is not addressable."""
    names: set[str] = set()
    repeated: set[str] = set()
    unconventional: set[str] = set()
    for node in _items(document, "nodes"):
        name = node.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in names:
            repeated.add(name)
        names.add(name)
        if not NODE_NAME_PATTERN.fullmatch(name):
            unconventional.add(name)
    if repeated:
        warnings.append(f"node names used more than once cannot be bound reliably: {sorted(repeated)}")
    if unconventional:
        warnings.append(f"node names outside [a-z0-9_]+: {sorted(unconventional)}")
    return names


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_extras(document: dict[str, Any], node_names: set[str]) -> None:
    """``extras.arkitekt``, if present, is the joint table of this schema version."""
    extras = document.get("extras")
    arkitekt = extras.get("arkitekt") if isinstance(extras, dict) else None
    if arkitekt is None:
        return

    def fail(message: str) -> ModelRefused:
        return ModelRefused("bad_extras", f"extras.arkitekt: {message}")

    if not isinstance(arkitekt, dict):
        raise fail("must be an object")
    if arkitekt.get("version") != 1:
        raise fail(f"version {arkitekt.get('version')!r} is not supported, only 1")
    joints = arkitekt.get("joints", [])
    if not isinstance(joints, list):
        raise fail("joints must be a list")
    source = arkitekt.get("source")
    if source is not None and not (isinstance(source, str) and REF_PATTERN.fullmatch(source)):
        raise fail("source must be a sha256: reference")

    joint_names: set[str] = set()
    driven: set[str] = set()
    for index, joint in enumerate(joints):
        where = f"joints[{index}]"
        if not isinstance(joint, dict):
            raise fail(f"{where} must be an object")
        name, node, kind, axis = joint.get("name"), joint.get("node"), joint.get("type"), joint.get("axis")
        if not isinstance(name, str) or not name:
            raise fail(f"{where} needs a name")
        if name in joint_names:
            raise fail(f"joint {name!r} is defined twice")
        joint_names.add(name)
        if node not in node_names:
            raise fail(f"joint {name!r} drives node {node!r}, which the model does not have")
        if node in driven:
            raise fail(f"node {node!r} is driven by more than one joint")
        driven.add(node)
        if kind not in JOINT_TYPES:
            raise fail(f"joint {name!r} has type {kind!r}; supported are {sorted(JOINT_TYPES)}")
        if not (isinstance(axis, list) and len(axis) == 3 and all(_number(component) for component in axis)):
            raise fail(f"joint {name!r} needs an axis of three numbers")
        if abs(math.sqrt(sum(component * component for component in axis)) - 1.0) > 1e-3:
            raise fail(f"the axis of joint {name!r} is not a unit vector")
        limit = joint.get("limit")
        if kind == "continuous":
            if limit is not None:
                raise fail(f"continuous joint {name!r} has no limit")
        else:
            if not (isinstance(limit, dict) and _number(limit.get("lower")) and _number(limit.get("upper"))):
                raise fail(f"joint {name!r} needs a limit with lower and upper")
            if limit["lower"] > limit["upper"]:
                raise fail(f"the limit of joint {name!r} has lower above upper")

    for joint in joints:
        mimic = joint.get("mimic")
        if mimic is None:
            continue
        if not (isinstance(mimic, dict) and _number(mimic.get("multiplier", 1.0)) and _number(mimic.get("offset", 0.0))):
            raise fail(f"the mimic of joint {joint['name']!r} needs a numeric multiplier and offset")
        if mimic.get("joint") not in joint_names or mimic.get("joint") == joint["name"]:
            raise fail(f"joint {joint['name']!r} mimics {mimic.get('joint')!r}, which is not another joint of this model")


def _stats(document: dict[str, Any]) -> ModelStats:
    accessors = _items(document, "accessors")

    def count_of(index: object) -> int:
        if not isinstance(index, int) or not 0 <= index < len(accessors):
            raise ModelRefused("bad_structure", "a mesh primitive names an accessor that does not exist")
        return int(accessors[index].get("count", 0))

    triangles = 0
    for mesh in _items(document, "meshes"):
        primitives = mesh.get("primitives", [])
        if not isinstance(primitives, list):
            raise ModelRefused("bad_structure", "mesh primitives must be a list")
        for primitive in primitives:
            if not isinstance(primitive, dict):
                raise ModelRefused("bad_structure", "a mesh primitive must be an object")
            mode = primitive.get("mode", 4)
            if mode not in (4, 5, 6):
                continue
            attributes = primitive.get("attributes", {})
            if "indices" in primitive:
                vertices = count_of(primitive["indices"])
            elif isinstance(attributes, dict) and "POSITION" in attributes:
                vertices = count_of(attributes["POSITION"])
            else:
                continue
            triangles += vertices // 3 if mode == 4 else max(vertices - 2, 0)

    return ModelStats(
        nodes=len(_items(document, "nodes")),
        triangles=triangles,
        textures=len(_items(document, "images")),
        animations=len(_items(document, "animations")),
    )


def _check_counts(stats: ModelStats, policy: ModelPolicy) -> None:
    for what, value, limit in (
        ("nodes", stats.nodes, policy.max_nodes),
        ("triangles", stats.triangles, policy.max_triangles),
        ("animation clips", stats.animations, policy.max_animations),
    ):
        if value > limit:
            raise ModelRefused("over_budget", f"the model has {value} {what}, over the limit of {limit}")
