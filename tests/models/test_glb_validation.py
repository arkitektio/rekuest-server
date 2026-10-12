"""The rules a GLB is held to before it enters the model store."""

import json
import struct
from dataclasses import replace
from typing import Any

import pytest

from facade.glb_validation import ModelPolicy, ModelRefused, model_ref, validate_model


def glb(document: dict[str, Any], binary: bytes = b"") -> bytes:
    """A GLB container around ``document`` and ``binary``, padded as the format requires."""
    payload = json.dumps(document).encode()
    payload += b" " * (-len(payload) % 4)
    binary += b"\0" * (-len(binary) % 4)
    chunks = struct.pack("<II", len(payload), 0x4E4F534A) + payload
    if binary:
        chunks += struct.pack("<II", len(binary), 0x004E4942) + binary
    return struct.pack("<III", 0x46546C67, 2, 12 + len(chunks)) + chunks


def triangle(**extra: Any) -> tuple[dict[str, Any], bytes]:
    """One triangle on a node called ``stage_x``."""
    binary = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    document: dict[str, Any] = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(binary)}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "nodes": [{"name": "stage_x", "mesh": 0}, {"name": "turret"}],
    }
    document.update(extra)
    return document, binary


def refusal(data: bytes, policy: ModelPolicy | None = None, ref: str | None = None) -> ModelRefused:
    with pytest.raises(ModelRefused) as caught:
        validate_model(data, ref or model_ref(data), policy)
    return caught.value


def test_a_plain_model_is_accepted_and_described() -> None:
    data = glb(*triangle())
    report = validate_model(data, model_ref(data))
    assert report.nodes == ["stage_x", "turret"]
    assert (report.stats.nodes, report.stats.triangles, report.stats.animations) == (2, 1, 0)
    assert report.size == len(data)
    assert report.warnings == []


def test_the_reference_is_the_hash_of_the_bytes() -> None:
    data = glb(*triangle())
    assert refusal(data, ref="sha256:" + "0" * 64).code == "hash_mismatch"
    assert refusal(data, ref="md5:abc").code == "bad_reference"
    assert refusal(data, ref=model_ref(data).upper()).code == "bad_reference"


def test_size_is_checked_before_anything_is_parsed() -> None:
    assert refusal(b"x" * 100, replace(ModelPolicy(), max_size=50)).code == "too_large"


def test_the_header_must_be_a_version_2_glb_of_the_stored_length() -> None:
    data = glb(*triangle())
    assert refusal(b"{}" + data[2:]).code == "bad_header"
    assert refusal(data[:4] + struct.pack("<I", 1) + data[8:]).code == "bad_header"
    assert refusal(data + b"\0\0\0\0").code == "bad_header"
    assert refusal(b"glTF").code == "bad_header"


def test_the_json_chunk_must_be_first_small_enough_and_gltf_2() -> None:
    document, binary = triangle()
    assert refusal(glb(document, binary), replace(ModelPolicy(), max_json=16)).code == "bad_structure"
    assert refusal(glb({**document, "asset": {"version": "1.0"}}, binary)).code == "bad_structure"
    data = glb(document, binary)
    assert refusal(data[:16] + struct.pack("<I", 0x004E4942) + data[20:]).code == "bad_structure"


def test_buffer_views_and_accessors_must_lie_inside_the_binary_chunk() -> None:
    document, binary = triangle()
    assert refusal(glb({**document, "buffers": [{"byteLength": len(binary) + 64}]}, binary)).code == "bad_structure"
    assert refusal(glb({**document, "bufferViews": [{"buffer": 0, "byteOffset": 8, "byteLength": len(binary)}]}, binary)).code == "bad_structure"
    oversized = [{"bufferView": 0, "componentType": 5126, "count": 4, "type": "VEC3"}]
    assert refusal(glb({**document, "accessors": oversized}, binary)).code == "bad_structure"


def test_a_model_carries_all_its_data() -> None:
    document, binary = triangle()
    assert refusal(glb({**document, "images": [{"uri": "https://example.org/t.png"}]}, binary)).code == "external_reference"
    assert refusal(glb({**document, "images": [{"uri": "data:image/png;base64,AAAA"}]}, binary)).code == "external_reference"
    assert refusal(glb({**document, "buffers": [{"byteLength": len(binary), "uri": "mesh.bin"}]}, binary)).code == "external_reference"


def test_required_extensions_must_be_allowed_and_unsupported_used_ones_only_warn() -> None:
    document, binary = triangle()
    assert refusal(glb({**document, "extensionsRequired": ["VENDOR_secret"]}, binary)).code == "unsupported_extension"

    data = glb({**document, "extensionsUsed": ["VENDOR_hint", "KHR_lights_punctual"], "extensionsRequired": ["KHR_mesh_quantization"]}, binary)
    report = validate_model(data, model_ref(data))
    assert len(report.warnings) == 1 and "VENDOR_hint" in report.warnings[0]


def test_counts_are_budgeted_from_what_the_json_declares() -> None:
    data = glb(*triangle())
    assert refusal(data, replace(ModelPolicy(), max_nodes=1)).code == "over_budget"
    assert refusal(data, replace(ModelPolicy(), max_triangles=0)).code == "over_budget"
    with_clips = glb(*triangle(animations=[{}, {}]))
    assert refusal(with_clips, replace(ModelPolicy(), max_animations=1)).code == "over_budget"


def test_repeated_and_unconventional_node_names_are_warnings() -> None:
    document, binary = triangle()
    data = glb({**document, "nodes": [{"name": "arm"}, {"name": "arm"}, {"name": "Stage X"}]}, binary)
    report = validate_model(data, model_ref(data))
    assert report.nodes == ["Stage X", "arm"]
    assert any("more than once" in warning for warning in report.warnings)
    assert any("Stage X" in warning for warning in report.warnings)


JOINTS = [
    {"name": "elbow", "node": "stage_x", "type": "revolute", "axis": [0, 0, 1], "limit": {"lower": -2.0, "upper": 2.0}},
    {"name": "wrist", "node": "turret", "type": "continuous", "axis": [1, 0, 0], "mimic": {"joint": "elbow", "multiplier": -1.0, "offset": 0.0}},
]


def with_joints(joints: list[dict[str, Any]], **arkitekt: Any) -> bytes:
    return glb(*triangle(extras={"arkitekt": {"version": 1, "joints": joints, **arkitekt}}))


def test_a_joint_table_is_accepted() -> None:
    data = with_joints(JOINTS, source="sha256:" + "a" * 64)
    assert validate_model(data, model_ref(data)).nodes == ["stage_x", "turret"]


@pytest.mark.parametrize(
    "joints",
    [
        [{**JOINTS[0], "node": "missing"}],
        [{**JOINTS[0], "type": "floating"}],
        [{**JOINTS[0], "axis": [0, 0, 2]}],
        [{**JOINTS[0], "limit": {"lower": 1.0, "upper": -1.0}}],
        [{key: value for key, value in JOINTS[0].items() if key != "limit"}],
        [JOINTS[0], {**JOINTS[0], "node": "turret"}],
        [JOINTS[0], {**JOINTS[1], "node": "stage_x"}],
        [JOINTS[0], {**JOINTS[1], "mimic": {"joint": "nobody"}}],
        [{**JOINTS[1], "limit": {"lower": 0.0, "upper": 1.0}, "mimic": None}],
    ],
)
def test_a_joint_table_that_does_not_fit_the_model_is_refused(joints: list[dict[str, Any]]) -> None:
    assert refusal(with_joints(joints)).code == "bad_extras"


def test_an_unknown_extras_version_is_refused() -> None:
    assert refusal(glb(*triangle(extras={"arkitekt": {"version": 2}}))).code == "bad_extras"
