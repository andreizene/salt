# -*- coding: utf-8 -*-
"""
HTTP transport classes
"""

# Import Python Libs
from __future__ import absolute_import, print_function, unicode_literals

import json
import base64
import logging
import socket
import time
import uuid

# Import Tornado Libs
import salt.ext.tornado
import salt.ext.tornado.web
import salt.ext.tornado.locks
import salt.ext.tornado.gen
import salt.ext.tornado.httpclient
import salt.ext.tornado.httpserver
import salt.transport.frame
import salt.transport.abstract
import salt.utils.msgpack
from salt.transport.tcp import _set_tcp_keepalive
from salt.ext import six

log = logging.getLogger(__name__)

if six.PY2:
    import urllib
    urlencode = urllib.urlencode
else:
    import urllib.parse
    urlencode = urllib.parse.urlencode

# pylint: disable=import-error,no-name-in-module
if six.PY2:
    import urlparse
else:
    import urllib.parse as urlparse
# pylint: enable=import-error,no-name-in-module

class BytesDump(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, bytes):                   # deal with bytes
            return None 
        return json.JSONEncoder.default(self, obj)   # everything else

class AsyncHTTPReqChannel(salt.transport.abstract.AbstractAsyncReqChannel):
    def __new__(cls, opts, **kwargs):
        kwargs["crypt"] = "clear"
        return super().__new__(cls, opts, **kwargs)

    @classmethod
    def __key(cls, opts, **kwargs):
        return super().__key(cls, opts, **kwargs)

    def start_channel(self, io_loop, **kwargs):
        publish_port = self.opts.get("publish_port", 4505)
        master = self.opts["master"]
        # TODO: Initial request should just fetch last_message_id?
        self.url = "http://" + master + ":" + str(publish_port) + "/req"
        self.http_client = salt.ext.tornado.httpclient.AsyncHTTPClient(io_loop=self.io_loop)

    def close(self):
        super().close()
        if hasattr(self, "http_client"):
            self.http_client.close()

    def publish_dict(self, dicty, tries=3, timeout=60):
        json_payload = json.dumps(dicty, cls=BytesDump)
        return self.publish_string(json_payload)

    @salt.ext.tornado.gen.coroutine
    def _uncrypted_transfer(self, load, tries=3, timeout=60):
        ret = yield self.publish_dict(
            self._package_load(load), timeout=timeout
        )
        raise salt.ext.tornado.gen.Return(ret)

    @salt.ext.tornado.gen.coroutine
    def crypted_transfer_decode_dictentry(
        self, load, dictkey=None, tries=3, timeout=60
    ):
        ret = yield self.publish_dict(
            self._package_load(json.dumps(load, cls=BytesDump)), timeout=timeout
        )        
        #data = json.loads()[dictKey]
        data = {}
        raise salt.ext.tornado.gen.Return(data)

    @salt.ext.tornado.gen.coroutine
    def send(self, load, tries=3, timeout=60, raw=False):
        """
        Send a request, return a future which will complete when we send the message
        """
        try:
            ret = yield self._uncrypted_transfer(load, tries=tries, timeout=timeout)
        except salt.ext.tornado.iostream.StreamClosedError:
            # Convert to 'SaltClientError' so that clients can handle this
            # exception more appropriately.
            raise SaltClientError("Connection to master lost")
        raise salt.ext.tornado.gen.Return(ret)

    def publish_string(self, spayload):
        headers = {"x-ni-api-key": self.opts["x-ni-api-key"]}
        http_request = salt.ext.tornado.httpclient.HTTPRequest(self.url, method="POST", headers=headers, body=spayload)

        return_future = salt.ext.tornado.gen.Future()
        def callback(response):
            if response.error:
                # TODO: Deal with errors
                print("!!", __class__, "Error:", response.error)
            else:
                payload = response.body
                return_future.set_result(payload)
                return
            return_future.set_result(None)
        self.http_client.fetch(http_request, callback)

        return return_future


class MessageRequestHandler(salt.ext.tornado.web.RequestHandler):
    """Long-polling request for new messages.
    Waits until new messages are available before returning anything.
    """

    def initialize(self, callback):
        self.callback = callback

    async def post(self):
        spayload = self.get_argument("payload")
        for framed_msg in spayload:
            self.callback(None, framed_msg, handler=self)

    def on_connection_close(self):
        self.wait_future.cancel()


class HTTPReqServerChannel(salt.transport.abstract.AbstractReqServerChannel):
    # TODO: opts!
    backlog = 5

    def __init__(self, opts):
        super().__init__(opts)
        self.xsocket = None

    @property
    def socket(self):
        return self.xsocket

    def close(self):
        super().close()
        if self.xsocket is not None:
            try:
                self.xsocket.shutdown(socket.SHUT_RDWR)
            except socket.error as exc:
                if exc.errno == errno.ENOTCONN:
                    # We may try to shutdown a socket which is already disconnected.
                    # Ignore this condition and continue.
                    pass
                else:
                    six.reraise(*sys.exc_info())
            self.xsocket.close()
            self.xsocket = None
        if hasattr(self.http_server, "shutdown"):
            try:
                self.http_server.shutdown()
            except Exception as exc:  # pylint: disable=broad-except
                log.exception(
                    "HTTPReqServerChannel close generated an exception: %s", str(exc)
                )
        elif hasattr(self.http_server, "stop"):
            self.http_server.stop()

    def _start_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _set_tcp_keepalive(sock, self.opts)
        sock.setblocking(0)
        sock.bind((self.opts["interface"], int(self.opts["ret_port"])))
        return sock

    def pre_fork(self, process_manager):
        super().pre_fork(process_manager)
        self.xsocket = self._start_socket()

    def start_channel(self, io_loop):
        """Start channel for minions to connect to.

        Whenever a message is received process_message should be called with
        the decoded message.
        """
        with salt.utils.asynchronous.current_ioloop(io_loop):
            app = salt.ext.tornado.web.Application(
                [
                    (r"/req", MessageRequestHandler, {'callback': self.process_message}),
                ],
            )
            self.http_server = salt.ext.tornado.httpserver.HTTPServer(app, io_loop=self.io_loop)
            self.http_server.add_socket(self.xsocket)
            self.xsocket.listen(self.backlog)

    def write_string(self, spayload, handler):
        """Send bytes back to minion as response.

        The kwargs provided to this function are the same start_channel passes
        to process_message to process receieved messages.

        This must implemented assuming the write_bytes method is not.
        """
        handler.write(spayload)

    def shutdown_processor(self, handler):
        """Shutdown the specific minion response channel.

        The kwargs provided to this function are the same start_channel passes
        to process_message to process receieved messages.
        """
        handler.close()


class AsyncHTTPPubChannel(salt.transport.abstract.AbstractAsyncPubChannel):
    # TODO: Handle minion connection state
    def __init__(self, opts, **kwargs):
        super().__init__(opts, **kwargs)
        self.callback = None
        self.auth = type('', (), {})()
        self.auth.gen_token = self.gen_token

    def close(self):
        super().close()
        if hasattr(self, "http_client"):
            self.http_client.close()

    @salt.ext.tornado.gen.coroutine
    def open_connection(self):
        # if this is changed from the default, we assume it was intentional
        if int(self.opts.get("publish_port", 4505)) != 4505:
            self.publish_port = self.opts.get("publish_port")
        # else take the relayed publish_port master reports
        else:
            self.publish_port = self.auth.creds["publish_port"]
        self.publish_ip = self.opts["master_ip"]
        # TODO: Initial request should just fetch last_message_id?
        self.url = "http://" + self.publish_ip + ":" + str(self.publish_port) + "/message/updates"
        self._fire_http_request()

    def gen_token(self, clear_tok):
        """
        Encrypt a string with the minion private key to verify identity
        with the master.

        :param str clear_tok: A plaintext token to encrypt
        :return: Encrypted token
        :rtype: str
        """
        return clear_tok

    @salt.ext.tornado.gen.coroutine
    def connect(self):
        try:
            yield self.open_connection()
            self.connected = True
        # TODO: better exception handling...
        except KeyboardInterrupt:  # pylint: disable=try-except-raise
            raise
        except Exception as exc:  # pylint: disable=broad-except
            if "-|RETRY|-" not in six.text_type(exc):
                raise SaltClientError(
                    "Unable to sign_in to master: {0}".format(exc)
                )  # TODO: better error message

    def _fire_http_request(self, cursor=None):
        post_data = {}
        if cursor:
            post_data["cursor"] = cursor
        body = urlencode(post_data)
        headers = {"x-ni-api-key": self.opts["x-ni-api-key"]}
        http_request = salt.ext.tornado.httpclient.HTTPRequest(self.url, method="POST", headers=headers, body=body)
        self.http_client = salt.ext.tornado.httpclient.AsyncHTTPClient(io_loop=self.io_loop)
        time.sleep(5)
        self.http_client.fetch(http_request, self._handle_response)

    def _handle_response(self, response):
        if response.error:
            # TODO: Deal with errors
            print("!!", __class__, "Error:", response.error)
            if response.code == 599:
                # Fire another long-polling request
                self._fire_http_request()
        else:
            message = json.loads(response.body)
            message["enc"] = "aes" # minion doesn't run jobs if enc is not aes so hack it to be able to run jobs
            if self.callback:
                self.io_loop.spawn_callback(self.callback, message)
            # Fire another long-polling request
            self._fire_http_request()

    def set_callback(self, callback):
        self.callback = callback

    def on_recv(self, callback):
        """
        Register an on_recv callback
        """
        if callback is None:
            return self.set_callback(None)

        return self.set_callback(callback)


class MessageBuffer(object):
    def __init__(self):
        # cond is notified whenever the message cache is updated
        self.cond = salt.ext.tornado.locks.Condition()
        self.cache = []
        self.cache_size = 200

    def get_messages_since(self, cursor):
        """Returns a list of messages newer than the given cursor.S
        ``cursor`` should be the ``id`` of the last message received.
        """
        results = []
        for msg in reversed(self.cache):
            message_id = msg["_id"]
            if message_id == cursor:
                break
            results.append(msg)
        results.reverse()
        return results

    def add_message(self, message):
        self.cache.append({"_id": str(uuid.uuid4()), "payload": message})
        if len(self.cache) > self.cache_size:
            self.cache = self.cache[-self.cache_size :]
        self.cond.notify_all()


class MessageUpdatesHandler(salt.ext.tornado.web.RequestHandler):
    """Long-polling request for new messages.
    Waits until new messages are available before returning anything.
    """

    def initialize(self, message_buffer):
        self.message_buffer = message_buffer

    async def post(self):
        cursor = self.get_argument("cursor", None)
        messages = self.message_buffer.get_messages_since(cursor)
        while not messages:
            # Save the Future returned here so we can cancel it in
            # on_connection_close.
            self.wait_future = self.message_buffer.cond.wait()
            try:
                await self.wait_future
            except asyncio.CancelledError:
                return
            messages = self.message_buffer.get_messages_since(cursor)
        if self.request.connection.stream.closed():
            return
        self.write(dict(messages=messages))

    def on_connection_close(self):
        self.wait_future.cancel()


class HTTPPubServerChannel(salt.transport.abstract.AbstractPubServerChannel):
    # TODO: opts!
    # Based on default used in salt.ext.tornado.netutil.bind_sockets()
    backlog = 128

    def close(self):
        super().close()
        if self.http_server:
            self.http_server.close()
            self.http_server = None

    def _start_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _set_tcp_keepalive(sock, self.opts)
        sock.setblocking(0)
        sock.bind((self.opts["interface"], int(self.opts["publish_port"])))
        sock.listen(self.backlog)
        return sock

    def start_channel(self, io_loop):
        self.message_buffer = MessageBuffer()
        app = salt.ext.tornado.web.Application(
            [
                (r"/message/updates", MessageUpdatesHandler, {"message_buffer": self.message_buffer}),
            ],
        )
        # Spin up the publisher
        self.http_server = salt.ext.tornado.httpserver.HTTPServer(app, io_loop=io_loop)
        self.http_server.add_socket(self._start_socket())

    def publish_string(self, spayload):
        # Add the message to our buffer, triggering every minions long-polling
        # HTTP call to get replied to.
        self.message_buffer.add_message(spayload)
