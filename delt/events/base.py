from pydantic.main import BaseModel

class Event(BaseModel):
    type: str