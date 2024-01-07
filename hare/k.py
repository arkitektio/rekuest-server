from kombu import Connection, Exchange, Queue
import logging


logger = logging.getLogger(__name__)

connection = Connection("amqp://guest:guest@mister/")
exchange = Exchange("arkitekt", type="direct")
video_queue = Queue("video", exchange=exchange, routing_key="video")


producer = connection.Producer(serializer="json")


def send_to_arkitekt(routing_key, message):
    logger.error(f"Publishing message to {routing_key} {message}")
    producer.publish(
        message, exchange=exchange, routing_key="video", declare=[video_queue]
    )
