# PS3 Stream: real-server validation gates

Last observed topology: macOS 15.5 arm64 Mac mini → Windows 10 x64 Jellyfin 12.4.0 at `http://192.168.31.243:8096`, Intel HD Graphics 630 and FFmpeg 8.1.3-Jellyfin. Transport is LAN HTTP; no VPN endpoint is required. As of 2026-11-24 the Mac mini reached `/System/Info/Public` with 200 in roughly 14 ms. These observations can become stale and must be rechecked before running tests.

## Release criteria

| Gate | Purpose | Safe evidence | Current result |
| --- | --- | --- | --- |
| G0 | Repo artifact integrity and Apple Silicon architecture | SHA256 matches published zip, ARM64 main executable and bundled mpv, valid app directory | Pending macOS Actions |
| G1 | Network and server health | 8096 reachable from Mac, health JSON 200 and version | PASS (live Mac + server, no auth) |
| G2 | PS3 identity and test authentication | Server sees Client=PS3/Device=PS3/Version=0.1/User-Agent=JellyfinPS3/0.1 with unique DeviceId; disposable test account only | PASS (fixture), PENDING live token |
| G3 | Media discovery | Authenticated titles, seasons/episodes, LiveTV queries, authorized poster art, pagination and search | PASS (fixture), PENDING live token |
| G4 | Video negotiation | PlaybackInfo PS3 profile, H264 MPEG-TS URL, quality ladder, video budget, audio channel selection and subtitles | PASS (fixture), PENDING real Jellyfin |
| G5 | Real stream decoding | Mac mpv decodes 720p baseline H264/MP3 with actual time progression, no simulator frames | PASS Windows CI, PENDING macOS native run |
| G6 | Playback lifecycle | Start/progress/stop requests, live ID only at stop, no 10.11 crash, no abandoned sessions | PASS fixture, PENDING live test |
| G7 | VOD resume and seek | PlaySessionId/media source unchanged across server-side seek; TS and timeline valid afterward | PASS fixture, PENDING live test |
| G8 | 360p/480p/720p/1080p modes | Actual FFmpeg profile/level and bitrate behavior, check server FFmpeg logs for transcode failures | PASS golden request tests, PENDING live transcodes |
| G9 | Fault tolerance and resource limits | Switchable 802.11b pacing and CPU/RSX timing; reconnection, pause, quality change and timeouts; redacted logs | PASS simulator fixture, PENDING live |
| G10 | Actual physical PS3 client | Real PS3 Jellyfin PKG startup, Cell/RSX playback, network, audio, subtitles, long-duration 1080p, LiveTV | NOT TESTED; needs physical PS3 |

PASS in a fixture means only modeled protocol conformance, not confirmed behavior on the actual Windows server.

## Live tests

Public-health only (no credentials, no writes):

```bash
python tools/ps3-lab-windows/tests/live_server_gate.py \
  --server http://192.168.31.243:8096
```

Authenticated read-only checks use one of:

```bash
export JELLYFIN_TEST_TOKEN='test-account-token'
# or set JELLYFIN_TEST_USERNAME and JELLYFIN_TEST_PASSWORD from the host secret store
python tools/ps3-lab-windows/tests/live_server_gate.py \
  --server http://192.168.31.243:8096
```

Do **not** send credentials into GitHub repositories, CI logs, Slack, Telegram or generated evidence. Store them in the connected Mac's secure environment or secrets manager. Use a dedicated low-privilege Jellyfin account with one disposable test library.

Authenticated opt-in real video test:

```bash
python tools/ps3-lab-windows/tests/live_server_gate.py \
  --server http://192.168.31.243:8096 \
  --stream-test --seek --media vod \
  --report ps3-redacted-report.json
```

This sends start/progress/stop playstate and reads a bounded sample of the stream. It updates the disposable test user's playback history, so only run it in the authorized test account. The default mode never sends playback-state mutations. Select a test movie or episode that exists in the authorized library; avoid private or sensitive content.

## Server changes

Keep the actual Windows Jellyfin installation stable until a specific error is reproduced. Before any change, collect (redacted) request parameters, timestamp, Jellyfin server version, code/FFmpeg exit reason, SessionId continuity, and transport packet metrics. Back up any config file before changing it. Avoid firewall changes, admin-token handling, credential extraction, global cache purges or automatic service restarts.

For Intel HD 630, only report Quick Sync capability after an actual synthetic encode passes; FFmpeg compiled-in `h264_qsv` does not establish GPU/driver runtime availability. Verify fallback software transcoding as a separate case.

## Escalation

Escalate here when the Mac host needs an installation approval; an authenticated test account or scoped token is unavailable; a GitHub-hosted macOS runner is inaccessible; a server change needs a maintenance window; or an unreproducible failure needs a live PS3 capture. Report one blocker, required action and relevant evidence. Do not claim the final PS3 is verified until G10 passes on real console hardware.
