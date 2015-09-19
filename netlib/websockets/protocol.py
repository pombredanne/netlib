


# Colleciton of utility functions that implement small portions of the RFC6455
# WebSockets Protocol Useful for building WebSocket clients and servers.
#
# Emphassis is on readabilty, simplicity and modularity, not performance or
# completeness
#
# This is a work in progress and does not yet contain all the utilites need to
# create fully complient client/servers #
# Spec: https://tools.ietf.org/html/rfc6455

# The magic sha that websocket servers must know to prove they understand
# RFC6455
from __future__ import absolute_import
import base64
import hashlib
import os
import six
from ..http import Headers
from .. import utils

websockets_magic = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
VERSION = "13"

HEADER_WEBSOCKET_KEY = 'sec-websocket-key'
HEADER_WEBSOCKET_ACCEPT = 'sec-websocket-accept'
HEADER_WEBSOCKET_VERSION = 'sec-websocket-version'


class Masker(object):

    """
        Data sent from the server must be masked to prevent malicious clients
        from sending data over the wire in predictable patterns

        Servers do not have to mask data they send to the client.
        https://tools.ietf.org/html/rfc6455#section-5.3
    """

    def __init__(self, key):
        self.key = key
        self.masks = [six.byte2int(byte) for byte in key]
        self.offset = 0

    def mask(self, offset, data):
        result = ""
        for c in data:
            result += chr(ord(c) ^ self.masks[offset % 4])
            offset += 1
        return result

    def __call__(self, data):
        ret = self.mask(self.offset, data)
        self.offset += len(ret)
        return ret


class WebsocketsProtocol(object):

    def __init__(self):
        pass

    @classmethod
    def client_handshake_headers(self, key=None, version=VERSION):
        """
            Create the headers for a valid HTTP upgrade request. If Key is not
            specified, it is generated, and can be found in sec-websocket-key in
            the returned header set.

            Returns an instance of Headers
        """
        if not key:
            key = base64.b64encode(os.urandom(16)).decode('utf-8')
        return Headers([
            ('Connection', 'Upgrade'),
            ('Upgrade', 'websocket'),
            (HEADER_WEBSOCKET_KEY, key),
            (HEADER_WEBSOCKET_VERSION, version)
        ])

    @classmethod
    def server_handshake_headers(self, key):
        """
          The server response is a valid HTTP 101 response.
        """
        return Headers(
            [
                ('Connection', 'Upgrade'),
                ('Upgrade', 'websocket'),
                (HEADER_WEBSOCKET_ACCEPT, self.create_server_nonce(key))
            ]
        )


    @classmethod
    def check_client_handshake(self, headers):
        if headers.get("upgrade") != "websocket":
            return
        return headers.get(HEADER_WEBSOCKET_KEY)


    @classmethod
    def check_server_handshake(self, headers):
        if headers.get("upgrade") != "websocket":
            return
        return headers.get(HEADER_WEBSOCKET_ACCEPT)


    @classmethod
    def create_server_nonce(self, client_nonce):
        return base64.b64encode(
            hashlib.sha1(client_nonce + websockets_magic).hexdigest().decode('hex')
        )
