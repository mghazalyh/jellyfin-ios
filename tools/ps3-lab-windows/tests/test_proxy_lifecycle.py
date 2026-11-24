"""Loopback-only proxy cancellation, ownership and teardown regression tests.

The mock upstream is a local threading HTTP server. No authentication, real
Jellyfin server, media decoder or persistent service is used here.
"""
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ps3lab.model import Profile
from ps3lab.proxy import LocalProxy, StreamController

CHUNK = bytes([0x47, 0x1f, 0xff, 0x10]) + bytes(184)
READ_SIZE = 188 * 64


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class SlowUpstream:
    def __init__(self, first_packets=64, second_packets=64):
        self.first_chunk = CHUNK * first_packets
        self.second_chunk = CHUNK * second_packets
        self.release = threading.Event()
        self.first_written = threading.Event()
        self.second_written = threading.Event()
        self.connection_finished = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args):
                return

            def do_GET(self):
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp2t")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(owner.first_chunk)
                    self.wfile.flush()
                    owner.first_written.set()
                    owner.release.wait(10)
                    self.wfile.write(owner.second_chunk)
                    self.wfile.flush()
                except (OSError, ValueError):
                    pass
                finally:
                    owner.second_written.set()
                    owner.connection_finished.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/fixture.ts"

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.first_written.is_set():
            self.connection_finished.wait(timeout=3)


class ProxyLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile.from_switches(False, False)
        self.events = []
        self.controllers = []
        self.proxies = []
        self.fixtures = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for fixture in reversed(self.fixtures):
            fixture.close()
        for proxy in reversed(self.proxies):
            proxy.close()
        for control in self.controllers:
            control.stop()

    def make_fixture(self):
        fixture = SlowUpstream()
        self.fixtures.append(fixture)
        return fixture.start()

    def start_proxy(self, url):
        control = StreamController(
            lambda name, data: self.events.append((name, data)))
        control.configure(url, {}, self.profile, live=False)
        self.controllers.append(control)
        proxy = LocalProxy(control)
        self.proxies.append(proxy)
        proxy.start()
        return control, proxy

    def open_stream(self, proxy):
        response = urllib.request.urlopen(proxy.url, timeout=5)
        self.addCleanup(response.close)
        return response

    def packets(self, control):
        return control.inspector.snapshot()["ts_packets"]

    def test_late_chunk_is_not_ingested_after_proxy_close(self):
        fixture = self.make_fixture()
        control, proxy = self.start_proxy(fixture.url)
        response = self.open_stream(proxy)
        self.assertEqual(response.read(READ_SIZE), fixture.first_chunk)
        self.assertTrue(wait_until(lambda: self.packets(control) >= 64))

        before = control._total_bytes
        proxy.close()
        self.assertEqual(control._total_bytes, before)

        fixture.release.set()
        self.assertTrue(fixture.second_written.wait(timeout=2))
        self.assertTrue(wait_until(lambda: proxy.active_connections == 0))
        self.assertEqual(control._total_bytes, before)
        self.assertEqual(self.packets(control), 64)
        response.close()

    def test_reconfigured_session_rejects_previous_handler_data(self):
        fixture = self.make_fixture()
        control, proxy = self.start_proxy(fixture.url)
        response = self.open_stream(proxy)
        self.assertEqual(response.read(READ_SIZE), fixture.first_chunk)
        self.assertTrue(wait_until(lambda: self.packets(control) >= 64))

        control.stop()
        control.configure(fixture.url, {}, self.profile, live=False)
        self.assertEqual(self.packets(control), 0)

        fixture.release.set()
        self.assertTrue(fixture.second_written.wait(timeout=2))
        time.sleep(0.2)
        self.assertEqual(self.packets(control), 0)
        self.assertEqual(control._total_bytes, 0)
        response.close()

    def test_suspended_stream_does_not_flush_one_more_chunk_after_close(self):
        fixture = self.make_fixture()
        control, proxy = self.start_proxy(fixture.url)
        control.suspended.set()
        response = self.open_stream(proxy)
        self.assertTrue(fixture.first_written.wait(timeout=2))
        time.sleep(0.2)

        before = control._total_bytes
        proxy.close()
        self.assertTrue(wait_until(lambda: proxy.active_connections == 0))
        self.assertEqual(control._total_bytes, before)
        self.assertEqual(self.packets(control), 0)
        response.close()

    def test_close_cancels_a_blocked_upstream_read(self):
        fixture = self.make_fixture()
        control, proxy = self.start_proxy(fixture.url)
        response = self.open_stream(proxy)
        self.assertEqual(response.read(READ_SIZE), fixture.first_chunk)
        self.assertTrue(wait_until(lambda: self.packets(control) >= 64))

        started = time.monotonic()
        proxy.close()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5)
        self.assertEqual(proxy.active_connections, 0)
        self.assertFalse(proxy._thread.is_alive())
        fixture.release.set()
        response.close()

    def test_close_before_start_is_bounded_and_stops_its_session(self):
        control = StreamController(lambda *_: None)
        control.configure("http://127.0.0.1:1/fixture.ts", {},
                          self.profile, live=False)
        self.controllers.append(control)
        proxy = LocalProxy(control)
        self.proxies.append(proxy)
        finished = threading.Event()

        def close():
            proxy.close()
            finished.set()

        thread = threading.Thread(target=close, daemon=True)
        thread.start()
        self.assertTrue(finished.wait(timeout=2),
                        "LocalProxy.close() blocked before start")
        thread.join(timeout=1)
        self.assertTrue(control.closed.is_set())
        self.assertEqual(control.stream_url, "")
        self.assertEqual(proxy.active_connections, 0)
        self.assertFalse(proxy._thread.is_alive())

    def test_stale_proxy_close_does_not_stop_a_newer_session(self):
        control = StreamController(lambda *_: None)
        control.configure("http://127.0.0.1:1/one.ts", {},
                          self.profile, live=False)
        self.controllers.append(control)
        proxy = LocalProxy(control)
        self.proxies.append(proxy)
        proxy.close()
        control.configure("http://127.0.0.1:1/two.ts", {},
                          self.profile, live=False)
        proxy.close()
        self.assertFalse(control.closed.is_set())
        self.assertEqual(control.stream_url, "http://127.0.0.1:1/two.ts")

    def test_repeated_close_is_idempotent(self):
        control, proxy = self.start_proxy("http://127.0.0.1:1/fixture.ts")
        proxy.close()
        proxy.close()
        self.assertTrue(control.closed.is_set())
        self.assertFalse(proxy._thread.is_alive())
        self.assertEqual(proxy.active_connections, 0)

    def test_head_after_session_stop_reports_unavailable(self):
        control, proxy = self.start_proxy("http://127.0.0.1:1/fixture.ts")
        control.stop()
        request = urllib.request.Request(proxy.url, method="HEAD")
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=5)
        error = context.exception
        try:
            self.assertEqual(error.code, 503)
        finally:
            error.close()


if __name__ == "__main__":
    unittest.main()
