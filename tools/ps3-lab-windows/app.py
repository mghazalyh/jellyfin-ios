"""Jellyfin PS3 Lab: Windows PS3-identity player, transport observer and simulator.

All client/server work is performed on worker threads. The Tk event loop
is the sole owner of UI controls. Credentials are never saved or exported.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import urllib.request

from ps3lab.jellyfin import JellyfinClient, JellyfinError
from ps3lab.model import Profile, ps3_memory_budget, simulate_scenario
from ps3lab.proxy import LocalProxy, StreamController
from ps3lab.player import (build_mpv_args, ipc_command, new_ipc_path,
                           validated_seek, validated_volume)
from ps3lab.cinema import CinematicUI

MAX_EVENTS = 6000
REF = "mghazalyh/JellyFin-PS3 v3.6.5-surf.1"


class Ps3Lab:
    def __init__(self, root):
        self.root = root
        root.title("Jellyfin PS3 Lab for Windows")
        root.geometry("1060x770")
        self.server = tk.StringVar()
        self.catalog = tk.StringVar()
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.token = tk.StringVar()
        self.cpu = tk.BooleanVar(value=False)
        self.wifi = tk.BooleanVar(value=False)
        self.severe = tk.BooleanVar(value=False)
        self.hd = tk.BooleanVar(value=False)
        self.surround = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Not connected")
        self.summary = tk.StringVar(value="No TS stream")
        self.device_display = tk.StringVar(value="PS3 | PS3 | v0.1 | unique lab DeviceId")
        self.profile_display = tk.StringVar()
        self.rpc_path = tk.StringVar()
        self.events = deque(maxlen=MAX_EVENTS)
        self.bus = queue.Queue(maxsize=5000)
        self.items = []
        self.client = None
        self.session = None
        self.proxy = None
        self.player = None
        self.ipc_path = None
        self.position_ticks = 0
        self.is_paused = False
        self.last_player_poll = 0.0
        self.probe_stop = threading.Event()
        self.profile = Profile.from_switches(False, False)
        self.controller = StreamController(self.emit)
        self.running = False
        self.last_progress_at = 0.0
        self.last_surf_at = 0.0
        self.surf_signature = None
        self.surf_epoch = 0
        self._layout()
        self.apply_switches()
        root.after(200, self.pump)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.emit("lab_started", {"ps3_reference": "v3.6.5-surf.1"})

    def _layout(self):
        # The real media library and embedded player are now the primary UI.
        # Keep the existing PS3 resource, evidence and RPCS3 panels behind
        # the diagnostics view, rather than presenting five debug-first tabs.
        self.ui = CinematicUI(self, self.root)

    def _connect_tab(self, tab):
        form = ttk.Frame(tab)
        form.pack(fill="x")
        for i, (label, var, hidden) in enumerate([
            ("Jellyfin URL", self.server, False),
            ("CatalogService URL (optional)", self.catalog, False),
            ("Jellyfin username", self.username, False),
            ("Password", self.password, True),
            ("Access token (alternative)", self.token, True)
        ]):
            ttk.Label(form, text=label, width=29).grid(row=i, column=0, sticky="w", pady=5)
            ttk.Entry(form, textvariable=var, show="*" if hidden else "", width=74).grid(
                row=i, column=1, sticky="ew", pady=5)
        form.columnconfigure(1, weight=1)
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=12)
        ttk.Button(buttons, text="Sign in", command=self.login).pack(side="left", padx=3)
        ttk.Button(buttons, text="Use token", command=self.login_token).pack(side="left", padx=3)
        ttk.Button(buttons, text="Live TV channels", command=self.load_channels).pack(side="left", padx=3)
        ttk.Button(buttons, text="Movies / episodes", command=self.load_library).pack(side="left", padx=3)
        ttk.Label(tab, textvariable=self.device_display, wraplength=970).pack(anchor="w", pady=10)
        ttk.Label(tab, wraplength=970, text=(
            "The Authorization header identifies Client=PS3, Device=PS3, Version=0.1, "
            "and User-Agent JellyfinPS3/0.1, matching the installed fork. "
            "This diagnostic device deliberately uses a different DeviceId from the console. "
            "Never copy the actual PS3 device identifier or authorization token."
        )).pack(anchor="w")

    def _play_tab(self, tab):
        actions = ttk.Frame(tab)
        actions.pack(fill="x")
        ttk.Button(actions, text="Play as PS3", command=lambda: self.begin(False)).pack(side="left", padx=3)
        ttk.Button(actions, text="Probe 20 seconds", command=lambda: self.begin(True)).pack(side="left", padx=3)
        ttk.Button(actions, text="Stop", command=self.stop).pack(side="left", padx=3)
        ttk.Checkbutton(actions, text="1080p", variable=self.hd).pack(side="left", padx=12)
        ttk.Checkbutton(actions, text="5.1 AC3", variable=self.surround).pack(side="left", padx=4)
        self.tree = ttk.Treeview(tab, show="headings", columns=("type", "title", "id"), height=16)
        for field, width in (("type", 140), ("title", 510), ("id", 275)):
            self.tree.heading(field, text=field)
            self.tree.column(field, width=width)
        self.tree.pack(fill="both", expand=True, pady=11)
        ttk.Label(tab, textvariable=self.summary).pack(anchor="w", pady=8)
        ttk.Label(tab, wraplength=950, text=(
            "Play uses bundled mpv over an opaque loopback MPEG-TS proxy. "
            "Probe reads the same transport without video output. "
            "All TS packets remain intact unless an explicit fault test is added."
        )).pack(anchor="w")

    def _limits_tab(self, tab):
        ttk.Label(tab, text="Switchable PS3 bottleneck simulation",
                  font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=(2, 14))
        ttk.Checkbutton(tab, variable=self.cpu, command=self.apply_switches,
                        text="SIMULATE PS3 CPU + GPU + RSX SLOWNESS").pack(anchor="w", pady=9)
        ttk.Label(tab, wraplength=930, text=(
            "Models the CPU decoder budget, RSX texture upload, flip FIFO, "
            "GPU synchronization, vblank cadence and decoded-frame queue. "
            "The real Windows player is slowed via transport pacing as a surrogate."
        )).pack(anchor="w", padx=22)
        ttk.Checkbutton(tab, variable=self.wifi, command=self.apply_switches,
                        text="SIMULATE 2.4 GHz WIRELESS 802.11b SLOWNESS").pack(anchor="w", pady=(19, 9))
        ttk.Label(tab, wraplength=930, text=(
            "11 Mb/s theoretical PHY, conservative effective throughput around 2.5 Mb/s, "
            "plus latency, jitter, retries and pauses. Models TCP delivery delays, "
            "not permanent TS packet loss."
        )).pack(anchor="w", padx=22)
        ttk.Checkbutton(tab, variable=self.severe, command=self.apply_switches,
                        text="EXTRA SEVERE: CONGESTED NETWORK / OVERLOADED RSX").pack(anchor="w", pady=(19, 9))
        ttk.Button(tab, text="Reset to normal performance", command=self.reset_switches).pack(anchor="w", pady=7)
        ttk.Button(tab, text="Run reproducible 20 second offline simulation",
                   command=self.simulate).pack(anchor="w", pady=7)
        ttk.Label(tab, textvariable=self.profile_display, wraplength=940).pack(anchor="w", pady=12)

    def _evidence_tab(self, tab):
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="Export redacted JSONL", command=self.export).pack(side="left", padx=4)
        ttk.Button(row, text="Export offline simulation JSON", command=self.export_simulation).pack(side="left", padx=4)
        ttk.Button(row, text="Clear events", command=self.clear).pack(side="left", padx=4)
        self.log = tk.Text(tab, state="disabled", wrap="none", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, pady=9)
        ttk.Label(tab, wraplength=940, text=(
            "TS continuity/PCR/PTS values are observations; counters prefixed simulated_ "
            "are model results only. Raw URLs, media titles, token and password are not exported."
        )).pack(anchor="w")

    def _rpc_tab(self, tab):
        ttk.Label(tab, text="Optional official RPCS3 integration",
                  font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=9)
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.rpc_path, width=75).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse", command=self.choose_rpc).pack(side="left", padx=5)
        ttk.Button(tab, text="Launch RPCS3", command=self.start_rpc).pack(anchor="w", pady=11)
        ttk.Button(tab, text="Import RPCS3 log counters", command=self.read_rpc).pack(anchor="w", pady=11)
        ttk.Label(tab, wraplength=950, text=(
            "RPCS3 is not bundled. Install the real JellyFin-PS3 package through "
            "RPCS3's own UI. Whether the PKG and its VDEC/RSX pipeline actually work "
            "under RPCS3 is not yet established. This tab launches RPCS3, not a fake emulator."
        )).pack(anchor="w", pady=10)

    def emit(self, name, data):
        # Only known, non-sensitive fields enter the durable event journal.
        fields = {}
        for key, value in (data or {}).items():
            if str(key).lower() in (
                "token", "authorization", "password", "url", "path",
                "item_id", "source_id", "title", "username", "headers"
            ):
                continue
            if isinstance(value, (int, float, bool)) or value is None:
                fields[key] = value
            elif isinstance(value, str):
                fields[key] = "[redacted]" if "://" in value or "token=" in value.lower() else value[:92]
        record = {"utc": datetime.now(timezone.utc).isoformat(), "event": name, "data": fields}
        self.dispatch("event", record)

    def dispatch(self, kind, value):
        try:
            self.bus.put_nowait((kind, value))
        except queue.Full:
            pass

    def worker(self, callback, *args):
        def run():
            try:
                self.dispatch("result", callback(*args))
            except Exception as ex:
                self.dispatch("error", str(ex) if isinstance(ex, JellyfinError) else type(ex).__name__)
        threading.Thread(target=run, daemon=True).start()

    def login(self):
        self.worker(self._login, self.server.get(), self.username.get(), self.password.get(), "")
    def login_token(self):
        self.worker(self._login, self.server.get(), "", "", self.token.get())

    def _login(self, base, user, password, token):
        new = JellyfinClient(base)
        details = new.authenticate_token(token) if token else new.authenticate(user, password)
        self.client = new
        return "login", details

    def load_channels(self):
        self.ui.navigate("live")

    def load_library(self):
        self.ui.navigate("home")

    def selected(self):
        item = self.ui.selected_item
        if not item or not self.client:
            raise JellyfinError("Sign in and select a movie, episode or channel")
        return item

    def begin(self, probe):
        try:
            item = self.selected()
        except JellyfinError as ex:
            messagebox.showwarning("PS3 Lab", str(ex))
            return
        if not probe:
            self.ui.show_player(item)
        self.worker(self.prepare, item, probe, self.hd.get(), self.surround.get())

    def prepare(self, item, probe, hd, surround):
        session = self.client.make_session(
            item, hd=hd, surround=surround, quality_bps=8000000 if hd else 4000000)
        return "prepared", (session, probe, hd)

    def begin_stream(self, session, probe, hd):
        self.stop()
        self.session = session
        self.probe_stop = threading.Event()
        self.controller.configure(session.url, self.client.headers(), self.profile, hd)
        self.proxy = LocalProxy(self.controller)
        self.proxy.start()
        self.running = True
        self.last_progress_at = 0
        self.last_player_poll = 0
        self.position_ticks = 0
        self.is_paused = False
        self.emit("stream_opened", {"live": session.live, "hd": hd, "mode": self.profile.name})
        self.worker(self.send_playstate, session, "start")
        if probe:
            self.worker(self.probe, self.proxy.url, self.probe_stop)
            self.status.set("Probing live MPEG-TS transport for 20 seconds")
            return
        mpv = self.find_mpv()
        if not mpv:
            self.status.set("No mpv.exe found. Use Probe or install portable mpv.")
            self.stop()
            return
        try:
            self.ipc_path = new_ipc_path()
            window_handle = int(self.ui.video_host.winfo_id())
            args = build_mpv_args(mpv, self.proxy.url, window_handle, self.ipc_path)
        except (ValueError, RuntimeError, tk.TclError) as ex:
            self.emit("mpv_embed_error", {"type": type(ex).__name__})
            self.status.set("Unable to initialize embedded video")
            self.stop()
            return
        try:
            self.player = subprocess.Popen(args, cwd=str(Path(mpv).parent),
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.status.set("Playing Jellyfin media in embedded mpv with PS3 stream negotiation")
        except OSError as ex:
            self.emit("mpv_start_error", {"type": type(ex).__name__})
            self.stop()

    def player_command(self, action, value=None):
        """Issue real media playback commands via private mpv IPC."""
        if not self.player or self.player.poll() is not None or not self.ipc_path:
            self.status.set("No active player")
            return
        commands = {
            "pause": ["cycle", "pause"],
            "back": ["seek", -10, "relative"],
            "forward": ["seek", 30, "relative"],
        }
        if action == "volume":
            parts = ["set_property", "volume", validated_volume(value)]
        elif action == "seek":
            parts = ["seek", validated_seek(value), "relative"]
        else:
            parts = commands.get(action)
        if parts is None:
            self.status.set("Unknown player command")
            return
        pipe = self.ipc_path
        self.worker(lambda: ("mpv_command", (action, ipc_command(pipe, parts))))

    @staticmethod
    def find_mpv():
        paths = []
        if getattr(sys, "frozen", False):
            paths.append(Path(sys.executable).parent / "mpv" / "mpv.exe")
        paths.append(Path(__file__).resolve().parent / "mpv" / "mpv.exe")
        from_path = shutil.which("mpv")
        if from_path:
            paths.append(Path(from_path))
        return str(next((p for p in paths if p.is_file()), ""))

    @staticmethod
    def probe(local_url, stop):
        try:
            with urllib.request.urlopen(local_url, timeout=50) as reader:
                start = time.monotonic()
                while time.monotonic() - start < 20 and not stop.is_set():
                    if not reader.read(188 * 64):
                        break
            return "probe_complete", None
        except (OSError, ValueError) as ex:
            return "probe_error", type(ex).__name__

    def send_playstate(self, session, action):
        try:
            self.client.playstate(session, action, pos_ticks=self.position_ticks,
                                  paused=self.is_paused)
            return "playstate", action
        except Exception as ex:
            return "playstate_error", type(ex).__name__

    def stop(self):
        prior = self.session
        self.session = None
        self.running = False
        self.probe_stop.set()
        self.ipc_path = None
        self.is_paused = False
        if self.player:
            try:
                self.player.terminate()
                self.player.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.player.kill()
                except OSError:
                    pass
            self.player = None
        if self.proxy:
            try:
                self.proxy.close()
            except (OSError, RuntimeError) as ex:
                self.emit("proxy_shutdown_error", {"type": type(ex).__name__})
            self.proxy = None
        if prior and self.client:
            self.worker(self.send_playstate, prior, "stopped")
        self.summary.set("No active stream")

    def apply_switches(self):
        self.profile = Profile.from_switches(self.cpu.get(), self.wifi.get(), self.severe.get())
        self.controller.set_profile(self.profile)
        m = ps3_memory_budget(1920, 1080, self.profile.free_xdr_mib)
        self.profile_display.set(
            f"{self.profile.name}\nCPU decode {self.profile.cpu_decode_ms}ms/frame | "
            f"RSX upload {self.profile.rsx_upload_ms}ms | "
            f"flip {self.profile.rsx_flip_ms}ms | "
            f"802.11b effective {self.profile.effective_wifi_kbps}kb/s (only if enabled)\n"
            f"1080p: {m['decoded_slots']} decoded slots, "
            f"{m['decoded_reserved_bytes'] // 1048576} MiB YUV420, "
            f"{m['live_ring_bytes'] // 1048576} MiB compressed live ring, "
            f"prefill {m['prefill_frames']} frames"
        )
        self.emit("stress_changed", {"cpu_rsx": self.cpu.get(), "wifi_b": self.wifi.get(),
                                     "severe": self.severe.get()})

    def reset_switches(self):
        self.cpu.set(False)
        self.wifi.set(False)
        self.severe.set(False)
        self.apply_switches()

    def simulate(self):
        profile, hd = self.profile, self.hd.get()
        self.worker(lambda: ("simulation", simulate_scenario(
            profile, seconds=20, width=1920 if hd else 1280, height=1080 if hd else 720,
            bitrate_bps=8000000 if hd else 4000000,
            input_bps=3000000 if profile.simulate_wifi_b else 10000000)))

    def _render_event(self, event):
        self.events.append(event)
        self.log.configure(state="normal")
        self.log.insert("end", json.dumps(event, separators=(",", ":")) + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 250:
            self.log.delete("1.0", "5.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def pump(self):
        try:
            for _ in range(120):
                try:
                    kind, value = self.bus.get_nowait()
                except queue.Empty:
                    break
                if kind == "cinema_backdrop":
                    generation, key, item_id, bytes_data = value
                    self.ui.set_backdrop(generation, key, item_id, bytes_data)
                    continue
                if kind == "artwork":
                    generation, idx, bytes_data = value
                    self.ui.set_artwork(generation, idx, bytes_data)
                    continue
                if kind == "event":
                    self._render_event(value)
                    if value["event"] == "sample":
                        data = value["data"]
                        self.summary.set(
                            f"TS={data.get('ts_packets',0)} "
                            f"CC errors={data.get('continuity_errors',0)} "
                            f"PCR={data.get('pcr_elapsed_ms',0)}ms "
                            f"simulated stalls={data.get('simulated_stalled_ms',0)}ms")
                elif kind == "error":
                    self.status.set("Operation failed: " + value)
                    self.emit("background_error", {"kind": value})
                elif kind == "result":
                    key, result = value
                    if key == "login":
                        self.password.set("")
                        self.token.set("")
                        self.device_display.set(
                            "Logged in as PS3. Diagnostic device id: " + result["device_id"])
                        self.status.set("Connected and authenticated")
                        self.ui.navigate("home")
                    elif key == "home_result":
                        generation, category, data, error = result
                        self.ui.home_result(generation, category, data, error)
                    elif key == "cinema_episodes":
                        generation, series_id, data, error = result
                        self.ui.show_episodes(generation, series_id, data, error)
                    elif key == "browse":
                        generation, payload = result
                        self.ui.show_items(generation, payload)
                    elif key == "items":
                        self.ui.show_items(self.ui.generation, {
                            "Items": result, "TotalRecordCount": len(result)})
                    elif key == "prepared":
                        session, probe, hd = result
                        self.begin_stream(session, probe, hd)
                    elif key == "simulation":
                        self.emit("offline_simulation", result["simulation"])
                        self.status.set("Offline 20-second simulation complete")
                    elif key in ("probe_complete", "probe_error"):
                        self.status.set(str(key))
                        self.stop()
                    elif key == "mpv_command":
                        action, _response = result
                        if action == "pause":
                            self.is_paused = not self.is_paused
                            self.ui.pause_text.set("Resume" if self.is_paused else "Pause")
                        self.emit("mpv_control", {"action": action})
                    elif key == "mpv_time":
                        try:
                            self.position_ticks = max(0, int(float(result or 0) * 10000000))
                        except (ValueError, TypeError, OverflowError):
                            pass
                    elif key == "playstate_error":
                        self.emit("playstate_error", {"type": result})
                    elif key == "playstate":
                        self.emit("playstate_ok", {"action": result})
            if self.player and self.player.poll() is not None:
                self.emit("mpv_exit", {"return_code": self.player.returncode})
                self.stop()
            if self.running and self.session and self.client:
                now = time.monotonic()
                if self.player and self.ipc_path and now - self.last_player_poll > 3:
                    self.last_player_poll = now
                    pipe = self.ipc_path
                    self.worker(lambda: ("mpv_time", ipc_command(
                        pipe, ["get_property", "time-pos"]).get("data", 0)))
                if now - self.last_progress_at > 10:
                    self.last_progress_at = now
                    self.worker(self.send_playstate, self.session, "progress")
        except Exception as ex:
            self.emit("ui_pump_error", {"type": type(ex).__name__})
        finally:
            if self.root.winfo_exists():
                self.root.after(250, self.pump)

    def export(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".jsonl", filetypes=[("JSON Lines", "*.jsonl")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as file:
                for record in self.events:
                    file.write(json.dumps(record, separators=(",", ":")) + "\n")
            self.status.set(f"Exported {len(self.events)} redacted events")
        except OSError as ex:
            messagebox.showerror("Export", type(ex).__name__)

    def export_simulation(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            report = simulate_scenario(self.profile, seconds=30)
            with open(path, "w", encoding="utf-8") as file:
                json.dump(report, file, indent=2)
        except (OSError, ValueError) as ex:
            messagebox.showerror("Simulation", type(ex).__name__)

    def clear(self):
        if messagebox.askyesno("Clear", "Delete local diagnostic history?"):
            self.events.clear()
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")

    def choose_rpc(self):
        p = filedialog.askopenfilename(filetypes=[("Windows executable", "*.exe")])
        if p:
            self.rpc_path.set(p)

    def start_rpc(self):
        p = Path(self.rpc_path.get())
        if not p.is_file():
            messagebox.showwarning("RPCS3", "Select your RPCS3.exe first")
            return
        try:
            subprocess.Popen([str(p)], cwd=str(p.parent))
            self.emit("rpcs3_launched", {"external": True})
        except OSError as ex:
            self.emit("rpcs3_error", {"type": type(ex).__name__})

    def read_rpc(self):
        p = filedialog.askopenfilename(filetypes=[("RPCS3 log", "*.log *.txt")])
        if not p:
            return
        counters = {k: 0 for k in ("rsx", "cellaudio", "vblank", "fifo", "flip", "vdec")}
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as file:
                for line in file:
                    for key in counters:
                        if key in line.lower():
                            counters[key] += 1
            self.emit("rpcs3_log_summary", counters)
        except OSError as ex:
            self.emit("rpcs3_log_error", {"type": type(ex).__name__})

    def close(self):
        try:
            self.stop()
            self.ui.close()
        finally:
            self.root.destroy()


def main():
    root = tk.Tk()
    lab = Ps3Lab(root)
    if "--smoke-test" in sys.argv:
        # Used only by GitHub-hosted Windows CI to validate the full GUI.
        root.after(450, lab.close)
    root.mainloop()


if __name__ == "__main__":
    main()
