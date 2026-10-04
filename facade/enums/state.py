from enum import Enum

import strawberry


@strawberry.enum
class JSONPatchOperation(str, Enum):
    add = "add"
    remove = "remove"
    replace = "replace"  # pyright: ignore[reportAssignmentType]  the JSON Patch operation, which is also a method of str
    move = "move"
    copy = "copy"
    test = "test"
