"""Reserve this run's published host ports before anything else is imported.

The dokker stack publishes Postgres and Redis on fixed host ports, which collide
with any other stack on the machine publishing the same numbers -- several
sibling repos publish 5555/6666 from a compose project that, like this one, is
named after its ``integration`` directory. A collision surfaces as a dokker
CommandError from ``backend_stack`` and takes out every database-backed test.

This lives in the root conftest because ``rekuest.settings_test`` reads the
ports at import time, which happens when pytest-django configures Django --
before any fixture, and before ``tests/conftest.py`` is imported.
"""

import os
import socket

_DB_PORT_VAR = "REKUEST_TEST_DB_PORT"
_REDIS_PORT_VAR = "REKUEST_TEST_REDIS_PORT"
_TAKT_PORT_VAR = "REKUEST_TEST_TAKT_PORT"


def _reserve_free_ports(count: int) -> list[int]:
    """Ask the OS for `count` distinct free TCP ports.

    Every socket is held open until all have been assigned, so the kernel cannot
    hand out the same port twice within one call.
    """
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [int(sock.getsockname()[1]) for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


# An explicit port from the environment always wins, so a developer can still
# point the suite at a stack they brought up themselves.
if _DB_PORT_VAR not in os.environ or _REDIS_PORT_VAR not in os.environ or _TAKT_PORT_VAR not in os.environ:
    _db_port, _redis_port, _takt_port = _reserve_free_ports(3)
    os.environ.setdefault(_DB_PORT_VAR, str(_db_port))
    os.environ.setdefault(_REDIS_PORT_VAR, str(_redis_port))
    os.environ.setdefault(_TAKT_PORT_VAR, str(_takt_port))
