import asyncio

import aiormq


class AioRMQConnection:
    def __init__(self, url="amqp://guest:guest@mister/") -> None:
        self.url = url
        self.connection = None
        self.open_channels = []
        self._lock = None

    async def aconnect(self):
        self.connection = await aiormq.connect(self.url)

    async def open_channel(self):
        if not self._lock:
            self._lock = asyncio.Lock()

        async with self._lock:
            if not self.connection:
                await self.aconnect()

        channel = await self.connection.channel()
        self.open_channels.append(channel)
        return channel


rmq = AioRMQConnection()
