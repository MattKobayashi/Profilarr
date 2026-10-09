"""Supervisor regressions using only the standard library and optional FFmpeg."""

import importlib.util
import io
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from collections import Counter
from contextlib import ExitStack, contextmanager, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SUPERVISOR_PATH = Path(__file__).resolve().parents[1] / "profilarr-supervisor.py"
SPEC = importlib.util.spec_from_file_location("profilarr_supervisor", SUPERVISOR_PATH)
supervisor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(supervisor)

USER_AGENT = "ProfilarrRegressionTests"


@contextmanager
def serve_http(routes):
    """Serve byte fixtures, optional redirects, and single-use endpoints."""
    requests = Counter()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            requests[path] += 1
            route = routes.get(path)
            if route is None:
                self.send_error(404)
                return

            if route.get("once") and requests[path] > 1:
                self.send_error(410, "Single-use token already consumed")
                return

            if "location" in route:
                self.send_response(302)
                self.send_header("Location", route["location"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            body = route["body"]
            self.send_response(200)
            self.send_header(
                "Content-Type",
                route.get("content_type", "application/octet-stream"),
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The probe and short FFmpeg runs intentionally close early.
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class DetectionTests(unittest.TestCase):
    def test_playlist_suffixes_skip_probe(self):
        with patch.object(supervisor, "probe_hls") as probe:
            for suffix in (".m3u8", ".M3U8", ".m3u"):
                with self.subTest(suffix=suffix):
                    url = "https://example.test/live" + suffix + "?token=once"
                    self.assertEqual(
                        supervisor.detect_hls(url, USER_AGENT),
                        (True, False),
                    )
            probe.assert_not_called()

    def test_ts_suffixes_skip_probe(self):
        with patch.object(supervisor, "probe_hls") as probe:
            for suffix in (".ts", ".TS", ".m2t", ".m2ts", ".mts", ".mpegts"):
                with self.subTest(suffix=suffix):
                    url = "https://example.test/live" + suffix + "?token=m3u8"
                    self.assertEqual(
                        supervisor.detect_hls(url, USER_AGENT),
                        (False, False),
                    )
            probe.assert_not_called()

    def test_explicit_input_formats_skip_probe(self):
        with patch.object(supervisor, "probe_hls") as probe:
            for input_format, expected in (
                ("mpegts", (False, False)),
                ("hls", (True, True)),
            ):
                with self.subTest(input_format=input_format):
                    self.assertEqual(
                        supervisor.detect_hls(
                            "https://example.test/watch?token=once",
                            USER_AGENT,
                            input_format,
                        ),
                        expected,
                    )
            probe.assert_not_called()

    def test_explicit_input_formats_override_suffixes(self):
        with patch.object(supervisor, "probe_hls") as probe:
            self.assertEqual(
                supervisor.detect_hls(
                    "https://example.test/live.ts", USER_AGENT, "hls"
                ),
                (True, True),
            )
            self.assertEqual(
                supervisor.detect_hls(
                    "https://example.test/live.m3u8", USER_AGENT, "mpegts"
                ),
                (False, False),
            )
            probe.assert_not_called()

    def test_invalid_input_format_rejected_before_probe(self):
        with patch.object(supervisor, "probe_hls") as probe:
            with self.assertRaisesRegex(ValueError, "unknown input format"):
                supervisor.detect_hls(
                    "https://example.test/watch", USER_AGENT, "unknown"
                )
            probe.assert_not_called()

    def test_extensionless_auto_detection_still_probes(self):
        url = "https://example.test/watch"
        with patch.object(supervisor, "probe_hls", return_value=(True, True)) as probe:
            self.assertEqual(supervisor.detect_hls(url, USER_AGENT), (True, True))
            probe.assert_called_once_with(url, USER_AGENT)

    def test_non_http_inputs_skip_probe(self):
        with patch.object(supervisor, "probe_hls") as probe:
            for url in ("udp://239.1.2.3:1234", "srt://example.test:1234"):
                with self.subTest(url=url):
                    self.assertEqual(
                        supervisor.detect_hls(url, USER_AGENT), (False, False)
                    )
            probe.assert_not_called()


class CommandTests(unittest.TestCase):
    def test_ts_suffix_retains_reconnect_without_probe(self):
        url = "https://example.test/live.ts?token=once"
        with patch.object(supervisor, "probe_hls") as probe:
            cmd = supervisor.ffmpeg_cmd("default", USER_AGENT, url, "copy", "copy")
            probe.assert_not_called()
        self.assertEqual(cmd[cmd.index("-reconnect_at_eof") + 1], "1")
        self.assertEqual(cmd[cmd.index("-i") + 1], url)
        self.assertNotIn("-f", cmd[:cmd.index("-i")])

    def test_explicit_mpegts_forces_input_and_retains_probe_budget(self):
        with patch.object(supervisor, "probe_hls") as probe:
            cmd = supervisor.ffmpeg_cmd(
                "default", USER_AGENT, "https://example.test/watch", "copy", "copy",
                input_format="mpegts",
            )
            probe.assert_not_called()
        self.assertEqual(cmd[cmd.index("-f"):cmd.index("-f") + 2], ["-f", "mpegts"])
        self.assertLess(cmd.index("-f"), cmd.index("-i"))
        self.assertIn("-reconnect_at_eof", cmd)
        self.assertEqual(cmd[cmd.index("-probesize") + 1], "5M")
        self.assertEqual(cmd[cmd.index("-analyzeduration") + 1], "5M")

    def test_explicit_hls_forces_input_without_probe_or_eof_reconnect(self):
        with patch.object(supervisor, "probe_hls") as probe:
            cmd = supervisor.ffmpeg_cmd(
                "default", USER_AGENT, "https://example.test/watch", "copy", "copy",
                input_format="hls",
            )
            probe.assert_not_called()
        self.assertEqual(cmd[cmd.index("-f"):cmd.index("-f") + 2], ["-f", "hls"])
        self.assertLess(cmd.index("-f"), cmd.index("-i"))
        self.assertNotIn("-reconnect_at_eof", cmd)

    def test_playlist_auto_detection_does_not_force_input(self):
        with patch.object(supervisor, "probe_hls") as probe:
            cmd = supervisor.ffmpeg_cmd(
                "default", USER_AGENT, "https://example.test/live.m3u8", "copy", "copy"
            )
            probe.assert_not_called()
        self.assertNotIn("-reconnect_at_eof", cmd)
        self.assertNotIn("-f", cmd[:cmd.index("-i")])


class CliTests(unittest.TestCase):
    def test_legacy_and_optional_input_format_arguments(self):
        args = [
            "profilarr-supervisor.py", "default", USER_AGENT,
            "https://example.test/watch", "copy", "copy", "copy", "6000",
        ]
        for extra, expected in (([], "auto"), (["mpegts"], "mpegts"), (["hls"], "hls")):
            with self.subTest(extra=extra), ExitStack() as stack:
                stack.enter_context(patch.object(supervisor.sys, "argv", args + extra))
                stack.enter_context(patch.object(supervisor, "set_pdeathsig"))
                stack.enter_context(patch.object(supervisor, "Path"))
                stack.enter_context(patch.object(supervisor.signal, "signal"))
                command = stack.enter_context(patch.object(
                    supervisor, "ffmpeg_cmd", side_effect=ValueError("test sentinel")
                ))
                stack.enter_context(redirect_stderr(io.StringIO()))
                self.assertEqual(supervisor.main(), 1)
                command.assert_called_once_with(
                    "default", USER_AGENT, args[3], "copy", "copy", "copy", expected
                )

    def test_usage_describes_optional_input_format(self):
        stderr = io.StringIO()
        with (
            patch.object(supervisor.sys, "argv", ["profilarr-supervisor.py"]),
            redirect_stderr(stderr),
        ):
            self.assertEqual(supervisor.main(), 2)
        self.assertIn("[inputFormat: auto|hls|mpegts]", stderr.getvalue())


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for streaming tests")
class StreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="profilarr-tests-")
        cls.addClassCleanup(cls.temp_dir.cleanup)
        root = Path(cls.temp_dir.name)
        common = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=10",
            "-c:v", "mpeg2video", "-g", "10",
        ]
        subprocess.run(common + [
            "-t", "3", "-hls_time", "1", "-hls_list_size", "0",
            "-hls_playlist_type", "vod", "-hls_segment_filename",
            str(root / "segment%02d.ts"), str(root / "index.m3u8"),
        ], check=True, capture_output=True, timeout=30)
        subprocess.run(common + [
            "-t", "20", "-f", "mpegts", str(root / "raw.ts"),
        ], check=True, capture_output=True, timeout=30)
        cls.playlist = (root / "index.m3u8").read_bytes()
        cls.raw_ts = (root / "raw.ts").read_bytes()
        cls.segments = {
            "/" + path.name: {"body": path.read_bytes(), "content_type": "video/mp2t"}
            for path in root.glob("segment*.ts")
        }

    def assert_streams(self, cmd):
        cmd = list(cmd)
        cmd[-1:-1] = ["-t", "1"]
        result = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertGreater(len(result.stdout), 188)
        self.assertEqual(result.stdout[:1], b"G")
        self.assertEqual(len(result.stdout) % 188, 0)

    def test_single_use_ts_url_is_opened_once(self):
        routes = {"/live.ts": {"body": self.raw_ts, "once": True}}
        with serve_http(routes) as (origin, requests):
            cmd = supervisor.ffmpeg_cmd(
                "default", USER_AGENT, origin + "/live.ts?token=once", "copy", "copy"
            )
            self.assertEqual(requests["/live.ts"], 0)
            self.assert_streams(cmd)
            self.assertEqual(requests["/live.ts"], 1)

    def test_single_use_extensionless_ts_override_is_opened_once(self):
        routes = {"/watch": {"body": self.raw_ts, "once": True}}
        with serve_http(routes) as (origin, requests):
            cmd = supervisor.ffmpeg_cmd(
                "default", USER_AGENT, origin + "/watch?token=once", "copy", "copy",
                input_format="mpegts",
            )
            self.assertEqual(requests["/watch"], 0)
            self.assert_streams(cmd)
            self.assertEqual(requests["/watch"], 1)

    def test_single_use_extensionless_hls_override_is_opened_once(self):
        routes = dict(self.segments)
        routes["/watch"] = {"body": self.playlist, "once": True}
        with serve_http(routes) as (origin, requests):
            cmd = supervisor.ffmpeg_cmd(
                "default", USER_AGENT, origin + "/watch?token=once", "copy", "copy",
                input_format="hls",
            )
            self.assertEqual(requests["/watch"], 0)
            self.assert_streams(cmd)
            self.assertEqual(requests["/watch"], 1)

    def test_single_use_playlist_suffix_is_opened_once(self):
        routes = dict(self.segments)
        routes["/index.m3u8"] = {"body": self.playlist, "once": True}
        with serve_http(routes) as (origin, requests):
            cmd = supervisor.ffmpeg_cmd(
                "default", USER_AGENT, origin + "/index.m3u8?token=once", "copy", "copy"
            )
            self.assertEqual(requests["/index.m3u8"], 0)
            self.assert_streams(cmd)
            self.assertEqual(requests["/index.m3u8"], 1)

    def test_extensionless_auto_detection_still_streams(self):
        routes = dict(self.segments)
        routes["/watch"] = {"body": self.playlist}
        with serve_http(routes) as (origin, requests):
            cmd = supervisor.ffmpeg_cmd(
                "default", USER_AGENT, origin + "/watch", "copy", "copy"
            )
            self.assertEqual(requests["/watch"], 1)
            self.assert_streams(cmd)
            self.assertEqual(requests["/watch"], 2)


if __name__ == "__main__":
    unittest.main()
