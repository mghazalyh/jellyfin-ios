# PS3 Surf parity diagnostics on iPad

This diagnostic build is based on the **installed PS3 fork release `v3.6.5-surf.1`** and also references the source-only unattended branch `feature/ps3-unattended-harness-20261123`.

## Usage

In Settings, enable **PS3 transport diagnostics (experimental)**. The native video player is requested in the WebView bridge and the negotiated device profile prefers HTTP MPEG-TS with H.264/AAC rather than HLS video, in line with the PS3 transport. You can disable the switch to recover ordinary iPad playback and its normal device profile. Changes cause the WebView to reload, leaving existing server-side routes unchanged.

The Settings page also contains **View, export or clear diagnostic evidence**. The log is local to the iPad, bounded to 320 records, persisted periodically, and shared as redacted JSON. No raw URLs, bearer tokens, media titles, channel IDs or usernames are stored. Network instrumentation records category, status, MIME type and latency; native video instrumentation records launch, first ready-for-display, 5-second playback samples, buffering transitions, errors and local session generations. No telemetry is posted automatically to the Windows server.

For comparison, collect PS3 `surf_sync:` records from `/dev_hdd0/tmp/player_log.txt` when debug logging is enabled, JFM1 metrics from the *development build only*, and CatalogService's `SURF_CLIENT` records from the Windows server. Use wall-clock UTC timestamps, relative durations, logical mode and source identity from server logs. iPad and PS3 client generations are *not* shared cross-device identifiers.

## Exact limitations

This is a **transport and behavior emulator**, not a PPU, RSX, CellAudio or Cell decoder emulator. Native iPad playback uses iOS AVPlayer exposed through `expo-av`. It cannot prove actual RSX flip completion, PS3 jitter-buffer occupancy, PCM DMA gate release, physical PS3 free-memory consumption or precise source-to-video frame ownership. The 3.6.5 PS3 client's adaptive gate is off by default. The source-only JFR1/JFM1 harness has not been installed on the physical console according to the handover.

The source itself may rotate while a continuous TS URL stays unchanged. A URL change alone therefore does *not* establish a Surf source transition. The iPad diagnostic log records observed transport and rendering timings, not authoritative source epochs unless those are later supplied through a verified server status integration.

A successful build is not proof that a particular iPad/AVPlayer version can decode a raw continuous MPEG-TS stream. Incompatible transport failures are useful diagnostic evidence and should be compared with standard iOS mode. This implementation does not alter Jellyfin server files or the protected PS3 release.

## Known routes from the server audit

- `/live/{all|sports|movies-series}.ts`: continuous TS (Surf companion)
- `/playlist.ts.m3u`: channel playlist referencing TS routes
- `/hls/{mode}/index.m3u8`: separate HLS interface, not preferred in diagnostic mode
- `POST /api/surf/{mode}/telemetry`: authenticated PS3 telemetry received by CatalogService on port 8097

The iPad uses its configured Jellyfin server URL. Localhost `127.0.0.1:8103` in the server playlist refers to the Windows host and must never be blindly used as an iPad URL. The diagnostic mode does not tunnel or bypass that network boundary.

## Release and regression

GitHub-hosted CI runs the repository's lint, TypeScript, Metro export, Jest and the existing unsigned TrollStore IPA workflow. No user Mac, PC, PS3 or iPad is used to compile this release. The regular release workflow publishes an unsigned IPA when this change reaches `master`.
