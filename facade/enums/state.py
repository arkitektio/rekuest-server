from enum import Enum

import strawberry


@strawberry.enum
class JSONPatchOperation(str, Enum):
    add = "add"
    remove = "remove"
    replace = "replace"
    move = "move"
    copy = "copy"
    test = "test"
