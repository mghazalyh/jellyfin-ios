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

## Requested workload accounting

Runtime model dimensions follow the request's MaxWidth and MaxHeight, rather
than a fixed 720p/1080p toggle. When both video and audio bitrate targets
are available, the nominal multiplexed rate includes the reference 3% TS
allowance. Unknown passthrough audio remains an incomplete estimate, not a
measured bitrate. Stress toggles retain the request's workload; live and VOD
use their distinct modeled compressed-buffer caps. These are request ceilings
and assumptions, not decoded frame dimensions or real console measurements.

### Memory and display cadence fidelity

The console's Live TV path reserves 1920x1080 frame slots even when a
lower stream resolution is requested. The VOD path uses the requested
dimensions. These are nominal slot reservations: previously cached larger
buffers can occupy additional XDR. The modeled ring uses three quarters of
assumed available XDR after the frame reservation, packet-aligned, with
2 MiB nominal minimum and 10 MiB live / 40 MiB VOD maxima. Allocation
success is not measured by the model. At an assumed 14 MiB remaining,
the ring request is approximately 10.5 MiB VOD or 10 MiB live.

The display model carries fractional frame duration between vblanks, matching
the console's duration-consumption gate at 59.94 Hz. It limits outstanding
display debt during starvation. The earlier model rounded 24 fps down to
about 20 fps. All prior simulated timing numbers are provisional until
recomputed using the corrected cadence and memory model. These are still
modeled results, not physical console performance measurements.
