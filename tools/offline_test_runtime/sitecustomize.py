"""Only loaded by run_offline_tests.py; prevent test fixtures contacting providers."""
import socket

_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex


def connect(self, address):
    if isinstance(address, tuple) and address[0] not in ('localhost', '127.0.0.1', '::1'):
        raise OSError('External network disabled by offline test runner')
    return _connect(self, address)


def connect_ex(self, address):
    if isinstance(address, tuple) and address[0] not in ('localhost', '127.0.0.1', '::1'):
        raise OSError('External network disabled by offline test runner')
    return _connect_ex(self, address)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
