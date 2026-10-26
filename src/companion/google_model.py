from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class GoogleModel(BaseModel):
    """A Google API payload: snake_case in Python, camelCase on the wire."""

    model_config = ConfigDict(
        alias_generator=to_camel, validate_by_name=True, serialize_by_alias=True
    )
