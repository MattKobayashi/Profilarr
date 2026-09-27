#!/usr/bin/env python3
"""Profilarr process supervisor."""
from __future__ import annotations

import ctypes
import ctypes.util
import http.client
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path


PR_SET_PDEATHSIG = 1

libc = ctypes.CDLL(
    ctypes.util.find_library("c") or "libc.so.6",
    use_errno=True,
)


VIDEO_ENGINES = {
    "copy": ("copy", "none"),
    "h264_nvenc": ("h264_nvenc", "nvenc"),
    "hevc_nvenc": ("hevc_nvenc", "nvenc"),
    "av1_nvenc": ("av1_nvenc", "nvenc"),
    "h264_amf": ("h264_amf", "amf"),
    "hevc_amf": ("hevc_amf", "amf"),
    "av1_amf": ("av1_amf", "amf"),
    "h264_qsv": ("h264_qsv", "qsv"),
    "hevc_qsv": ("hevc_qsv", "qsv"),
    "av1_qsv": ("av1_qsv", "qsv"),
    "libx264": ("libx264", "cpu"),
    "libx265": ("libx265", "cpu"),
    "libsvtav1": ("libsvtav1", "cpu"),
}


AUDIO_ENCODERS = {
    "copy": ("copy", None),
    "aac": ("aac", "192k"),
    "ac3": ("ac3", "192k"),
    "eac3": ("eac3", "192k"),
    "opus": ("libopus", "128k"),
    "mp3": ("libmp3lame", "192k"),
}


ALLOWED = {
    "default": {"copy"},
    "nvidia": {
        "h264_nvenc",
        "hevc_nvenc",
        "av1_nvenc",
    },
    "amd": {
        "h264_amf",
        "hevc_amf",
        "av1_amf",
    },
    "intel": {
        "h264_qsv",
        "hevc_qsv",
        "av1_qsv",
    },
    "cpu": {
        "libx264",
        "libx265",
        "libsvtav1",
    },
}


FPS_ALLOWED = {
    "default": {"copy"},
    "nvidia": {"copy", "30", "60"},
    "amd": {"copy", "30", "60"},
    "intel": {"copy", "30", "60"},
    "cpu": {"copy"},
}


def set_pdeathsig():
    if libc.prctl(
        PR_SET_PDEATHSIG,
        signal.SIGKILL,
        0,
        0,
        0,
    ) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))

    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGKILL)


def child_setup():
    set_pdeathsig()


def terminate(proc):
    if proc is None or proc.poll() is not None:
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + 2

    while (
        proc.poll() is None
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)

    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def video_args(v, fps="copy"):
    """Return (input_args, output_args) for the given video encoder.

    input_args  - hardware-accel / device options that MUST precede -i.
    output_args - encoder, filter, and metadata options that come after -i.
    """
    enc, family = VIDEO_ENGINES[v]

    if enc == "copy":
        if fps != "copy":
            raise ValueError(
                "fps override requires a video encoder, not copy"
            )

        return [], ["-c:v", "copy"]

    input_args = []
    output_args = []

    # =========================================================================
    # CORE HARDWARE ACCELERATION ENGINE TRACKS
    # =========================================================================

    if family == "nvenc":
        output_args += [
            "-c:v",
            enc,
            "-preset",
            "p4",
            "-rc",
            "cbr",
            "-b:v",
            "8M",
            "-maxrate",
            "8M",
            "-bufsize",
            "16M",
        ]

    elif family == "amf":
        output_args += [
            "-c:v",
            enc,
            "-quality",
            "balanced",
            "-rc",
            "cbr",
            "-b:v",
            "8M",
            "-maxrate",
            "8M",
            "-bufsize",
            "16M",
        ]

    elif family == "qsv":
        # FIX FOR FFmpeg 8.x + Intel QSV container timeout lags:
        # Forces immediate initialization of the QuickSync hardware
        # environment before opening codecs, bypassing the internal
        # device-probing delay that triggers provider 404 drops.
        #
        # -init_hw_device, -hwaccel, and -hwaccel_device are INPUT
        # options -- they MUST appear before -i <url>.  Placing them
        # after -i causes FFmpeg to treat them as output options for
        # pipe:1, producing:
        #   "Option hwaccel cannot be applied to output url pipe:1"
        input_args += [
            "-init_hw_device",
            "qsv=qsv",
            "-hwaccel",
            "qsv",
            "-hwaccel_device",
            "qsv",
        ]
        output_args += [
            "-c:v",
            enc,
            "-preset",
            "veryfast",
            "-async_depth",
            "1",
            "-b:v",
            "8M",
            "-maxrate",
            "8M",
            "-bufsize",
            "16M",
        ]

    elif family == "cpu":
        if fps != "copy":
            raise ValueError(
                "fps override is only available for NVIDIA, AMD, "
                "and Intel profiles"
            )

        output_args += [
            "-c:v",
            enc,
            "-preset",
            "superfast",
            "-tune",
            "zerolatency",
            "-b:v",
            "8M",
            "-maxrate",
            "8M",
            "-bufsize",
            "16M",
        ]

    # --- Frame Rate Processing Track ---

    if fps != "copy" and family in ("nvenc", "amf", "qsv"):
        rate = (
            "30000/1001"
            if fps == "30"
            else "60000/1001"
        )

        gop = (
            "60"
            if fps == "30"
            else "120"
        )

        # QSV handles framerate alterations natively within
        # the hwaccel engine pipeline via vpp_qsv.
        if family == "qsv":
            output_args += [
                "-vf",
                f"vpp_qsv=fps={rate}",
                "-fps_mode",
                "cfr",
                "-g",
                gop,
                "-keyint_min",
                gop,
            ]
        else:
            output_args += [
                "-vf",
                f"fps={rate}",
                "-fps_mode",
                "cfr",
                "-g",
                gop,
                "-keyint_min",
                gop,
            ]

    # --- Profile Metadata Flags ---

    if enc.startswith("h264_") or enc == "libx264":
        output_args += [
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
        ]

    elif enc.startswith("hevc_") or enc == "libx265":
        output_args += [
            "-pix_fmt",
            "yuv420p",
        ]

    return input_args, output_args


def audio_args(a):
    enc, br = AUDIO_ENCODERS[a]

    out = [
        "-c:a",
        enc,
    ]

    if br:
        out += [
            "-b:a",
            br,
            "-ac",
            "2",
        ]

    return out


PLAYLIST_SUFFIXES = (
    ".m3u8",
    ".m3u",
)

HLS_CONTENT_TYPES = (
    "mpegurl",
    "m3u8",
)

SNIFF_BYTES = 1024

SNIFF_TIMEOUT = 3.0


def url_ext_hls(url):
    parts = urllib.parse.urlsplit(url)
    hay = (parts.path + "?" + parts.query).lower()
    return ".m3u" in hay or "m3u8" in hay


def ffmpeg_auto_hls(ctype, final):
    if any(t in ctype for t in HLS_CONTENT_TYPES):
        return True

    path = urllib.parse.urlsplit(final).path.lower()
    return path.endswith(PLAYLIST_SUFFIXES)


def probe_hls(url, ua):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": ua,
            "Accept-Encoding": "identity",
            "Range": "bytes=0-" + str(SNIFF_BYTES - 1),
            "Connection": "close",
        },
    )

    with urllib.request.urlopen(
        req,
        timeout=SNIFF_TIMEOUT,
    ) as resp:
        ctype = (
            resp.headers.get("Content-Type") or ""
        ).lower()
        final = resp.geturl()
        try:
            body = resp.read(SNIFF_BYTES)
        except http.client.IncompleteRead as e:
            body = e.partial

    if body.startswith(b"\x1f\x8b"):
        try:
            body = zlib.decompressobj(31).decompress(body, SNIFF_BYTES)
        except zlib.error:
            return None, False

    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
    body = body.lstrip()

    if body.startswith(b"#EXTM3U"):
        return True, not ffmpeg_auto_hls(ctype, final)

    if body:
        return False, False

    if any(t in ctype for t in HLS_CONTENT_TYPES):
        return True, False

    if url_ext_hls(final):
        return True, False

    return None, False


def detect_hls(url, ua):
    parts = urllib.parse.urlsplit(url)

    if parts.path.lower().endswith(PLAYLIST_SUFFIXES):
        return True, False

    if parts.scheme.lower() not in ("http", "https"):
        return url_ext_hls(url), False

    try:
        sniffed, force = probe_hls(url, ua)
    except (
        OSError,
        ValueError,
        http.client.HTTPException,
    ) as e:
        print(
            f"[Profilarr] HLS probe failed "
            f"({type(e).__name__}: {e}); "
            f"falling back to URL heuristics",
            file=sys.stderr,
        )
        sniffed, force = None, False

    if sniffed is not None:
        return sniffed, force

    return url_ext_hls(url), False


def ffmpeg_cmd(
    profile,
    ua,
    url,
    video,
    audio,
    fps="copy",
):
    if profile not in ALLOWED:
        raise ValueError(
            "unknown profile identifier: " + profile
        )

    if video not in ALLOWED[profile]:
        raise ValueError(
            f"video override '{video}' is invalid "
            f"for profile '{profile}'"
        )

    if audio not in AUDIO_ENCODERS:
        raise ValueError(
            "unknown audio override: " + audio
        )

    if fps not in FPS_ALLOWED.get(profile, {"copy"}):
        raise ValueError(
            f"fps override '{fps}' is invalid "
            f"for profile '{profile}'"
        )

    if fps != "copy" and video == "copy":
        raise ValueError(
            "fps override requires a video codec override, "
            "not copy"
        )

    video_input, video_output = video_args(video, fps)

    hls_input, force_demux = detect_hls(url, ua)

    c = [
        "ffmpeg",
        "-hide_banner",
        "-user_agent",
        ua,
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-multiple_requests",
        "1",
        "-seekable",
        "0",
        "-fflags",
        "+discardcorrupt+genpts+igndts",
        "-probesize",
        "5M",
        "-analyzeduration",
        "5M",
    ]

    if not hls_input:
        c += [
            "-reconnect_at_eof",
            "1",
        ]

    # Hardware-accel / device input options must precede -i.
    c += video_input

    if force_demux:
        c += ["-f", "hls"]

    c += [
        "-i",
        url,
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
    ]

    # Encoder, filter, and metadata output options follow -i.
    c += video_output
    c += audio_args(audio)

    c += [
        "-mpegts_copyts",
        "0",
        "-avoid_negative_ts",
        "make_zero",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-max_muxing_queue_size",
        "4096",
        "-flush_packets",
        "1",
        "-mpegts_flags",
        "+pat_pmt_at_frames+resend_headers+initial_discontinuity",
        "-f",
        "mpegts",
        "pipe:1",
    ]

    return c


CACHE_ALLOWED = {
    "3000",
    "6000",
    "9000",
    "12000",
    "15000",
}


def cvlc_cmd(cache="6000"):
    if cache not in CACHE_ALLOWED:
        raise ValueError(
            "unknown network cache value: " + cache
        )

    return [
        "cvlc",
        "-I",
        "dummy",
        "--no-lua",
        "--no-auto-preparse",
        "--no-dbus",
        "--no-interact",
        "--no-stats",
        "--aout",
        "adummy",
        "--vout",
        "vdummy",
        "--no-sout-all",
        "--sout-keep",
        "--network-caching",
        cache,
        "--sout-mux-caching",
        "1500",
        "--adaptive-logic=highest",
        "--sout=#std{access=file,mux=ts,dst=-}",
        "fd://0",
    ]


def main():
    if len(sys.argv) != 8:
        print(
            "usage: profilarr-supervisor.py "
            "<profile_key> <userAgent> <streamUrl> "
            "<videoOverride> <audioOverride> "
            "<fpsOverride> <networkCaching>",
            file=sys.stderr,
        )
        return 2

    profile, ua, url, video, audio, fps, cache = sys.argv[1:]

    try:
        set_pdeathsig()

        run_dir = Path("/data/plugins/profilarr/run")
        run_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        pidfile = run_dir / f"{os.getpid()}.pid"
        pidfile.write_text(
            str(os.getpid()) + "\n"
        )

        ffmpeg = None
        cvlc = None

        def shutdown(signum, frame):
            terminate(cvlc)
            terminate(ffmpeg)

            try:
                pidfile.unlink()
            except FileNotFoundError:
                pass

            raise SystemExit(128 + signum)

        signal.signal(
            signal.SIGTERM,
            shutdown,
        )

        signal.signal(
            signal.SIGINT,
            shutdown,
        )

        try:
            ffmpeg = subprocess.Popen(
                ffmpeg_cmd(
                    profile,
                    ua,
                    url,
                    video,
                    audio,
                    fps,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=None,
                start_new_session=True,
                preexec_fn=child_setup,
                close_fds=True,
            )

            cvlc = subprocess.Popen(
                cvlc_cmd(cache),
                stdin=ffmpeg.stdout,
                stdout=sys.stdout,
                stderr=None,
                start_new_session=True,
                preexec_fn=child_setup,
                close_fds=True,
            )

            ffmpeg.stdout.close()

            while True:
                if cvlc.poll() is not None:
                    terminate(ffmpeg)
                    return cvlc.returncode

                if ffmpeg.poll() is not None:
                    terminate(cvlc)
                    return ffmpeg.returncode

                time.sleep(0.25)

        finally:
            terminate(cvlc)
            terminate(ffmpeg)

            try:
                pidfile.unlink()
            except FileNotFoundError:
                pass

    except (ValueError, OSError) as e:
        print(
            f"[Profilarr] {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
