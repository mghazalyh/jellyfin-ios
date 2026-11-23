# Jellyfin PS3 Lab for Windows

This is a Windows Jellyfin playback client and PS3 protocol/resource simulator.
The code and binaries are built through GitHub, not on any user's machines.

## Implemented capabilities

- Authenticates with a unique diagnostic DeviceId and the installed PS3
  client's exact MediaBrowser Client=PS3, Device=PS3, Version=0.1 and
  JellyfinPS3/0.1 User-Agent identity.
- Makes PS3-style PlaybackInfo and Videos/{id}/stream.ts requests, and
  posts Sessions/Playing, Progress, and Stopped using real PS3 live-safety rules.
- Streams through a loopback TCP relay into bundled portable mpv, or performs
  a 20-second TS probe without a video decoder.
- Captures MPEG-TS transport sync, PAT/PMT, PIDs, continuity, transport
  error flags, PCR time and audio/video PTS deltas.
- Includes live buttons for PS3 CPU/GPU/RSX slowdowns, 2.4 GHz 802.11b
  Wi-Fi slowdowns and severe combined stress.
- Simulates PS3 1080p YUV420 frame slots, compressed TS read-ahead ring,
  RSX upload / flip FIFO, vblank cadence and source-change PCM gate.
- Produces bounded, redacted JSONL observations and deterministic JSON scenarios.
- Can launch a separately installed official RPCS3 and categorize its log
  messages. It does not embed the PPU/RSX instruction emulator.

## Usage

Extract the Windows release ZIP and run Jellyfin-PS3-Lab.exe. Sign in to
your Jellyfin instance, load Live TV channels or movies/episodes, then
select an item and press Play as PS3 or Probe 20 seconds. Use the PS3
slowness tab to independently toggle CPU/RSX and 802.11b bottlenecks.
Export evidence from the Evidence tab.

## Interpretation

Normal Jellyfin server logs should identify this diagnostic client as PS3,
while the DeviceId remains distinct from the physical console. The
simulator is not guaranteed to trigger every app-specific server branch.
An actual server integration test is required to confirm all behavior.

This is not a cycle-accurate PS3 emulator. RSX and CellAudio timing are
modeled based on sources in JellyFin-PS3 and RPCS3. For actual instruction
execution, install the real PS3 PKG under official RPCS3 and test separately,
subject to real emulator compatibility.

The slow wireless mode represents a conservative 2.4 GHz IEEE 802.11b
effective throughput, jitter and retry-latency model rather than measured
PS3 signal strength. Packet delivery is delayed rather than corrupted
because TCP retransmits wireless frame losses.

Reference PS3 build: mghazalyh/JellyFin-PS3 v3.6.5-surf.1.
Official upstream: jw-notes/JellyFin-PS3.
Hardware reference: RPCS3/rpcs3 RSXThread.cpp, RSXFIFO.cpp, cellAudio.cpp.

## Build

The GitHub Windows workflow tests the simulator on Linux and Windows,
freezes the Python/Tk GUI with PyInstaller, downloads public portable
mpv, and packages the Windows ZIP and SHA256. On a feature-branch push
it publishes a GitHub prerelease once Windows tests and packaging pass.
The iOS IPA workflow remains independent.
