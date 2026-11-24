"""`Blok.hasLogo`: whether a stored tree declares its own `<logo>`."""

from facade.types.blok import blok_has_logo


def _root(*children: dict) -> list[dict]:
    return [{"id": "0", "component": "Blok", "props": [], "children": list(children)}]


def test_a_blok_with_its_own_logo_has_one() -> None:
    logo = {"id": "1", "component": "logo", "props": [], "children": []}
    assert blok_has_logo(_root(logo, {"id": "2", "component": "Card"})) is True


def test_a_blok_without_one_has_none() -> None:
    assert blok_has_logo(_root({"id": "2", "component": "Card"})) is False
    assert blok_has_logo([]) is False


def test_a_logo_inside_the_content_is_not_the_bloks() -> None:
    nested = {
        "id": "2",
        "component": "Card",
        "children": [{"id": "3", "component": "logo"}],
    }
    assert blok_has_logo(_root(nested)) is False
