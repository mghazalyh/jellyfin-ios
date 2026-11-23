# PS3 end-to-end compatibility testing

The diagnostic client advertises PS3 in its Jellyfin authentication and stream
requests: Client=PS3, Device=PS3, Version=0.1 and User-Agent=JellyfinPS3/0.1.
Its device ID is distinct from the real console to protect console sessions.

## Verified

1. Authenticated Jellyfin 12.4.0 integration via a disposable QA user:
   movie, series, home, resume and Live TV browsing; PS3 PlaybackInfo;
   actual MPEG-TS relay; seek with session/source continuity; start/progress/
   stopped playstate. This validates real server API exchange, not decoding
   on the console.
2. Real native mpv decoding of synthetic H.264/MP3 MPEG-TS over the private
   loopback relay on Apple Silicon at 360p Baseline 3.1, 720p Baseline 3.1
   and 1080p High 4.2. These decode on the host, not the PS3.
3. Device profile, quality ladder, bitrate and audio budget parity derived
   from the actual PS3 client source, plus session and transport tests.
4. Twelve virtual CPU/RSX/network scenarios: three resolutions by LAN,
   Cell/RSX stress, 802.11b Wi-Fi stress and severe combined stress. This
   model executes no PS3 hardware instructions or firmware.

Run from tools/ps3-lab-windows:

    python -m unittest discover -s tests -p 'test_*.py' -v

Run the safe public readiness gate:

    python tests/live_server_gate.py --server http://SERVER:8096

Authenticated stream/seek gates require non-admin QA credentials in the
local process environment and the explicit --stream-test --seek --media vod
options. Those requests update the test user's Jellyfin playstate.

## PS3 hardware handoff

Model assumptions include 2.5 Mbps effective throughput for nominal 802.11b.
This risks starving 720p 4 Mbps and 1080p 10 Mbps but gives 360p 700 kbps
additional headroom. Simulated Cell/RSX load may register late flips.
No modeled timing is a physical measurement or a hardware guarantee.

The physical PS3 is still required to validate console decoder support,
Cell memory pressure, actual RSX, sync, seek, network and system firmware.
RPCS3 is a separate emulator and needs a compiled PS3 executable; running
the desktop mpv client is not PS3 instruction-level emulation.

The Jellyfin client and server/FFmpeg transcode pipeline are both adjustable.
Use reproducible real-console evidence to choose minimal compatible changes
on either side, and verify/rollback any production-server configuration change.
