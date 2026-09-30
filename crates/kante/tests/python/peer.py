"""The Python side of kante's contract tests: the real channels_redis layer.

    peer.py receive <redis_url> <prefix> <group>          prints READY, then the message as JSON
    peer.py send    <redis_url> <prefix> <group> <json>   group_sends the message
"""

import asyncio
import json
import sys

from channels_redis.core import RedisChannelLayer


async def main() -> None:
    mode, url, prefix, group = sys.argv[1:5]
    layer = RedisChannelLayer(hosts=[url], prefix=prefix, capacity=5000)
    if mode == "receive":
        channel = await layer.new_channel()
        await layer.group_add(group, channel)
        print("READY", flush=True)
        message = await asyncio.wait_for(layer.receive(channel), 20)
        print(json.dumps(message), flush=True)
        await layer.group_discard(group, channel)
    else:
        await layer.group_send(group, json.loads(sys.argv[5]))
    await layer.close_pools()


asyncio.run(main())
