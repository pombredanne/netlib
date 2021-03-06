from __future__ import (absolute_import, print_function, division)
import itertools
import time

from hpack.hpack import Encoder, Decoder
from ... import utils
from .. import Headers, Response, Request, ALPN_PROTO_H2
from . import frame


class TCPHandler(object):

    def __init__(self, rfile, wfile=None):
        self.rfile = rfile
        self.wfile = wfile


class HTTP2Protocol(object):

    ERROR_CODES = utils.BiDi(
        NO_ERROR=0x0,
        PROTOCOL_ERROR=0x1,
        INTERNAL_ERROR=0x2,
        FLOW_CONTROL_ERROR=0x3,
        SETTINGS_TIMEOUT=0x4,
        STREAM_CLOSED=0x5,
        FRAME_SIZE_ERROR=0x6,
        REFUSED_STREAM=0x7,
        CANCEL=0x8,
        COMPRESSION_ERROR=0x9,
        CONNECT_ERROR=0xa,
        ENHANCE_YOUR_CALM=0xb,
        INADEQUATE_SECURITY=0xc,
        HTTP_1_1_REQUIRED=0xd
    )

    CLIENT_CONNECTION_PREFACE = "PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

    def __init__(
        self,
        tcp_handler=None,
        rfile=None,
        wfile=None,
        is_server=False,
        dump_frames=False,
        encoder=None,
        decoder=None,
        unhandled_frame_cb=None,
    ):
        self.tcp_handler = tcp_handler or TCPHandler(rfile, wfile)
        self.is_server = is_server
        self.dump_frames = dump_frames
        self.encoder = encoder or Encoder()
        self.decoder = decoder or Decoder()
        self.unhandled_frame_cb = unhandled_frame_cb

        self.http2_settings = frame.HTTP2_DEFAULT_SETTINGS.copy()
        self.current_stream_id = None
        self.connection_preface_performed = False

    def read_request(
        self,
        __rfile,
        include_body=True,
        body_size_limit=None,
        allow_empty=False,
    ):
        if body_size_limit is not None:
            raise NotImplementedError()

        self.perform_connection_preface()

        timestamp_start = time.time()
        if hasattr(self.tcp_handler.rfile, "reset_timestamps"):
            self.tcp_handler.rfile.reset_timestamps()

        stream_id, headers, body = self._receive_transmission(
            include_body=include_body,
        )

        if hasattr(self.tcp_handler.rfile, "first_byte_timestamp"):
            # more accurate timestamp_start
            timestamp_start = self.tcp_handler.rfile.first_byte_timestamp

        timestamp_end = time.time()

        authority = headers.get(':authority', '')
        method = headers.get(':method', 'GET')
        scheme = headers.get(':scheme', 'https')
        path = headers.get(':path', '/')
        host = None
        port = None

        if path == '*' or path.startswith("/"):
            form_in = "relative"
        elif method == 'CONNECT':
            form_in = "authority"
            if ":" in authority:
                host, port = authority.split(":", 1)
            else:
                host = authority
        else:
            form_in = "absolute"
            # FIXME: verify if path or :host contains what we need
            scheme, host, port, _ = utils.parse_url(path)

        if host is None:
            host = 'localhost'
        if port is None:
            port = 80 if scheme == 'http' else 443
        port = int(port)

        request = Request(
            form_in,
            method,
            scheme,
            host,
            port,
            path,
            (2, 0),
            headers,
            body,
            timestamp_start,
            timestamp_end,
        )
        # FIXME: We should not do this.
        request.stream_id = stream_id

        return request

    def read_response(
        self,
        __rfile,
        request_method='',
        body_size_limit=None,
        include_body=True,
        stream_id=None,
    ):
        if body_size_limit is not None:
            raise NotImplementedError()

        self.perform_connection_preface()

        timestamp_start = time.time()
        if hasattr(self.tcp_handler.rfile, "reset_timestamps"):
            self.tcp_handler.rfile.reset_timestamps()

        stream_id, headers, body = self._receive_transmission(
            stream_id=stream_id,
            include_body=include_body,
        )

        if hasattr(self.tcp_handler.rfile, "first_byte_timestamp"):
            # more accurate timestamp_start
            timestamp_start = self.tcp_handler.rfile.first_byte_timestamp

        if include_body:
            timestamp_end = time.time()
        else:
            timestamp_end = None

        response = Response(
            (2, 0),
            int(headers.get(':status', 502)),
            "",
            headers,
            body,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
        )
        response.stream_id = stream_id

        return response

    def assemble(self, message):
        if isinstance(message, Request):
            return self.assemble_request(message)
        elif isinstance(message, Response):
            return self.assemble_response(message)
        else:
            raise ValueError("HTTP message not supported.")

    def assemble_request(self, request):
        assert isinstance(request, Request)

        authority = self.tcp_handler.sni if self.tcp_handler.sni else self.tcp_handler.address.host
        if self.tcp_handler.address.port != 443:
            authority += ":%d" % self.tcp_handler.address.port

        headers = request.headers.copy()

        if ':authority' not in headers:
            headers.fields.insert(0, (':authority', bytes(authority)))
        if ':scheme' not in headers:
            headers.fields.insert(0, (':scheme', bytes(request.scheme)))
        if ':path' not in headers:
            headers.fields.insert(0, (':path', bytes(request.path)))
        if ':method' not in headers:
            headers.fields.insert(0, (':method', bytes(request.method)))

        if hasattr(request, 'stream_id'):
            stream_id = request.stream_id
        else:
            stream_id = self._next_stream_id()

        return list(itertools.chain(
            self._create_headers(headers, stream_id, end_stream=(request.body is None or len(request.body) == 0)),
            self._create_body(request.body, stream_id)))

    def assemble_response(self, response):
        assert isinstance(response, Response)

        headers = response.headers.copy()

        if ':status' not in headers:
            headers.fields.insert(0, (':status', bytes(str(response.status_code))))

        if hasattr(response, 'stream_id'):
            stream_id = response.stream_id
        else:
            stream_id = self._next_stream_id()

        return list(itertools.chain(
            self._create_headers(headers, stream_id, end_stream=(response.body is None or len(response.body) == 0)),
            self._create_body(response.body, stream_id),
        ))

    def perform_connection_preface(self, force=False):
        if force or not self.connection_preface_performed:
            if self.is_server:
                self.perform_server_connection_preface(force)
            else:
                self.perform_client_connection_preface(force)

    def perform_server_connection_preface(self, force=False):
        if force or not self.connection_preface_performed:
            self.connection_preface_performed = True

            magic_length = len(self.CLIENT_CONNECTION_PREFACE)
            magic = self.tcp_handler.rfile.safe_read(magic_length)
            assert magic == self.CLIENT_CONNECTION_PREFACE

            frm = frame.SettingsFrame(state=self, settings={
                frame.SettingsFrame.SETTINGS.SETTINGS_ENABLE_PUSH: 0,
                frame.SettingsFrame.SETTINGS.SETTINGS_MAX_CONCURRENT_STREAMS: 1,
            })
            self.send_frame(frm, hide=True)
            self._receive_settings(hide=True)

    def perform_client_connection_preface(self, force=False):
        if force or not self.connection_preface_performed:
            self.connection_preface_performed = True

            self.tcp_handler.wfile.write(self.CLIENT_CONNECTION_PREFACE)

            self.send_frame(frame.SettingsFrame(state=self), hide=True)
            self._receive_settings(hide=True)  # server announces own settings
            self._receive_settings(hide=True)  # server acks my settings

    def send_frame(self, frm, hide=False):
        raw_bytes = frm.to_bytes()
        self.tcp_handler.wfile.write(raw_bytes)
        self.tcp_handler.wfile.flush()
        if not hide and self.dump_frames:  # pragma no cover
            print(frm.human_readable(">>"))

    def read_frame(self, hide=False):
        while True:
            frm = frame.Frame.from_file(self.tcp_handler.rfile, self)
            if not hide and self.dump_frames:  # pragma no cover
                print(frm.human_readable("<<"))

            if isinstance(frm, frame.PingFrame):
                raw_bytes = frame.PingFrame(flags=frame.Frame.FLAG_ACK, payload=frm.payload).to_bytes()
                self.tcp_handler.wfile.write(raw_bytes)
                self.tcp_handler.wfile.flush()
                continue
            if isinstance(frm, frame.SettingsFrame) and not frm.flags & frame.Frame.FLAG_ACK:
                self._apply_settings(frm.settings, hide)
            if isinstance(frm, frame.DataFrame) and frm.length > 0:
                self._update_flow_control_window(frm.stream_id, frm.length)
            return frm

    def check_alpn(self):
        alp = self.tcp_handler.get_alpn_proto_negotiated()
        if alp != ALPN_PROTO_H2:
            raise NotImplementedError(
                "HTTP2Protocol can not handle unknown ALP: %s" % alp)
        return True

    def _handle_unexpected_frame(self, frm):
        if isinstance(frm, frame.SettingsFrame):
            return
        if self.unhandled_frame_cb:
            self.unhandled_frame_cb(frm)

    def _receive_settings(self, hide=False):
        while True:
            frm = self.read_frame(hide)
            if isinstance(frm, frame.SettingsFrame):
                break
            else:
                self._handle_unexpected_frame(frm)

    def _next_stream_id(self):
        if self.current_stream_id is None:
            if self.is_server:
                # servers must use even stream ids
                self.current_stream_id = 2
            else:
                # clients must use odd stream ids
                self.current_stream_id = 1
        else:
            self.current_stream_id += 2
        return self.current_stream_id

    def _apply_settings(self, settings, hide=False):
        for setting, value in settings.items():
            old_value = self.http2_settings[setting]
            if not old_value:
                old_value = '-'
            self.http2_settings[setting] = value

        frm = frame.SettingsFrame(
            state=self,
            flags=frame.Frame.FLAG_ACK)
        self.send_frame(frm, hide)

    def _update_flow_control_window(self, stream_id, increment):
        frm = frame.WindowUpdateFrame(stream_id=0, window_size_increment=increment)
        self.send_frame(frm)
        frm = frame.WindowUpdateFrame(stream_id=stream_id, window_size_increment=increment)
        self.send_frame(frm)

    def _create_headers(self, headers, stream_id, end_stream=True):
        def frame_cls(chunks):
            for i in chunks:
                if i == 0:
                    yield frame.HeadersFrame, i
                else:
                    yield frame.ContinuationFrame, i

        header_block_fragment = self.encoder.encode(headers.fields)

        chunk_size = self.http2_settings[frame.SettingsFrame.SETTINGS.SETTINGS_MAX_FRAME_SIZE]
        chunks = range(0, len(header_block_fragment), chunk_size)
        frms = [frm_cls(
            state=self,
            flags=frame.Frame.FLAG_NO_FLAGS,
            stream_id=stream_id,
            header_block_fragment=header_block_fragment[i:i+chunk_size]) for frm_cls, i in frame_cls(chunks)]

        last_flags = frame.Frame.FLAG_END_HEADERS
        if end_stream:
            last_flags |= frame.Frame.FLAG_END_STREAM
        frms[-1].flags = last_flags

        if self.dump_frames:  # pragma no cover
            for frm in frms:
                print(frm.human_readable(">>"))

        return [frm.to_bytes() for frm in frms]

    def _create_body(self, body, stream_id):
        if body is None or len(body) == 0:
            return b''

        chunk_size = self.http2_settings[frame.SettingsFrame.SETTINGS.SETTINGS_MAX_FRAME_SIZE]
        chunks = range(0, len(body), chunk_size)
        frms = [frame.DataFrame(
            state=self,
            flags=frame.Frame.FLAG_NO_FLAGS,
            stream_id=stream_id,
            payload=body[i:i+chunk_size]) for i in chunks]
        frms[-1].flags = frame.Frame.FLAG_END_STREAM

        if self.dump_frames:  # pragma no cover
            for frm in frms:
                print(frm.human_readable(">>"))

        return [frm.to_bytes() for frm in frms]

    def _receive_transmission(self, stream_id=None, include_body=True):
        if not include_body:
            raise NotImplementedError()

        body_expected = True

        header_block_fragment = b''
        body = b''

        while True:
            frm = self.read_frame()
            if (
                (isinstance(frm, frame.HeadersFrame) or isinstance(frm, frame.ContinuationFrame)) and
                (stream_id is None or frm.stream_id == stream_id)
            ):
                stream_id = frm.stream_id
                header_block_fragment += frm.header_block_fragment
                if frm.flags & frame.Frame.FLAG_END_STREAM:
                    body_expected = False
                if frm.flags & frame.Frame.FLAG_END_HEADERS:
                    break
            else:
                self._handle_unexpected_frame(frm)

        while body_expected:
            frm = self.read_frame()
            if isinstance(frm, frame.DataFrame) and frm.stream_id == stream_id:
                body += frm.payload
                if frm.flags & frame.Frame.FLAG_END_STREAM:
                    break
            else:
                self._handle_unexpected_frame(frm)

        headers = Headers(
            [[str(k), str(v)] for k, v in self.decoder.decode(header_block_fragment)]
        )

        return stream_id, headers, body
