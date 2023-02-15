import asyncio
from asgiref.sync import async_to_sync
import aiormq
import logging
from django.conf import settings

logger = logging.getLogger(__name__)


class AioRMQConnection:
    def __init__(self, url= None) -> None:
        self.url = url or settings.BROKER_URL
        self.connection = None
        self.open_channels = []
        self._lock = None

        self.publish_channel = None

    async def aconnect(self):
        self.connection = await aiormq.connect(self.url)
        self.publish_channel = await self.connection.channel()

    async def open_channel(self):
        if not self._lock:
            self._lock = asyncio.Lock()

        async with self._lock:
            if not self.connection:
                await self.aconnect()

        channel = await self.connection.channel()
        self.open_channels.append(channel)
        return channel

    async def apublish(self, routing_key, message):
        if not self._lock:
            self._lock = asyncio.Lock()

        async with self._lock:
            if not self.connection:
                await self.aconnect()

        await self.publish_channel.basic_publish(
            message,
            routing_key=routing_key,  # Lets take the first best one
        )

    async def afanout(self, exchange, message):
        if not self._lock:
            self._lock = asyncio.Lock()

        async with self._lock:
            if not self.connection:
                await self.aconnect()

        await self.publish_channel.exchange_declare(
            exchange=exchange, exchange_type='fanout'
        )

        await self.publish_channel.basic_publish(
            message,
            exchange=exchange,
            routing_key="",  # Lets take the first best one
        )




    def publish(self, routing_key, message):
        logger.error(f"Publishing message to {routing_key} {message}")
        return async_to_sync(self.apublish)(routing_key, message)

    def fanout(self, routing_key, message):
        logger.error(f"Faning out message to {routing_key} {message}")
        return async_to_sync(self.afanout)(routing_key, message)


rmq = AioRMQConnection()
