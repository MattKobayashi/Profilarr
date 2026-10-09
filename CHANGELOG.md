# Changelog

All notable changes to Profilarr are documented here.

## [2.1.7] - 2026-09-25

### Fixed

* HLS sources no longer hang at startup with an empty buffer and
  `Will reconnect at <playlist size>, error=End of file` in the logs.
  `-reconnect_at_eof` treats the normal end of an HLS playlist or
  segment as a dropped connection and re-requests the master playlist
  forever, so no media is ever downloaded. The flag is now only
  applied to non-HLS sources, where EOF really does indicate a dropped
  live TS source. Other reconnect options are unchanged.
* HLS detection no longer relies on URL string matching. URLs whose path
  ends in `.m3u8` / `.m3u` are still trusted immediately; MPEG-TS paths
  also skip probing. Other HTTP(S) URLs are probed for the `#EXTM3U`
  playlist header (the same check FFmpeg's own `hls_probe` uses), so
  extensionless playlists, redirects, and proxy endpoints are classified
  correctly. If the probe fails, detection falls back to path/query
  extension heuristics.
  When the probe finds a playlist that FFmpeg would not auto-detect
  (no standard playlist extension or MIME type), the supervisor now
  passes `-f hls` so the input still opens; FFmpeg otherwise refuses
  such inputs with `Not detecting m3u8/hls with non standard extension
  and non standard mime type`.
* Single-use MPEG-TS URLs with standard TS suffixes are no longer consumed
  by a preliminary detection request. An optional trailing `hls` / `mpegts`
  supervisor argument also bypasses probing for extensionless single-use
  URLs and forces the corresponding input format. Existing commands retain
  automatic detection when the argument is omitted.
* Redirected playlists use the original input URL when deciding whether
  FFmpeg needs `-f hls`. A playlist suffix on a redirect target no longer
  masks a non-standard extension on the URL passed to FFmpeg.
* HLS MIME checks now match FFmpeg's exact allowlist, ignoring case and
  MIME parameters. Sniffed playlists served as `application/mpegurl`,
  `application/m3u8`, or other non-standard types now receive `-f hls`
  instead of incorrectly relying on FFmpeg's automatic detection.
* Increased the FFmpeg probe budget from `-probesize 2M -analyzeduration 1M`
  to `5M` / `5M`. The smaller budget failed to resolve HE-AAC (implicit
  SBR) audio parameters on multi-variant HLS masters, producing
  `Could not find codec parameters ... unspecified sample rate` and
  failing Audio: Copy profiles with `[mpegts] sample rate not set`.

## [2.1.6] - 2026-09-23

### Changed

* Simplified the Profilarr interface to three configuration selectors:

  * Hardware / Video Profile
  * Audio Transcoding Override
  * CVLC Network Cache
* Added explicit hardware and frame-rate selections:

  * Passthrough
  * NVIDIA NVENC Source FPS
  * NVIDIA NVENC Force 30 FPS
  * NVIDIA NVENC Force 60 FPS
  * Intel QSV Source FPS
  * Intel QSV Force 30 FPS
  * Intel QSV Force 60 FPS
  * AMD AMF Source FPS
  * AMD AMF Force 30 FPS
  * AMD AMF Force 60 FPS
  * CPU Software Source FPS
  * CPU Software Force 30 FPS
  * CPU Software Force 60 FPS
* Standardized the audio selection names:

  * AAC
  * AC3
  * E-AC3
  * Opus
  * MP3
  * Copy
* Changed the internal Copy audio label to simply `Copy`.
* Moved the complete Profilarr runtime installation to:

  * `/data/plugins/profilarr`
* Removed use of the previous runtime directory:

  * `/data/profilarr`

### Added

* Native Dispatcharr Output Profile synchronization.
* Matching Stream Profile and Output Profile generation.
* Automatic Stream Profile default synchronization.
* Automatic live Output Profile default synchronization.
* Native FFmpeg Output Profile pipeline using video copy:

  * `-c:v copy`
* Automatic cleanup of obsolete unlocked Profilarr Stream Profiles.
* Automatic cleanup of obsolete unlocked Profilarr Output Profiles.
* Locked Dispatcharr profiles are preserved.
* Predictable matching profile names.

### Profile Naming

Stream Profiles now use:

```text
Profilarr Profile - [PROFILE] + Audio: [AUDIO]
```

Output Profiles use:

```text
Profilarr Output - [PROFILE] + Audio: [AUDIO]
```

### Defaults

* Removed the separate `Set as Default` configuration option.
* `Apply & Synchronize` now automatically makes the generated Stream Profile the Dispatcharr Stream Profile default.
* `Apply & Synchronize` now automatically makes the generated Output Profile the live Output Profile default.

### Runtime

* Profilarr supervisor architecture retained.
* Wrapper scripts remain generated automatically.
* Runtime scripts are installed under `/data/plugins/profilarr`.
* Supervisor and wrapper permissions are enforced during installation.

### Testing

Validated NVIDIA configurations:

* NVIDIA NVENC Source FPS
* NVIDIA NVENC Force 30 FPS
* NVIDIA NVENC Force 60 FPS
* Multiple audio overrides
* CVLC network caching configuration
* Native Stream Profile synchronization
* Native Output Profile synchronization
* Automatic default synchronization

Intel and AMD profiles remain available for hardware-specific validation on systems containing the corresponding GPUs.

---

## [2.0.7]

### Changed

* Continued the Profilarr supervisor architecture.
* Improved hardware-aware stream profile generation.
* Added configurable video, audio, FPS, and network cache controls.
* Improved runtime process management.
* Added supervisor lifecycle handling.

---

## [2.0.6]

### Changed

* Improved profile generation and runtime handling.
* Updated supervisor behavior.
* Improved stream process cleanup and lifecycle management.

---

## [2.0.5]

### Added

* Hardware-aware FFmpeg profiles.
* NVIDIA, AMD, Intel, and CPU encoding support.
* Configurable CVLC network caching.
* Profile generation through the Dispatcharr plugin system.

---

## [2.0.2]

### Changed

* Improved stream supervisor handling.
* Updated generated wrapper scripts.
* Improved FFmpeg/CVLC pipeline stability.

---

## [2.0.1]

### Added

* Initial Profilarr plugin architecture.
* Dispatcharr Stream Profile integration.
* FFmpeg and CVLC streaming pipeline.
* Hardware encoder support.
