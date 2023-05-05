import asyncio
from asgiref.sync import async_to_sync
import aiormq
import logging
from django.conf import settings
import pika
import threading
logger = logging.getLogger(__name__)




class PikaConnection:

    def __init__(self, url= None) -> None:
        
        try:
            self.connection = pika.BlockingConnection(pika.ConnectionParameters(host=settings.BROKER_HOST, port=settings.BROKER_PORT, credentials=pika.PlainCredentials(username=settings.BROKER_USERNAME, password=settings.BROKER_PASSWORD), heartbeat=600, blocked_connection_timeout=300))
            self.channel = self.connection.channel()
        except Exception as e:
            logger.error(f"Error connecting to rabbitmq {e} with {settings.BROKER_HOST} {settings.BROKER_PORT} {settings.BROKER_VHOST} {settings.BROKER_USERNAME} {settings.BROKER_PASSWORD}")
            raise e

    def publish(self, routing_key, message):
        self.channel.basic_publish(
            body=message,
            routing_key=routing_key,
            exchange="",
        )


class ThreadedConnection(threading.Thread):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.daemon = True
        self.is_running = True
        self.name = "Publisher"
        self.queue = "downstream_queue"

        try:
            self.connection = pika.BlockingConnection(pika.ConnectionParameters(host=settings.BROKER_HOST, port=settings.BROKER_PORT, credentials=pika.PlainCredentials(username=settings.BROKER_USERNAME, password=settings.BROKER_PASSWORD), heartbeat=600, blocked_connection_timeout=300))
            self.channel = self.connection.channel()
        except Exception as e:
            logger.error(f"Error connecting to rabbitmq {e} with {settings.BROKER_HOST} {settings.BROKER_PORT} {settings.BROKER_VHOST} {settings.BROKER_USERNAME} {settings.BROKER_PASSWORD}")
            raise e

    def run(self):
        while self.is_running:
            self.connection.process_data_events(time_limit=1)

    def publish(self, routing_key, message):
        self.connection.add_callback_threadsafe(lambda: self._publish(routing_key, message))

    def _publish(self, routing_key, message):
        self.channel.basic_publish(
            body=message,
            routing_key=routing_key,
            exchange="",
        )

    def stop(self):
        print("Stopping...")
        self.is_running = False
        # Wait until all the data events have been processed
        self.connection.process_data_events(time_limit=1)
        if self.connection.is_open:
            self.connection.close()
        print("Stopped")

class AioRMQConnection:
    def __init__(self, url= None) -> None:
        self.url = url or settings.BROKER_URL
        self.connection = None
        self.open_channels = []
        self._lock = None

        self.publish_channel = None

    async def aconnect(self):
        self.connection = await aiormq.connect(self.url)
        self._loop = asyncio.get_event_loop()
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
pikaconnection = ThreadedConnection()
pikaconnection.start()