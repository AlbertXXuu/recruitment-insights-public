"""TLS-transparent, loopback-only edge forwarding through an existing SOCKS5 proxy."""
import ipaddress
import socket
import threading
import time

EDGE_HOSTS = ('region1.v2.argotunnel.com', 'region2.v2.argotunnel.com')
EDGE_PORT = 7844


def _receive(stream, size):
    data = b''
    while len(data) < size:
        chunk = stream.recv(size - len(data))
        if not chunk:
            raise OSError('本地代理提前关闭连接')
        data += chunk
    return data


def connect_socks(proxy_port, address, on_open=None):
    """Forward only an IPv4 Cloudflare edge connection; keep TLS in cloudflared."""
    packed = ipaddress.IPv4Address(address).packed
    stream = socket.create_connection(('127.0.0.1', proxy_port), timeout=5)
    try:
        if on_open is not None:
            on_open(stream)
        stream.sendall(b'\x05\x01\x00')
        if _receive(stream, 2) != b'\x05\x00':
            raise OSError('本地代理不支持无认证 SOCKS5')
        stream.sendall(b'\x05\x01\x00\x01' + packed + EDGE_PORT.to_bytes(2, 'big'))
        reply = _receive(stream, 4)
        if reply[:3] != b'\x05\x00\x00':
            raise OSError('本地代理未连接 Cloudflare 节点')
        if reply[3] == 1:
            _receive(stream, 6)
        elif reply[3] == 4:
            _receive(stream, 18)
        elif reply[3] == 3:
            _receive(stream, _receive(stream, 1)[0] + 2)
        else:
            raise OSError('本地代理返回无效地址类型')
        stream.settimeout(None)
        return stream
    except BaseException:
        stream.close()
        raise


class LoopbackEdgeProxy:
    """Own two private relays and their sockets; never change system routing."""
    def __init__(self, proxy_port, preferred_addresses=()):
        if isinstance(proxy_port, bool) or not isinstance(proxy_port, int) or not 1 <= proxy_port <= 65535:
            raise ValueError('本地 SOCKS5 代理端口必须为 1–65535')
        self.proxy_port = proxy_port
        self.preferred_addresses = tuple(preferred_addresses)
        self.edges = []
        self._listeners, self._threads, self._sockets = [], set(), set()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._capacity = threading.BoundedSemaphore(8)

    def start(self):
        if self._listeners:
            raise RuntimeError('此 Tunnel 的本地转发已经运行')
        self._stopped.clear()
        try:
            for hostname in EDGE_HOSTS:
                addresses = list(dict.fromkeys(item[4][0] for item in
                    socket.getaddrinfo(hostname, EDGE_PORT, socket.AF_INET, socket.SOCK_STREAM)))
                if not addresses:
                    raise OSError('Cloudflare 节点 DNS 未返回 IPv4 地址')
                addresses.sort(key=lambda address: address not in self.preferred_addresses)
                listener = socket.socket()
                self._listeners.append(listener)
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.bind(('127.0.0.1', 0))
                listener.listen(8)
                listener.settimeout(0.25)
                self.edges.append('127.0.0.1:' + str(listener.getsockname()[1]))
                self._spawn(self._accept, listener, addresses)
            return list(self.edges)
        except BaseException:
            self.stop()
            raise

    def _spawn(self, function, *args):
        def run():
            try:
                function(*args)
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())
        thread = threading.Thread(target=run, daemon=True)
        with self._lock:
            if self._stopped.is_set():
                return None
            self._threads.add(thread)
            try:
                thread.start()
            except BaseException:
                self._threads.discard(thread)
                raise
        return thread

    def _track(self, stream):
        with self._lock:
            if self._stopped.is_set():
                stream.close()
                raise OSError('此 Tunnel 的本地转发已停止')
            self._sockets.add(stream)

    def _accept(self, listener, addresses):
        index = 0
        while not self._stopped.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._capacity.acquire(blocking=False):
                client.close()
                continue
            try:
                self._track(client)
            except OSError:
                self._capacity.release()
                return
            # On a failed TLS connection cloudflared retries another edge address.
            chosen = addresses[index % len(addresses)]
            index += 1
            try:
                worker = self._spawn(self._forward, client, chosen)
            except BaseException:
                self._close(client)
                self._capacity.release()
                raise
            if worker is None:
                self._close(client)
                self._capacity.release()
                return

    def _forward(self, client, address):
        upstream = None
        def opened(stream):
            nonlocal upstream
            upstream = stream
            self._track(stream)
        try:
            upstream = connect_socks(self.proxy_port, address, opened)
            if self._stopped.is_set():
                return
            outgoing = self._spawn(self._copy, client, upstream)
            self._copy(upstream, client)
            if outgoing is not None:
                outgoing.join()
        except OSError:
            pass  # No payloads or credentials are logged.
        finally:
            for stream in (client, upstream):
                if stream is not None:
                    self._close(stream)
            self._capacity.release()

    def _copy(self, source, destination):
        try:
            while not self._stopped.is_set():
                chunk = source.recv(65536)
                if not chunk:
                    break
                destination.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                destination.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _close(self, stream):
        try:
            stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        stream.close()
        with self._lock:
            self._sockets.discard(stream)

    def stop(self):
        self._stopped.set()
        for listener in self._listeners:
            listener.close()
        with self._lock:
            streams = list(self._sockets)
        for stream in streams:
            self._close(stream)
        # Proxy connect has a five-second deadline; joined workers cannot outlive it.
        with self._lock:
            threads = list(self._threads)
        deadline = time.monotonic() + 6
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError('本地 Tunnel 转发未确认停止，请保留控制台重试')
        self._listeners.clear()
        self._threads.clear()
        self.edges.clear()
