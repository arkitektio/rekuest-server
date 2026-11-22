from typing import NewType

import strawberry

Args = NewType("Args", object)
Props = NewType("Props", object)


scalar_map = {
    Args: strawberry.scalar(
        name="Args",
        description="The `Args` scalar type represents a Dictionary of arguments",
        serialize=lambda v: v,
        parse_value=lambda v: v,
    ),
    Props: strawberry.scalar(
        name="Props",
        description="The `Props` scalar type represents a JSON object of UI props or state",
        serialize=lambda v: v,
        parse_value=lambda v: v,
    ),
}
