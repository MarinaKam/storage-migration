"""Run fixture-based tests; reject real socket connections."""

import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


def blocked(*args, **kwargs):
    raise AssertionError("Network connections are forbidden in offline tests")


def main():
    directory = Path(__file__).resolve().parent
    with (
        patch.object(socket, "create_connection", blocked),
        patch.object(socket.socket, "connect", blocked),
        patch.object(socket.socket, "connect_ex", blocked),
        patch.object(socket, "getaddrinfo", blocked),
    ):
        suite = unittest.defaultTestLoader.discover(str(directory), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
