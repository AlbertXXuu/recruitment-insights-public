"""SOCKS and relay regression tests using synthetic bytes and loopback sockets only."""
import ipaddress
import socket
import threading
import time
import unittest
from unittest.mock import patch

from scripts import tunnel_proxy as proxy


def receive_exact(stream, length):
    data = bytearray()
    while len(data) < length:
        chunk = stream.recv(length - len(data))
        if not chunk:
            raise EOFError('fixture connection closed')
        data.extend(chunk)
    return bytes(data)


class ScriptedStream:
    """Serve one-byte fragments to exercise protocol framing, including EOF."""
    def __init__(self, incoming):
        self.incoming = bytearray(incoming)
        self.sent = []
        self.closed = False
        self.timeout = 5

    def recv(self, size):
        chunk = bytes(self.incoming[:min(size, 1)])
        del self.incoming[:len(chunk)]
        return chunk

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


class SocksHandshakeTests(unittest.TestCase):
    def test_fragmented_success_accepts_all_three_bound_address_types(self):
        replies = (
            b'\x05\x00\x00\x01\x7f\x00\x00\x01\x12\x34',
            b'\x05\x00\x00\x04' + bytes(15) + b'\x01\x12\x34',
            b'\x05\x00\x00\x03\x07fixture\x12\x34',
        )
        request = b'\x05\x01\x00\x01' + ipaddress.IPv4Address('203.0.113.9').packed
        request += proxy.EDGE_PORT.to_bytes(2, 'big')
        for reply in replies:
            with self.subTest(address_type=reply[3]):
                stream = ScriptedStream(b'\x05\x00' + reply)
                with patch.object(proxy.socket, 'create_connection', return_value=stream) as connect:
                    self.assertIs(proxy.connect_socks(1080, '203.0.113.9'), stream)
                connect.assert_called_once_with(('127.0.0.1', 1080), timeout=5)
                self.assertEqual(stream.sent, [b'\x05\x01\x00', request])
                self.assertFalse(stream.incoming)
                self.assertFalse(stream.closed)
                self.assertIsNone(stream.timeout)

    def test_rejected_authentication_reply_or_truncated_frame_closes_socket(self):
        invalid = {
            'authentication required': b'\x05\x02',
            'authentication refused': b'\x05\xff',
            'wrong greeting version': b'\x04\x00',
            'EOF in greeting': b'\x05',
            'EOF in reply header': b'\x05\x00\x05\x00',
            'connection rejected': b'\x05\x00\x05\x05\x00\x01',
            'wrong reply version': b'\x05\x00\x04\x00\x00\x01',
            'nonzero reserved byte': b'\x05\x00\x05\x00\x01\x01',
            'unknown address type': b'\x05\x00\x05\x00\x00\xff',
            'EOF in IPv4 address': b'\x05\x00\x05\x00\x00\x01\x7f',
            'EOF in IPv6 address': b'\x05\x00\x05\x00\x00\x04' + bytes(17),
            'EOF in domain length': b'\x05\x00\x05\x00\x00\x03',
            'EOF in domain or port': b'\x05\x00\x05\x00\x00\x03\x07fixture\x12',
        }
        for reason, incoming in invalid.items():
            with self.subTest(reason=reason):
                stream = ScriptedStream(incoming)
                with patch.object(proxy.socket, 'create_connection', return_value=stream):
                    with self.assertRaises(OSError):
                        proxy.connect_socks(1080, '203.0.113.9')
                self.assertTrue(stream.closed)
                self.assertEqual(stream.timeout, 5)
                if 'authentication' in reason or 'greeting' in reason:
                    self.assertEqual(stream.sent, [b'\x05\x01\x00'])

    def test_invalid_destination_is_rejected_before_opening_a_socket(self):
        for address in ('example.invalid', '::1', '256.1.2.3', ''):
            with self.subTest(address=address), patch.object(proxy.socket, 'create_connection') as connect:
                with self.assertRaises(ValueError):
                    proxy.connect_socks(1080, address)
                connect.assert_not_called()

    def test_interrupted_handshake_closes_socket(self):
        stream = ScriptedStream(b'')
        with patch.object(proxy.socket, 'create_connection', return_value=stream), \
                patch.object(stream, 'sendall', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                proxy.connect_socks(1080, '203.0.113.9')
        self.assertTrue(stream.closed)

    def test_owner_receives_socket_before_handshake_and_callback_failure_closes_it(self):
        reply = b'\x05\x00\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x01'
        stream = ScriptedStream(reply)
        opened = []

        def on_open(value):
            self.assertIs(value, stream)
            self.assertFalse(value.sent, 'owner cannot cancel a handshake already in progress')
            opened.append(value)

        with patch.object(proxy.socket, 'create_connection', return_value=stream):
            self.assertIs(proxy.connect_socks(1080, '203.0.113.9', on_open=on_open), stream)
        self.assertEqual(opened, [stream])

        def stopped_owner(_):
            raise RuntimeError('fixture owner stopped')

        failed = ScriptedStream(reply)
        with patch.object(proxy.socket, 'create_connection', return_value=failed):
            with self.assertRaisesRegex(RuntimeError, 'fixture owner stopped'):
                proxy.connect_socks(1080, '203.0.113.9', on_open=stopped_owner)
        self.assertTrue(failed.closed)
        self.assertFalse(failed.sent)


class LoopbackSocksServer:
    """A local SOCKS server that never opens an upstream connection."""
    BANNER = b'\x16\x03\x03\x00\x09\x00\xffloopback'

    def __init__(self, *, half_close_response=False, stall_handshake=False,
                 reject_handshake=False):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(8)
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.streams = set()
        self.workers = []
        self.requests = []
        self.errors = []
        self.closed_connections = threading.Event()
        self.handshake_received = threading.Event()
        self.half_close_response = half_close_response
        self.stall_handshake = stall_handshake
        self.reject_handshake = reject_handshake
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stopped.is_set():
            try:
                stream, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self.lock:
                self.streams.add(stream)
            worker = threading.Thread(target=self.serve, args=(stream,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def serve(self, stream):
        try:
            stream.settimeout(3)
            if receive_exact(stream, 3) != b'\x05\x01\x00':
                raise AssertionError('unexpected SOCKS greeting')
            self.handshake_received.set()
            if self.stall_handshake:
                if stream.recv(1):
                    raise AssertionError('client continued a stalled SOCKS handshake')
                return
            if self.reject_handshake:
                stream.sendall(b'\x05\xff')
                return
            stream.sendall(b'\x05')
            stream.sendall(b'\x00')
            request = receive_exact(stream, 10)
            expected = b'\x05\x01\x00\x01\x7f\x00\x00\x01'
            expected += proxy.EDGE_PORT.to_bytes(2, 'big')
            if request != expected:
                raise AssertionError('relay requested a non-fixture destination')
            self.requests.append(request)
            reply = b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x01'
            for byte in reply:
                stream.sendall(bytes([byte]))
            stream.sendall(self.BANNER)
            if self.half_close_response:
                chunks = []
                while True:
                    chunk = stream.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                stream.sendall(b'reply after EOF:' + b''.join(chunks))
                return
            while not self.stopped.is_set():
                chunk = stream.recv(4096)
                if not chunk:
                    return
                stream.sendall(chunk)
        except (ConnectionResetError, ConnectionAbortedError):
            pass
        except BaseException as error:
            if not self.stopped.is_set():
                self.errors.append(error)
        finally:
            stream.close()
            with self.lock:
                self.streams.discard(stream)
            self.closed_connections.set()

    def stop(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(2)
        with self.lock:
            streams = list(self.streams)
        for stream in streams:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()
        for worker in self.workers:
            worker.join(2)


class LoopbackEdgeProxyTests(unittest.TestCase):
    def setUp(self):
        self.original_dns = socket.getaddrinfo
        self.dns_requests = []

        def resolve(host, port, family=0, kind=0, *args, **kwargs):
            if host in proxy.EDGE_HOSTS:
                self.dns_requests.append((host, port, family, kind))
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
                         ('127.0.0.1', proxy.EDGE_PORT))]
            if host != '127.0.0.1':
                raise AssertionError('test attempted a non-loopback DNS lookup')
            return self.original_dns(host, port, family, kind, *args, **kwargs)

        dns = patch.object(proxy.socket, 'getaddrinfo', side_effect=resolve)
        dns.start()
        self.addCleanup(dns.stop)

    def start_relay(self, **server_options):
        server = LoopbackSocksServer(**server_options)
        self.addCleanup(server.stop)
        relay = proxy.LoopbackEdgeProxy(server.port)
        self.addCleanup(relay.stop)
        edges = relay.start()
        self.assertEqual(len(edges), 2)
        self.assertEqual(len(set(edges)), 2)
        self.assertEqual(self.dns_requests, [
            (host, proxy.EDGE_PORT, socket.AF_INET, socket.SOCK_STREAM)
            for host in proxy.EDGE_HOSTS])
        for edge in edges:
            self.assertTrue(edge.startswith('127.0.0.1:'))
        return relay, server, edges

    def connect_edge(self, edge):
        host, port = edge.split(':')
        stream = socket.create_connection((host, int(port)), timeout=3)
        self.addCleanup(stream.close)
        self.assertEqual(receive_exact(stream, len(LoopbackSocksServer.BANNER)),
                         LoopbackSocksServer.BANNER)
        return stream

    def wait_for(self, predicate, description):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(predicate(), description)

    def test_two_relays_preserve_binary_bytes_in_both_directions(self):
        relay, server, edges = self.start_relay()
        payload = bytes(range(256)) * 2049
        for edge in edges:
            client = self.connect_edge(edge)
            failures = []

            def send():
                try:
                    client.sendall(payload)
                except BaseException as error:
                    failures.append(error)

            sender = threading.Thread(target=send, daemon=True)
            sender.start()
            try:
                self.assertEqual(receive_exact(client, len(payload)), payload)
            finally:
                client.close()
                sender.join(3)
            self.assertFalse(sender.is_alive(), 'loopback sender did not finish')
            self.assertFalse(failures)
        self.wait_for(lambda: not relay._sockets, 'disconnected relay sockets were retained')
        self.wait_for(lambda: not server.streams, 'disconnected SOCKS peers were retained')
        self.assertEqual(len(server.requests), 2)
        self.assertFalse(server.errors)

    def test_stop_disconnects_clients_releases_listeners_and_joins_workers(self):
        relay, server, edges = self.start_relay()
        clients = [self.connect_edge(edge) for edge in edges]
        listeners = list(relay._listeners)
        workers = list(relay._threads)
        relay.stop()
        self.assertTrue(all(listener.fileno() == -1 for listener in listeners))
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertFalse(relay._listeners)
        self.assertFalse(relay._threads)
        self.assertFalse(relay._sockets)
        self.assertFalse(relay.edges)
        for client in clients:
            try:
                remaining = client.recv(1)
            except (ConnectionResetError, ConnectionAbortedError):
                remaining = b''
            self.assertEqual(remaining, b'')
        for edge in edges:
            host, port = edge.split(':')
            with self.assertRaises(OSError):
                connection = socket.create_connection((host, int(port)), timeout=0.3)
                connection.close()
        relay.stop()
        self.wait_for(lambda: not server.streams, 'SOCKS peers remained open after stop')
        self.assertFalse(server.errors)

    def test_rejected_socks_handshake_releases_all_tracked_sockets(self):
        relay, server, edges = self.start_relay(reject_handshake=True)
        host, port = edges[0].split(':')
        client = socket.create_connection((host, int(port)), timeout=3)
        self.addCleanup(client.close)
        self.assertTrue(server.handshake_received.wait(3))
        try:
            received = client.recv(1)
        except (ConnectionResetError, ConnectionAbortedError):
            received = b''
        self.assertEqual(received, b'')
        self.wait_for(lambda: len(relay._threads) == 2, 'rejected handshake worker was retained')
        self.assertFalse(relay._sockets, 'closed SOCKS handshake socket remained tracked')
        self.assertFalse(server.errors)

    def test_half_closed_client_receives_response_written_after_its_eof(self):
        relay, server, edges = self.start_relay(half_close_response=True)
        client = self.connect_edge(edges[0])
        payload = bytes(range(256)) * 129
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        expected = b'reply after EOF:' + payload
        self.assertEqual(receive_exact(client, len(expected)), expected)
        self.assertEqual(client.recv(1), b'')
        self.wait_for(lambda: not relay._sockets, 'half-closed connection was retained')
        self.assertFalse(server.errors)

    def test_stop_interrupts_pending_socks_handshake(self):
        relay, server, edges = self.start_relay(stall_handshake=True)
        host, port = edges[0].split(':')
        client = socket.create_connection((host, int(port)), timeout=3)
        self.addCleanup(client.close)
        self.assertTrue(server.handshake_received.wait(3), 'SOCKS handshake never reached fixture')
        workers = list(relay._threads)
        finished = threading.Event()

        def stop():
            try:
                relay.stop()
            finally:
                finished.set()

        stopper = threading.Thread(target=stop, daemon=True)
        stopper.start()
        try:
            self.assertTrue(finished.wait(2), 'stop left SOCKS handshake blocked')
        finally:
            stopper.join(6)
        self.assertFalse(stopper.is_alive())
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertFalse(relay._sockets)
        self.wait_for(lambda: not server.streams, 'SOCKS handshake socket remained open')
        self.assertFalse(server.errors)

    def test_start_failure_after_first_listener_releases_partial_start(self):
        relay = proxy.LoopbackEdgeProxy(1080)
        self.addCleanup(relay.stop)
        listeners, workers = [], []

        def resolve(host, *_args):
            if host == proxy.EDGE_HOSTS[0]:
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
                         ('127.0.0.1', proxy.EDGE_PORT))]
            listeners.extend(relay._listeners)
            workers.extend(relay._threads)
            raise OSError('fixture DNS failure')

        with patch.object(proxy.socket, 'getaddrinfo', side_effect=resolve):
            with self.assertRaisesRegex(OSError, 'fixture DNS failure'):
                relay.start()
        self.assertEqual(len(listeners), 1)
        self.assertTrue(all(listener.fileno() == -1 for listener in listeners))
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertFalse(relay._listeners)
        self.assertFalse(relay._threads)
        self.assertFalse(relay._sockets)
        self.assertFalse(relay.edges)

    def test_new_listener_is_closed_when_bind_fails(self):
        relay = proxy.LoopbackEdgeProxy(1080)
        self.addCleanup(relay.stop)
        with patch.object(proxy.socket, 'socket') as create_socket:
            listener = create_socket.return_value
            listener.bind.side_effect = OSError('fixture bind failure')
            with self.assertRaisesRegex(OSError, 'fixture bind failure'):
                relay.start()
        listener.close.assert_called_once_with()
        self.assertFalse(relay._listeners)
        self.assertFalse(relay.edges)

    def test_preferred_addresses_only_sort_current_official_dns_results(self):
        relay = proxy.LoopbackEdgeProxy(1080, preferred_addresses=('127.0.0.2', '192.0.2.1'))
        self.addCleanup(relay.stop)
        records = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
                    (address, proxy.EDGE_PORT))
                   for address in ('127.0.0.3', '127.0.0.2', '127.0.0.3')]
        with patch.object(proxy.socket, 'getaddrinfo', return_value=records) as dns, \
                patch.object(relay, '_spawn') as spawn:
            edges = relay.start()
        self.assertEqual(len(edges), 2)
        self.assertEqual(dns.call_count, 2)
        self.assertEqual([call.args for call in dns.call_args_list], [
            (host, proxy.EDGE_PORT, socket.AF_INET, socket.SOCK_STREAM)
            for host in proxy.EDGE_HOSTS])
        self.assertEqual(spawn.call_count, 2)
        for call in spawn.call_args_list:
            self.assertEqual(call.args[2], ['127.0.0.2', '127.0.0.3'])

    def test_port_validation_precedes_dns_or_listener_creation(self):
        for port in (True, False, None, '1080', 1.5, 0, -1, 65536):
            with self.subTest(port=port), patch.object(proxy.socket, 'socket') as create_socket:
                with self.assertRaises(ValueError):
                    proxy.LoopbackEdgeProxy(port)
                create_socket.assert_not_called()
        self.assertFalse(self.dns_requests)
        for port in (1, 65535):
            self.assertEqual(proxy.LoopbackEdgeProxy(port).proxy_port, port)


if __name__ == '__main__':
    unittest.main()
