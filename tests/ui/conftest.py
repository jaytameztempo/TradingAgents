"""UI tests keep the suite's no-network rule, except for loopback.

On Windows, the event loop behind FastAPI's TestClient connects a socket pair
over 127.0.0.1; everything else is still refused, as in tests/conftest.py.
"""

import socket

import pytest

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        return
    connect, connect_ex, getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def _host(address):
        return address[0] if isinstance(address, tuple) else address

    def guarded(original):
        def call(sock, address, *args, **kwargs):
            if _host(address) not in _LOOPBACK:
                raise OSError(f"test tried to reach the network: {address}")
            return original(sock, address, *args, **kwargs)
        return call

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in _LOOPBACK:
            raise OSError(f"test tried to reach the network: {host}")
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded(connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(connect_ex))
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
