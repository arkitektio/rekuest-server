from abc import ABC, abstractmethod


class BaseHare(ABC):


    def __init__(self) -> None:
        pass


    @abstractmethod
    async def connect(self):
        raise NotImplementedError("Nanan")



    async def close(self):
        pass


    async def __aenter__(self):
        await self.connect()
        return self


    async def __aclose__(self, *args, **kwargs):
        await self.close()