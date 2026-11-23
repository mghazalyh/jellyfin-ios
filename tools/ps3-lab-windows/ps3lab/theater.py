"""Jellyfin-like dark media library with poster artwork and embedded playback.

All Tk widgets and images are created on the GUI thread. Remote cover bytes
are fetched on bounded workers. This UI renders real Jellyfin results and
controls a real embedded mpv window, not a mocked video frame.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import tkinter as tk
from tkinter import ttk, messagebox

try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    Image = ImageDraw = ImageTk = None

BG = "#101116"
SIDEBAR = "#171820"
SURFACE = "#20212b"
SURFACE2 = "#292b36"
WHITE = "#f5f5f8"
MUTED = "#a6a7b5"
ACCENT = "#a65cff"
ACCENT_HOVER = "#bd88ff"
CARD = "#242631"
BORDER = "#363743"


def readable_runtime(ticks):
    try:
        secs = int(ticks or 0) // 10000000
        if secs < 0:
            return ""
        return (str(secs // 3600) + ":" if secs >= 3600 else "") + f"{secs // 60 % 60:02}:{secs % 60:02}"
    except (ValueError, TypeError, OverflowError):
        return ""


class TheaterUI:
    NAV = (
        ("home", "Home"),
        ("movies", "Movies"),
        ("shows", "TV Shows"),
        ("episodes", "Episodes"),
        ("live", "Live TV"),
        ("resume", "Continue watching"),
        ("connect", "Server & account"),
        ("diagnostics", "PS3 Lab & RPCS3"),
    )

    def __init__(self, lab, root):
        self.lab = lab
        self.root = root
        self.category = "home"
        self.parent_id = ""
        self.current_title = "Home"
        self.start = 0
        self.limit = 48
        self.total = 0
        self.generation = 0
        self.selected_item = None
        self.cards = {}
        self.photos = {}
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cover")
        self._closed = False
        self.last_items = []
        self.page_buttons = {}
        self.search_text = tk.StringVar()
        self.details_text = tk.StringVar(value="Select a movie, episode, or channel to see its details.")
        self.details_title = tk.StringVar(value="Browse your Jellyfin library")
        self.player_title = tk.StringVar(value="Nothing playing")
        self.pause_text = tk.StringVar(value="Pause")
        self.volume = tk.DoubleVar(value=80)
        self._build()

    def _btn(self, parent, text, command, accent=False, **kwargs):
        return tk.Button(
            parent, text=text, command=command, relief="flat", bd=0,
            bg=ACCENT if accent else SURFACE2,
            activebackground=ACCENT_HOVER if accent else BORDER,
            fg=WHITE, activeforeground=WHITE, cursor="hand2",
            font=("Segoe UI", 10, "bold"), padx=16, pady=9, **kwargs
        )

    def _label(self, parent, text="", size=11, color=WHITE, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"),
                        **kwargs)

    def _build(self):
        root = self.root
        root.title("Jellyfin PS3 Theater")
        root.geometry("1390x875")
        root.minsize(1000, 650)
        root.configure(bg=BG)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Lab.TNotebook", background=BG, borderwidth=0)
        style.configure("Lab.TNotebook.Tab", background=SURFACE, foreground=WHITE,
                        padding=(16, 9), borderwidth=0)
        style.map("Lab.TNotebook.Tab", background=[("selected", ACCENT)])
        style.configure("Lab.TFrame", background=BG)
        style.configure("Lab.TLabel", background=BG, foreground=WHITE)
        style.configure("Lab.TCheckbutton", background=BG, foreground=WHITE)
        style.map("Lab.TCheckbutton", background=[("active", BG)], foreground=[("active", WHITE)])
        style.configure("Lab.TButton", background=SURFACE2, foreground=WHITE)
        style.map("Lab.TButton", background=[("active", ACCENT)])
        style.configure("Lab.TEntry", fieldbackground=SURFACE2, foreground=WHITE)

        outer = tk.Frame(root, bg=BG)
        outer.pack(fill="both", expand=True)
        sidebar = tk.Frame(outer, bg=SIDEBAR, width=225)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        mark = tk.Frame(sidebar, bg=SIDEBAR)
        mark.pack(fill="x", padx=23, pady=(29, 28))
        self._label(mark, "◈", 26, ACCENT, True).pack(side="left", padx=(0, 11))
        word = tk.Frame(mark, bg=SIDEBAR)
        word.pack(side="left")
        self._label(word, "Jellyfin", 19, WHITE, True).pack(anchor="w")
        self._label(word, "PS3 THEATER", 9, ACCENT, True).pack(anchor="w")
        for name, title in self.NAV:
            button = self._btn(sidebar, title, lambda name=name: self.navigate(name))
            button.configure(bg=SIDEBAR, font=("Segoe UI", 11),
                             anchor="w", padx=24, pady=12, width=19)
            button.pack(fill="x", padx=8, pady=2)
            self.page_buttons[name] = button
        tk.Frame(sidebar, bg=SIDEBAR).pack(fill="both", expand=True)
        self._label(sidebar, "PS3 protocol • Native playback", 9, MUTED).pack(
            anchor="w", padx=20, pady=(0, 8))
        self._label(sidebar, "RPCS3 runs separately", 9, MUTED).pack(
            anchor="w", padx=20, pady=(0, 24))

        right = tk.Frame(outer, bg=BG)
        right.pack(side="left", fill="both", expand=True)
        toolbar = tk.Frame(right, bg=BG, height=74)
        toolbar.pack(fill="x", padx=28, pady=(13, 4))
        self.heading = self._label(toolbar, "Home", 20, WHITE, True)
        self.heading.pack(side="left", padx=(0, 25))
        search = tk.Entry(toolbar, textvariable=self.search_text, bg=SURFACE,
                          insertbackground=WHITE, fg=WHITE, relief="flat",
                          font=("Segoe UI", 12), width=31)
        search.pack(side="right", ipady=9, padx=(9, 0))
        search.bind("<Return>", lambda _event: self.search())
        self._btn(toolbar, "Search", self.search).pack(side="right")
        self.workspace = tk.Frame(right, bg=BG)
        self.workspace.pack(fill="both", expand=True, padx=26, pady=6)
        self.workspace.grid_rowconfigure(0, weight=1)
        self.workspace.grid_columnconfigure(0, weight=1)
        self.pages = {}
        for key in ("library", "player", "connect", "diagnostics"):
            page = tk.Frame(self.workspace, bg=BG)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = page

        self._library_page(self.pages["library"])
        self._player_page(self.pages["player"])
        self._connect_page(self.pages["connect"])
        self._diagnostics_page(self.pages["diagnostics"])

        statusbar = tk.Frame(right, bg=SURFACE, height=38)
        statusbar.pack(side="bottom", fill="x")
        tk.Label(statusbar, textvariable=self.lab.status, bg=SURFACE,
                 fg=MUTED, anchor="w", font=("Segoe UI", 9)).pack(
                     fill="x", padx=16, pady=8)
        self.activate("library")
        self._nav_highlight("home")

    def _library_page(self, parent):
        hero = tk.Frame(parent, bg=SURFACE, height=163)
        hero.pack(fill="x", pady=(0, 15))
        hero.pack_propagate(False)
        info = tk.Frame(hero, bg=SURFACE)
        info.pack(side="left", fill="both", expand=True, padx=22, pady=19)
        self._label(info, "YOUR MEDIA LIBRARY", 9, ACCENT, True).pack(anchor="w")
        self._label(info, textvariable=self.details_title, size=19, bold=True,
                    wraplength=690, justify="left").pack(anchor="w", pady=3)
        self._label(info, textvariable=self.details_text, size=10, color=MUTED,
                    wraplength=740, justify="left").pack(anchor="w")
        action = tk.Frame(hero, bg=SURFACE)
        action.pack(side="right", padx=22, pady=19)
        self._btn(action, "▶  Play selected", self.play_selected, True).pack(pady=(20, 6))
        self._btn(action, "Inspect 20s", self.probe_selected).pack(fill="x")

        actions = tk.Frame(parent, bg=BG)
        actions.pack(fill="x", pady=(4, 12))
        self.section_title = self._label(actions, "Recently added", 16, WHITE, True)
        self.section_title.pack(side="left")
        self.count_label = self._label(actions, "", 10, MUTED)
        self.count_label.pack(side="left", padx=14)
        self._btn(actions, "Next ›", lambda: self.page(1)).pack(side="right")
        self._btn(actions, "‹ Previous", lambda: self.page(-1)).pack(
            side="right", padx=(0, 7))

        host = tk.Frame(parent, bg=BG)
        host.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(host, bg=BG, highlightthickness=0)
        bar = ttk.Scrollbar(host, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.gridhost = tk.Frame(self.canvas, bg=BG)
        self.canvas_window = self.canvas.create_window(
            (0, 0), anchor="nw", window=self.gridhost)
        self.gridhost.bind("<Configure>", self._resize_scroll)
        self.canvas.bind("<Configure>", self._resize_canvas)
        self.canvas.bind("<MouseWheel>", self._wheel)

    def _resize_scroll(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_canvas(self, event):
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def _wheel(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def _player_page(self, parent):
        titlebar = tk.Frame(parent, bg=BG)
        titlebar.pack(fill="x", pady=(3, 13))
        self._btn(titlebar, "‹ Back to library", self.back_to_library).pack(side="left")
        self._label(titlebar, textvariable=self.player_title, size=16, bold=True).pack(
            side="left", padx=19)
        self._btn(titlebar, "Fullscreen", self.fullscreen).pack(side="right")

        theater = tk.Frame(parent, bg="#000000", highlightbackground=BORDER,
                           highlightthickness=1)
        theater.pack(fill="both", expand=True)
        theater.grid_rowconfigure(0, weight=1)
        theater.grid_columnconfigure(0, weight=1)
        self.video_host = tk.Frame(theater, bg="#000000")
        self.video_host.grid(row=0, column=0, sticky="nsew")
        self.video_host.update_idletasks()

        controls = tk.Frame(parent, bg=SURFACE)
        controls.pack(fill="x", pady=(10, 3))
        self._btn(controls, "◀ 10s", lambda: self.lab.player_command("back")).pack(
            side="left", padx=(9, 3), pady=8)
        self._btn(controls, "▶ / ❚❚", self.toggle_pause, True).pack(
            side="left", padx=3)
        self._btn(controls, "30s ▶", lambda: self.lab.player_command("forward")).pack(
            side="left", padx=3)
        self._btn(controls, "Stop ■", self.lab.stop).pack(side="left", padx=3)
        self._label(controls, "Volume", 10, MUTED).pack(side="left", padx=(25, 4))
        tk.Scale(controls, from_=0, to=100, orient="horizontal", length=150,
                 variable=self.volume, command=self.change_volume, bg=SURFACE,
                 fg=WHITE, highlightthickness=0, troughcolor=BORDER,
                 showvalue=False).pack(side="left")
        self._btn(controls, "Diagnostics", lambda: self.navigate("diagnostics")).pack(
            side="right", padx=9)
        subtitle = self._label(parent, textvariable=self.lab.summary, size=10, color=MUTED)
        subtitle.pack(anchor="w", pady=8)

    def _entry(self, parent, label, variable, hidden=False):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=8)
        self._label(row, label, 11, MUTED).pack(anchor="w", pady=(0, 5))
        box = tk.Entry(row, textvariable=variable, bg=SURFACE2, fg=WHITE,
                       insertbackground=WHITE, relief="flat",
                       font=("Segoe UI", 12), show="*" if hidden else "")
        box.pack(fill="x", ipady=9)
        return box

    def _connect_page(self, parent):
        pane = tk.Frame(parent, bg=BG)
        pane.pack(anchor="n", fill="x", padx=50, pady=30)
        self._label(pane, "Connect to Jellyfin", 24, WHITE, True).pack(anchor="w")
        self._label(pane, "Play real media with PS3 identity, without changing your physical console.",
                    11, MUTED).pack(anchor="w", pady=(6, 20))
        self._entry(pane, "Jellyfin server address", self.lab.server)
        self._entry(pane, "Username", self.lab.username)
        self._entry(pane, "Password", self.lab.password, True)
        self._entry(pane, "Access token (alternative to username/password)", self.lab.token, True)
        self._entry(pane, "Surf CatalogService URL (optional)", self.lab.catalog)
        buttons = tk.Frame(pane, bg=BG)
        buttons.pack(fill="x", pady=16)
        self._btn(buttons, "Sign in", self.lab.login, True).pack(side="left")
        self._btn(buttons, "Connect using token", self.lab.login_token).pack(
            side="left", padx=10)
        self._label(pane, textvariable=self.lab.device_display, size=10,
                    color=MUTED, wraplength=770, justify="left").pack(anchor="w", pady=9)

    def _diagnostics_page(self, parent):
        self._label(parent, "PS3 diagnostics, constraints and actual RPCS3",
                    size=19, bold=True).pack(anchor="w", pady=12)
        self._label(parent,
                    "The player uses real mpv; the resource controls are simulations. "
                    "Actual PPU/RSX instruction execution requires RPCS3.",
                    size=10, color=MUTED, wraplength=880, justify="left").pack(
                        anchor="w", pady=(0, 12))
        tabs = ttk.Notebook(parent, style="Lab.TNotebook")
        tabs.pack(fill="both", expand=True)
        stress = ttk.Frame(tabs, style="Lab.TFrame", padding=12)
        evidence = ttk.Frame(tabs, style="Lab.TFrame", padding=12)
        rpcs3 = ttk.Frame(tabs, style="Lab.TFrame", padding=12)
        for tab, text in ((stress, "CPU / RSX / Wireless"),
                          (evidence, "Transport evidence"), (rpcs3, "RPCS3")):
            tabs.add(tab, text=text)
        self.tabs = tabs
        self.lab._limits_tab(stress)
        self.lab._evidence_tab(evidence)
        self.lab._rpc_tab(rpcs3)

    def activate(self, key):
        self.pages[key].tkraise()

    def _nav_highlight(self, selected):
        for name, button in self.page_buttons.items():
            button.configure(bg=ACCENT if name == selected else SIDEBAR)

    def navigate(self, category, parent_id="", title=""):
        if category == "connect":
            self.heading.configure(text="Server & account")
            self._nav_highlight("connect")
            self.activate("connect")
            return
        if category == "diagnostics":
            self.heading.configure(text="PS3 Lab & RPCS3")
            self._nav_highlight("diagnostics")
            self.activate("diagnostics")
            return
        self.category = category
        self.parent_id = parent_id
        self.current_title = title or next(
            (label for key, label in self.NAV if key == category), "Episodes")
        self.start = 0
        self.heading.configure(text=self.current_title)
        self.section_title.configure(text=self.current_title)
        self._nav_highlight(category if category in self.page_buttons else "episodes")
        self.activate("library")
        if self.lab.client:
            self.load()
        else:
            self.lab.status.set("Connect to your Jellyfin server to load the library")

    def search(self):
        if not self.lab.client:
            self.navigate("connect")
            return
        self.start = 0
        self.load()

    def page(self, step):
        target = max(0, self.start + step * self.limit)
        if target == self.start or (step > 0 and self.total <= self.start + self.limit):
            return
        self.start = target
        self.load()

    def load(self):
        if not self.lab.client:
            return
        self.generation += 1
        generation = self.generation
        self.lab.status.set("Loading " + self.current_title + "…")
        self.lab.worker(
            lambda: ("browse", (generation, self.lab.client.browse(
                self.category, start=self.start, limit=self.limit,
                search=self.search_text.get().strip(), parent_id=self.parent_id)))
        )

    def show_items(self, generation, payload):
        if self._closed or generation != self.generation:
            return
        self.last_items = [x for x in (payload.get("Items") or []) if x.get("Id")]
        self.lab.items = self.last_items
        self.total = int(payload.get("TotalRecordCount", len(self.last_items)))
        self.count_label.configure(
            text=f"{len(self.last_items)} shown · {self.total} total")
        self._clear_grid()
        self.photos.clear()
        self.cards.clear()
        self.selected_item = None
        self.details_title.set(self.current_title)
        self.details_text.set("Choose an item to see details or start playback.")
        if not self.last_items:
            self._label(self.gridhost,
                        "No media matched this selection. Choose another section or search.",
                        13, MUTED, wraplength=600).pack(anchor="w", padx=12, pady=40)
        for idx, item in enumerate(self.last_items):
            self._make_card(idx, item, generation)
        self.canvas.yview_moveto(0)
        self.lab.status.set(f"Loaded {len(self.last_items)} items from Jellyfin")

    def _clear_grid(self):
        for child in self.gridhost.winfo_children():
            child.destroy()

    def _make_card(self, idx, item, generation):
        col_count = max(3, min(6, (self.canvas.winfo_width() or 1040) // 174))
        card = tk.Frame(self.gridhost, bg=CARD, width=155)
        card.grid(row=idx // col_count, column=idx % col_count,
                  padx=9, pady=10, sticky="n")
        card.grid_propagate(False)
        card.configure(height=288)
        poster = tk.Label(card, bg=SURFACE2, fg=MUTED, width=21, height=13,
                          text="◈\n" + (item.get("Type") or "Media"),
                          font=("Segoe UI", 13, "bold"), cursor="hand2")
        poster.pack(fill="x", padx=3, pady=(3, 4))
        title = (item.get("Name") or "Untitled").strip()
        tk.Label(card, text=title, bg=CARD, fg=WHITE, font=("Segoe UI", 10, "bold"),
                 width=20, anchor="w").pack(fill="x", padx=5)
        runtime = readable_runtime(item.get("RunTimeTicks"))
        year = str(item.get("ProductionYear") or "")
        kind = item.get("Type") or "Media"
        tk.Label(card, text=" · ".join(x for x in (year, runtime, kind) if x),
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8), anchor="w", width=23).pack(
                     fill="x", padx=5, pady=4)
        for widget in (card, poster):
            widget.bind("<Button-1>", lambda _e, i=idx: self.choose(i))
            widget.bind("<Double-Button-1>", lambda _e, i=idx: self.quick_play(i))
        self.cards[idx] = poster
        tags = item.get("ImageTags") or {}
        if tags.get("Primary") or item.get("PrimaryImageItemId") or item.get("SeriesPrimaryImageTag"):
            self.executor.submit(self._fetch_cover, generation, idx, item)

    def _fetch_cover(self, generation, idx, item):
        try:
            if self._closed or not self.lab.client:
                return
            data = self.lab.client.image_bytes(item)
            if data:
                self.lab.dispatch("artwork", (generation, idx, data))
        except Exception as exc:
            self.lab.emit("image_error", {"kind": type(exc).__name__})

    def set_artwork(self, generation, idx, data):
        if (self._closed or generation != self.generation or not Image or
                idx not in self.cards):
            return
        try:
            image = Image.open(BytesIO(data))
            image.thumbnail((148, 211), Image.Resampling.LANCZOS)
            image = image.convert("RGB")
            if image.size != (148, 211):
                back = Image.new("RGB", (148, 211), SURFACE2)
                back.paste(image, ((148 - image.width) // 2, (211 - image.height) // 2))
                image = back
            photo = ImageTk.PhotoImage(image, master=self.root)
            self.photos[idx] = photo
            poster = self.cards[idx]
            poster.configure(image=photo, text="", width=148, height=211)
        except (OSError, ValueError, RuntimeError) as exc:
            self.lab.emit("artwork_decode_error", {"kind": type(exc).__name__})

    def choose(self, idx):
        if idx < 0 or idx >= len(self.last_items):
            return
        item = self.last_items[idx]
        self.selected_item = item
        self.details_title.set(item.get("Name") or "Untitled")
        text = (item.get("Overview") or "").strip().replace("\n", " ")
        if not text:
            text = (item.get("Type") or "Media") + " · " + (
                readable_runtime(item.get("RunTimeTicks")) or "No duration")
        self.details_text.set(text[:260])
        self.lab.status.set("Selected " + (item.get("Name") or "item"))

    def quick_play(self, idx):
        self.choose(idx)
        self.play_selected()

    def play_selected(self):
        item = self.selected_item
        if not item:
            messagebox.showinfo("Jellyfin", "Select a media card first.")
            return
        if item.get("Type") == "Series":
            self.navigate("episodes", parent_id=item["Id"],
                          title=item.get("Name") or "Episodes")
            return
        self.lab.begin(False)

    def probe_selected(self):
        if self.selected_item:
            self.lab.begin(True)
        else:
            messagebox.showinfo("Jellyfin", "Select an item to probe.")

    def show_player(self, item):
        self.player_title.set(item.get("Name") or "Now playing")
        self.heading.configure(text="Now playing")
        self.pause_text.set("Pause")
        self.activate("player")
        self.video_host.update_idletasks()
        return int(self.video_host.winfo_id())

    def toggle_pause(self):
        self.lab.player_command("pause")

    def change_volume(self, value):
        if self.lab.player and self.lab.player.poll() is None:
            self.lab.player_command("volume", value)

    def fullscreen(self):
        self.root.attributes("-fullscreen", not bool(self.root.attributes("-fullscreen")))

    def back_to_library(self):
        if self.root.attributes("-fullscreen"):
            self.root.attributes("-fullscreen", False)
        self.heading.configure(text=self.current_title)
        self.activate("library")

    def close(self):
        self._closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
