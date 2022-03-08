from queue import Queue
from kombu import Connection


kombu_connect = Connection("amqp://guest:guest@mister/")
