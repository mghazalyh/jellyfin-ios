# Jellyfin PS3 Stream for macOS Apple Silicon

Target: macOS 15, ARM64 Mac mini. The SentinelX-connected Mac mini reported arm64 and Darwin kernel 24.5.0. The application and its native media decoder are built and tested on GitHub-hosted Apple Silicon runners, without accessing or modifying the Mac mini.

## Install

Get the Jellyfin-PS3-Stream-macOS-AppleSilicon.zip from the successful GitHub prerelease. Unzip, move Jellyfin-PS3-Stream.app into Applications and open. The package contains the first-party Apple Silicon mpv.app inside its Contents/Resources folder. Homebrew and Python are not required at runtime.

The app is ad hoc signed but not notarized by Apple. If Finder blocks it, use right-click > Open and confirm. Deployment through SentinelX requires a separately authorized transfer and suitable writable file/command permissions.

## Playback

The macOS player launches a native mpv window, preserving real H.264/MP3/AC-3 video playback, TS proxy telemetry, resume, server-side seeking, pause and volume commands from the main cinematic library window. A foreign process cannot use the Windows HWND embedding technique with Cocoa. It does not claim in-process NSView embedding. A future integrated renderer can use libmpv with a native Cocoa view.

The same PS3 MediaBrowser identity, device profile, quality ladder, LiveStreamId safety rules and transport limits used in the Windows version are included, with a distinct diagnostic DeviceId that cannot overwrite physical PS3 credentials.

CPU, RSX and 802.11b resource controls simulate the console. They do not execute PS3 instructions. A separately installed native RPCS3 application is needed for actual emulation.

## Tests

The macOS CI job runs cross-platform Jellyfin protocol tests, macOS Unix-socket IPC tests, the cinematic GUI smoke, a synthetic H.264+MP3 TS decode through the PS3 transport observer, ARM64 PyInstaller app assembly, embedded official mpv validation, signature checks and SHA256 output. GitHub Actions uploads a release ZIP only after the checks succeed.
