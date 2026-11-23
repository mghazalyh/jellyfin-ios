"""Cinematic Jellyfin client inspired by the layout patterns of Nuvio and Stremio.

Actual media, posters, backdrops, playback state and episode lists are sourced
from Jellyfin. PS3 timing simulation and RPCS3 remain in the laboratory panel.
Tk and PhotoImage work is confined to the UI thread.
"""
from __future__ import annotations

from io import BytesIO
import tkinter as tk
from tkinter import messagebox, ttk

from .theater import (
    TheaterUI, BG, SIDEBAR, SURFACE, SURFACE2, WHITE, MUTED, ACCENT, BORDER,
    Image, ImageDraw, ImageTk, readable_runtime,
)
from .parity import QUALITY_ORDER

HERO_BG = "#12131c"
PANEL = "#1b1d28"


class CinematicUI(TheaterUI):
    NAV = (
        ("home", "⌂     Home"),
        ("movies", "▣     Movies"),
        ("shows", "▤     TV Series"),
        ("live", "◉     Live TV"),
        ("resume", "▶     Continue Watching"),
        ("connect", "⚙     Server & Account"),
        ("diagnostics", "◇     PS3 Lab / RPCS3"),
    )

    def _build(self):
        self.home_sections = {}
        self.home_pending = set()
        self.featured_item = None
        self.detail_item = None
        self.hero_photo = None
        self.detail_photo = None
        self.home_generation = 0
        self.detail_generation = 0
        self.current_view = "home"
        self.episodes = []
        self.selected_source_id = ""
        self.source_buttons = []
        self.quality_choice = tk.StringVar(value="auto")
        self.no_video_copy = tk.BooleanVar(value=False)
        self.surround_hd = tk.BooleanVar(value=False)
        self.ac3_passthrough = tk.BooleanVar(value=False)
        self.audio_stream_index = -1
        self.subtitle_stream_index = -1
        self.selected_audio_codec = ""
        self.selected_audio_bitrate = 0
        self.sources = []
        self.home_scroll_width = 1040
        super()._build()
        self._build_home()
        self._build_details()
        self.activate("home")
        self.heading.configure(text="Home")
        self._nav_highlight("home")
        self.root.bind("<Escape>", self._escape)
        self.root.bind("<Control-f>", self._focus_search)
        self.root.bind("<F11>", lambda _event: self.fullscreen())

    def _escape(self, _event):
        if self.root.attributes("-fullscreen"):
            self.fullscreen()
        elif self.current_view == "detail":
            self.back_to_library()
        elif self.current_view == "player":
            self.back_to_library()

    def _focus_search(self, _event):
        self.search_entry.focus_set()
        return "break"

    def _player_page(self, parent):
        """Playback stays embedded in the same cinematic desktop window."""
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", pady=(2, 11))
        self._btn(top, "‹  Library", self.back_to_library).pack(side="left")
        self._label(top, textvariable=self.player_title,
                    size=15, bold=True).pack(side="left", padx=19)
        self._btn(top, "Fullscreen  ⛶", self.fullscreen).pack(side="right")

        screen = tk.Frame(parent, bg="#000000", highlightbackground=BORDER,
                          highlightthickness=1)
        screen.pack(fill="both", expand=True)
        self.video_host = tk.Frame(screen, bg="#000000")
        self.video_host.pack(fill="both", expand=True)
        self.video_host.update_idletasks()

        bottom = tk.Frame(parent, bg=SURFACE, padx=15, pady=8)
        bottom.pack(fill="x", pady=(9, 0))
        timeline = tk.Frame(bottom, bg=SURFACE)
        timeline.pack(fill="x")
        self.time_left = tk.StringVar(value="00:00")
        self.time_right = tk.StringVar(value="00:00")
        self._label(timeline, textvariable=self.time_left, size=10, color=MUTED).pack(
            side="left", padx=(0, 7))
        self.progress = tk.DoubleVar(value=0)
        self._dragging = False
        self.timeline = tk.Scale(
            timeline, from_=0, to=1, resolution=1, variable=self.progress,
            orient="horizontal", showvalue=False, highlightthickness=0,
            bg=SURFACE, fg=WHITE, troughcolor="#555368", bd=0,
            activebackground=ACCENT, sliderrelief="flat")
        self.timeline.pack(side="left", fill="x", expand=True)
        self.timeline.bind("<ButtonPress-1>", self._scrub_start)
        self.timeline.bind("<ButtonRelease-1>", self._scrub_end)
        self._label(timeline, textvariable=self.time_right, size=10, color=MUTED).pack(
            side="left", padx=(7, 0))
        controls = tk.Frame(bottom, bg=SURFACE)
        controls.pack(fill="x", pady=(4, 0))
        self._btn(controls, "↶ 10s", lambda: self.lab.player_command("back")).pack(
            side="left", padx=(0, 5))
        self._btn(controls, "▶ / ❚❚", lambda: self.lab.player_command("pause"), True).pack(
            side="left", padx=(0, 5))
        self._btn(controls, "30s ↷", lambda: self.lab.player_command("forward")).pack(
            side="left", padx=(0, 5))
        self._btn(controls, "■  Stop", self.lab.stop).pack(side="left")
        self._label(controls, "Volume", 10, MUTED).pack(side="left", padx=(24, 9))
        tk.Scale(controls, from_=0, to=100, orient="horizontal",
                 length=125, variable=self.volume, command=self.change_volume,
                 bg=SURFACE, fg=WHITE, showvalue=False,
                 highlightthickness=0, troughcolor=BORDER).pack(side="left")
        self._btn(controls, "PS3 Diagnostics",
                  lambda: self.navigate("diagnostics")).pack(side="right")
        self._label(parent, textvariable=self.lab.summary, size=9, color=MUTED).pack(
            anchor="w", pady=(6, 2))

    def _scrub_start(self, _event):
        self._dragging = True

    def _scrub_end(self, _event):
        self._dragging = False
        self.lab.seek_to_seconds(self.progress.get())

    def update_playback_position(self, position_ticks, duration_ticks):
        try:
            position = max(0, float(position_ticks or 0) / 10000000.0)
            duration = max(0, float(duration_ticks or 0) / 10000000.0)
        except (TypeError, ValueError, OverflowError):
            return
        self.time_left.set(self._clock(position))
        self.time_right.set(self._clock(duration))
        self.timeline.configure(to=max(1, int(duration)))
        if not self._dragging:
            self.progress.set(min(position, duration) if duration else position)

    @staticmethod
    def _clock(seconds):
        total = max(0, int(seconds or 0))
        return f"{total // 3600:02}:{total // 60 % 60:02}:{total % 60:02}" if total >= 3600 else (
            f"{total // 60:02}:{total % 60:02}")

    def _build_home(self):
        page = tk.Frame(self.workspace, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages["home"] = page
        canvas = tk.Canvas(page, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(page, orient="vertical", command=canvas.yview,
                                 bg=SURFACE2, troughcolor=BG)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        body = tk.Frame(canvas, bg=BG)
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: (
            canvas.itemconfigure(window_id, width=e.width),
            setattr(self, "home_scroll_width", e.width),
        ))
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(
            -int(e.delta / 120), "units"))
        self.home_canvas = canvas
        self.hero = tk.Canvas(body, bg=HERO_BG, height=355,
                              highlightthickness=0)
        self.hero.pack(fill="x", padx=4, pady=(3, 22))
        self.hero_ids = self._hero_contents(self.hero, detail=False)
        self.hero.bind("<Configure>", lambda _e: self._hero_geometry(self.hero_ids))
        self.shelves = tk.Frame(body, bg=BG)
        self.shelves.pack(fill="x", pady=(0, 26))
        self._label(self.shelves,
                    "Connect to Jellyfin to explore your movies, shows and Live TV.",
                    12, MUTED).pack(anchor="w", padx=14, pady=22)

    def _hero_contents(self, canvas, detail=False):
        tagline = "JELLYFIN · PS3 STREAM" if not detail else "NOW AVAILABLE ON JELLYFIN"
        title = canvas.create_text(34, 86, text="Your collection. Your screen.",
                                   anchor="nw", fill=WHITE,
                                   font=("Segoe UI", 30, "bold"), width=670)
        eyebrow = canvas.create_text(34, 56, text=tagline, anchor="nw",
                                     fill=ACCENT, font=("Segoe UI", 10, "bold"))
        metadata = canvas.create_text(36, 164, text="", anchor="nw",
                                      fill="#d3d3de", font=("Segoe UI", 11))
        overview = canvas.create_text(35, 193,
                                     text="Sign in to start browsing your Jellyfin server.",
                                     anchor="nw", width=625, fill="#c8c8d2",
                                     font=("Segoe UI", 11))
        buttons = tk.Frame(canvas, bg=HERO_BG)
        first = self._btn(buttons, "▶  Watch now",
                          self.play_detail if detail else self.play_featured, True)
        first.pack(side="left")
        second = self._btn(buttons, "Back to library" if detail else "More info",
                           self.back_to_library if detail else self.open_featured)
        second.pack(side="left", padx=10)
        controls = canvas.create_window(35, 297, window=buttons, anchor="nw")
        return {
            "title": title, "eyebrow": eyebrow, "metadata": metadata,
            "overview": overview, "buttons": controls, "play": first
        }

    @staticmethod
    def _hero_geometry(ids):
        # Canvas elements are anchored in a stable safe area so resizing never
        # overlaps the artwork's main focal point at the right of the screen.
        return ids

    def _set_hero(self, ids, item, detail=False):
        canvas = self.detail_hero if detail else self.hero
        if not item:
            canvas.itemconfigure(ids["title"], text="Your collection. Your screen.")
            canvas.itemconfigure(ids["metadata"], text="")
            canvas.itemconfigure(ids["overview"],
                                 text="Sign in to browse films, series and live channels.")
            return
        canvas.itemconfigure(ids["title"], text=(item.get("Name") or "Untitled")[:75])
        canvas.itemconfigure(ids["metadata"], text=self._metadata(item)[:110])
        overview = (item.get("Overview") or "Ready to stream from your Jellyfin server.")
        canvas.itemconfigure(ids["overview"], text=overview.strip()[:250])
        if detail:
            ids["play"].configure(
                text="Browse episodes" if item.get("Type") == "Series" else "▶  Play now")

    def _metadata(self, item):
        values = []
        if item.get("ProductionYear"):
            values.append(str(item["ProductionYear"]))
        if item.get("OfficialRating"):
            values.append(str(item["OfficialRating"]))
        if item.get("RunTimeTicks"):
            values.append(readable_runtime(item.get("RunTimeTicks")))
        values.extend(str(x) for x in (item.get("Genres") or [])[:2])
        values.append(str(item.get("Type") or "Media"))
        return "   •   ".join(x for x in values if x)

    def _build_details(self):
        page = tk.Frame(self.workspace, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages["detail"] = page
        self.detail_hero = tk.Canvas(page, bg=HERO_BG, height=345,
                                     highlightthickness=0)
        self.detail_hero.pack(fill="x", padx=4, pady=(4, 18))
        self.detail_ids = self._hero_contents(self.detail_hero, detail=True)
        quality_row = tk.Frame(page, bg=BG)
        quality_row.pack(fill="x", padx=10, pady=(0, 11))
        self._label(quality_row, "PS3 playback profile", 11, MUTED, True).pack(
            side="left", padx=(0, 12))
        self._label(quality_row, "Quality", 10, MUTED).pack(
            side="left", padx=(4, 8))
        self.quality_combo = ttk.Combobox(quality_row, textvariable=self.quality_choice,
                                           values=list(QUALITY_ORDER), width=12,
                                           state="readonly")
        self.quality_combo.pack(side="left", padx=(0, 12))
        for text, var in (("Auto 1080p", self.lab.hd),
                          ("AC-3 5.1", self.lab.surround),
                          ("Surround HD", self.surround_hd),
                          ("AC-3 passthrough", self.ac3_passthrough),
                          ("No video copy", self.no_video_copy)):
            tk.Checkbutton(
                quality_row, text=text, variable=var, bg=BG, fg=WHITE,
                activebackground=BG, activeforeground=WHITE,
                selectcolor=SURFACE2, font=("Segoe UI", 10),
            ).pack(side="left", padx=11)
        self.episode_header = tk.Frame(page, bg=BG)
        self.episode_header.pack(fill="x", padx=12, pady=(8, 4))
        self._label(self.episode_header, "Episodes and media", 17, WHITE, True).pack(
            side="left")
        self.source_panel = tk.Frame(page, bg=BG)
        self.source_panel.pack(fill="x", padx=12, pady=(0, 6))
        track_row = tk.Frame(page, bg=BG)
        track_row.pack(fill="x", padx=12, pady=(0, 7))
        self._label(track_row, "Audio", 10, MUTED, True).pack(side="left")
        self.audio_choice = tk.StringVar(value="Default")
        self.audio_options = [("Default", -1, "", 0)]
        self.audio_combo = ttk.Combobox(track_row, textvariable=self.audio_choice,
                                         values=["Default"], state="readonly", width=26)
        self.audio_combo.pack(side="left", padx=(9, 25))
        self.audio_combo.bind("<<ComboboxSelected>>",
                              lambda _e: self._update_selected_tracks())
        self._label(track_row, "Subtitles", 10, MUTED, True).pack(side="left")
        self.subtitle_choice = tk.StringVar(value="Off")
        self.subtitle_options = [("Off", -1)]
        self.subtitle_combo = ttk.Combobox(track_row, textvariable=self.subtitle_choice,
                                            values=["Off"], state="readonly", width=25)
        self.subtitle_combo.pack(side="left", padx=9)
        self.subtitle_combo.bind("<<ComboboxSelected>>",
                                 lambda _e: self._update_selected_tracks())
        self.detail_content = tk.Frame(page, bg=BG)
        self.detail_content.pack(fill="both", expand=True, padx=12)
        self._label(self.detail_content, "Select a title to explore.",
                    11, MUTED).pack(anchor="w", pady=18)

    def navigate(self, category, parent_id="", title=""):
        self.current_view = category
        if category == "home":
            self.category = "home"
            self.current_title = "Home"
            self.heading.configure(text="Home")
            self._nav_highlight("home")
            self.activate("home")
            if self.lab.client:
                self.load_home()
            return
        if category in ("connect", "diagnostics"):
            return super().navigate(category, parent_id=parent_id, title=title)
        self.current_view = "library"
        return super().navigate(category, parent_id=parent_id, title=title)

    def _nav_highlight(self, selected):
        for name, button in self.page_buttons.items():
            button.configure(bg=ACCENT if name == selected else SIDEBAR)

    def search(self):
        if not self.lab.client:
            self.navigate("connect")
            return
        query = self.search_text.get().strip()
        if not query:
            self.navigate("home")
            return
        self.category = "search"
        self.current_title = "Search results"
        self.current_view = "library"
        self.parent_id = ""
        self.start = 0
        self.heading.configure(text='Results for "' + query[:50] + '"')
        self.section_title.configure(text="Search results")
        self.activate("library")
        self.load()

    def load_home(self):
        if not self.lab.client or self._closed:
            return
        self.generation += 1
        generation = self.generation
        self.home_generation = generation
        self.home_sections.clear()
        self.home_pending = {"resume", "home", "movies", "shows", "live"}
        self.cards.clear()
        self.photos.clear()
        for child in self.shelves.winfo_children():
            child.destroy()
        self._label(self.shelves, "Loading your Jellyfin library…", 11, MUTED).pack(
            anchor="w", padx=14, pady=23)
        for category in ("resume", "home", "movies", "shows", "live"):
            def fetch(cat=category):
                try:
                    return ("home_result", (generation, cat,
                            self.lab.client.browse(cat, limit=16), ""))
                except Exception as exc:
                    return ("home_result", (generation, cat, {"Items": []},
                                            type(exc).__name__))
            self.lab.worker(fetch)
        self.lab.status.set("Loading media from Jellyfin")

    def home_result(self, generation, category, data, error):
        if self._closed or generation != self.home_generation:
            return
        self.home_pending.discard(category)
        self.home_sections[category] = [
            item for item in (data.get("Items") or []) if isinstance(item, dict)
            and item.get("Id")]
        if error:
            self.lab.emit("home_section_error",
                          {"section": category, "kind": error})
        if not self.home_pending:
            self.render_home()

    def render_home(self):
        generation = self.home_generation
        for child in self.shelves.winfo_children():
            child.destroy()
        self.cards.clear()
        self.photos.clear()
        self.featured_item = next(
            (item for category in ("resume", "home", "movies", "shows")
             for item in self.home_sections.get(category, [])
             if item.get("Type") in ("Movie", "Episode", "Series")), None)
        self._set_hero(self.hero_ids, self.featured_item)
        if self.featured_item:
            self._request_backdrop(generation, "hero", self.featured_item)
        headings = (
            ("resume", "Continue Watching"),
            ("home", "Recently Added"),
            ("movies", "Movies"),
            ("shows", "TV Series"),
            ("live", "Live TV"),
        )
        row_count = 0
        for category, title in headings:
            items = self.home_sections.get(category, [])
            if items:
                row_count += 1
                self._rail(generation, category, title, items)
        if not row_count:
            self._label(self.shelves,
                        "No media returned. Check your server libraries or permissions.",
                        11, MUTED).pack(anchor="w", padx=16, pady=25)
        self.lab.status.set(f"Loaded {row_count} library rows from Jellyfin")

    def _rail(self, generation, category, title, items):
        section = tk.Frame(self.shelves, bg=BG)
        section.pack(fill="x", pady=(0, 16))
        bar = tk.Frame(section, bg=BG)
        bar.pack(fill="x", padx=12, pady=(0, 9))
        self._label(bar, title, 17, WHITE, True).pack(side="left")
        target = "recent" if category == "home" else category
        self._btn(bar, "View all", lambda c=target: self.navigate(c)).pack(
            side="right", padx=4)
        strip = tk.Canvas(section, bg=BG, height=256, highlightthickness=0)
        strip.pack(fill="x", padx=3)
        contents = tk.Frame(strip, bg=BG)
        strip.create_window((0, 0), window=contents, anchor="nw")
        contents.bind("<Configure>", lambda _e, c=strip: c.configure(
            scrollregion=c.bbox("all")))
        strip.bind("<MouseWheel>", lambda e, c=strip: c.xview_scroll(
            -int(e.delta / 120), "units"))
        self._btn(bar, "›", lambda c=strip: c.xview_moveto(
            min(1.0, c.xview()[0] + 0.55))).pack(side="right", padx=3)
        self._btn(bar, "‹", lambda c=strip: c.xview_moveto(
            max(0.0, c.xview()[0] - 0.55))).pack(side="right", padx=3)
        for index, item in enumerate(items):
            self._rail_card(contents, item, f"{category}:{index}", generation)

    def _rail_card(self, parent, item, key, generation):
        card = tk.Frame(parent, bg=BG, width=157, height=253)
        card.pack(side="left", padx=7)
        card.pack_propagate(False)
        poster = tk.Label(card, bg=SURFACE2, fg=MUTED, text="◈\n" +
                          (item.get("Type") or "Media"), width=19, height=12,
                          font=("Segoe UI", 11, "bold"), cursor="hand2")
        poster.pack(fill="x")
        title = tk.Label(card, text=(item.get("Name") or "Untitled")[:29],
                         bg=BG, fg=WHITE, anchor="w",
                         font=("Segoe UI", 10, "bold"))
        title.pack(fill="x", pady=(6, 2))
        tk.Label(card, text=str(item.get("ProductionYear") or item.get("Type") or ""),
                 bg=BG, fg=MUTED, anchor="w", font=("Segoe UI", 9)).pack(fill="x")
        progress = (item.get("UserData") or {}).get("PlayedPercentage", 0) or 0
        if progress > 0 and progress < 99:
            meter = tk.Frame(card, bg=SURFACE2, height=3)
            meter.pack(fill="x", side="bottom", pady=3)
            meter.pack_propagate(False)
            fill = tk.Frame(meter, bg=ACCENT, width=max(1, int(145 *
                              min(100, float(progress)) / 100)))
            fill.pack(side="left", fill="y")
        for widget in (poster, title):
            widget.bind("<Button-1>", lambda _event, x=item: self.show_details(x))
            widget.bind("<Double-Button-1>",
                        lambda _event, x=item: self._play_item(x))
        self.cards[key] = poster
        if (item.get("ImageTags") or {}).get("Primary") or item.get("PrimaryImageItemId"):
            self.executor.submit(self._fetch_cover, generation, key, item)

    def _play_item(self, item):
        if item.get("Type") == "Series":
            self.show_details(item)
            return
        self.selected_item = item
        self.lab.begin(False)

    def play_featured(self):
        if self.featured_item:
            self._play_item(self.featured_item)
        else:
            self.navigate("connect")

    def open_featured(self):
        if self.featured_item:
            self.show_details(self.featured_item)
        else:
            self.navigate("connect")

    def choose(self, idx):
        if idx < 0 or idx >= len(self.last_items):
            return
        self.show_details(self.last_items[idx])

    def quick_play(self, idx):
        if 0 <= idx < len(self.last_items):
            self._play_item(self.last_items[idx])

    def show_details(self, item):
        if not isinstance(item, dict) or not item.get("Id"):
            return
        self.detail_generation += 1
        generation = self.detail_generation
        self.detail_item = item
        self.selected_item = item
        self.selected_source_id = ""
        self.sources = []
        self._reset_tracks()
        for child in self.source_panel.winfo_children():
            child.destroy()
        self.current_view = "detail"
        self._set_hero(self.detail_ids, item, detail=True)
        self.heading.configure(text="Title details")
        for child in self.detail_content.winfo_children():
            child.destroy()
        self._label(self.detail_content, "Episodes" if item.get("Type") == "Series"
                    else "Playback", 17, WHITE, True).pack(anchor="w")
        if item.get("Type") == "Series":
            self._label(self.detail_content, "Loading episodes…", 11, MUTED).pack(
                anchor="w", pady=9)
            series_id = item["Id"]
            def load_episodes():
                try:
                    return ("cinema_episodes", (generation, series_id,
                            self.lab.client.series_episodes(series_id), ""))
                except Exception as exc:
                    return ("cinema_episodes", (generation, series_id,
                                               {"Items": []}, type(exc).__name__))
            self.lab.worker(load_episodes)
        else:
            self._label(self.detail_content,
                        "Play with a PS3-compatible H.264 transport stream.",
                        11, MUTED).pack(anchor="w", pady=(8, 14))
            self._btn(self.detail_content, "▶   Start watching", self.play_detail, True).pack(
                anchor="w")
        self.activate("detail")
        self._request_backdrop(self.generation, "detail", item)
        if item.get("Type") in ("Movie", "Episode"):
            item_id = item["Id"]
            def fetch_sources():
                try:
                    details = self.lab.client.item_details(item_id)
                    return ("cinema_sources", (generation, item_id,
                                              details.get("MediaSources") or [], ""))
                except Exception as exc:
                    return ("cinema_sources", (generation, item_id,
                                              item.get("MediaSources") or [],
                                              type(exc).__name__))
            self.lab.worker(fetch_sources)

    def show_sources(self, generation, item_id, sources, error):
        if (self._closed or generation != self.detail_generation or
                not self.detail_item or self.detail_item.get("Id") != item_id):
            return
        for child in self.source_panel.winfo_children():
            child.destroy()
        self.source_buttons.clear()
        if not isinstance(sources, list) or not sources:
            self.selected_source_id = ""
            return
        self.sources = [x for x in sources if isinstance(x, dict)]
        if len(self.sources) < 2:
            self.selected_source_id = str(self.sources[0].get("Id") or "")
            self._update_tracks()
            return
        self._label(self.source_panel, "STREAM SOURCES", 10, MUTED, True).pack(
            side="left", padx=(0, 13))
        for idx, source in enumerate(sources[:8]):
            if not isinstance(source, dict) or not source.get("Id"):
                continue
            codec = str(source.get("Container") or "media").upper()[:10]
            try:
                bps = int(source.get("Bitrate") or 0) // 1000000
            except (TypeError, ValueError):
                bps = 0
            descriptor = "Source " + str(idx + 1) + " • " + codec
            if bps:
                descriptor += " • " + str(bps) + "Mbps"
            source_id = str(source["Id"])
            button = self._btn(self.source_panel, descriptor,
                               lambda sid=source_id: self.select_source(sid))
            button.pack(side="left", padx=3)
            self.source_buttons.append((source_id, button))
        if self.source_buttons:
            self.select_source(self.source_buttons[0][0])

    def select_source(self, source_id):
        self.selected_source_id = str(source_id or "")
        for sid, button in self.source_buttons:
            button.configure(bg=ACCENT if sid == self.selected_source_id else SURFACE2)
        self._update_tracks()

    def _reset_tracks(self):
        self.audio_options = [("Default", -1, "", 0)]
        self.subtitle_options = [("Off", -1)]
        self.audio_combo.configure(values=["Default"])
        self.subtitle_combo.configure(values=["Off"])
        self.audio_choice.set("Default")
        self.subtitle_choice.set("Off")
        self._update_selected_tracks()

    def _update_tracks(self):
        self._reset_tracks()
        source = next((x for x in self.sources if
                       str(x.get("Id") or "") == self.selected_source_id), None)
        if not source:
            return
        media_streams = source.get("MediaStreams") or []
        audio = []
        subtitles = []
        for stream in media_streams:
            if not isinstance(stream, dict):
                continue
            try:
                index = int(stream.get("Index", -1))
            except (ValueError, TypeError, OverflowError):
                continue
            if index < 0:
                continue
            kind = str(stream.get("Type") or "")
            label = str(stream.get("DisplayTitle") or
                        stream.get("Codec") or kind)[:56]
            if kind == "Audio":
                codec = str(stream.get("Codec") or "").lower()
                try:
                    bps = max(0, int(stream.get("BitRate") or 0))
                except (TypeError, ValueError, OverflowError):
                    bps = 0
                audio.append((f"{index}: {label}", index, codec, bps,
                              bool(stream.get("IsDefault"))))
            elif kind == "Subtitle":
                subtitles.append((f"{index}: {label}", index))
        self.audio_options = [("Default", -1, "", 0)] + [
            (label, idx, codec, bps) for label, idx, codec, bps, _ in audio
        ]
        self.subtitle_options = [("Off", -1)] + subtitles
        self.audio_combo.configure(values=[x[0] for x in self.audio_options])
        self.subtitle_combo.configure(values=[x[0] for x in self.subtitle_options])
        default_audio = next((a[0] for a in audio if a[4]), "Default")
        self.audio_choice.set(default_audio)
        self.subtitle_choice.set("Off")
        self._update_selected_tracks()

    def _update_selected_tracks(self):
        audio = next((a for a in self.audio_options if
                      a[0] == self.audio_choice.get()),
                     self.audio_options[0])
        subtitle = next((s for s in self.subtitle_options if
                         s[0] == self.subtitle_choice.get()),
                        self.subtitle_options[0])
        self.audio_stream_index = audio[1]
        self.selected_audio_codec = audio[2]
        self.selected_audio_bitrate = audio[3]
        self.subtitle_stream_index = subtitle[1]

    def show_episodes(self, generation, series_id, data, error):
        if (self._closed or generation != self.detail_generation or
                not self.detail_item or self.detail_item.get("Id") != series_id):
            return
        for child in self.detail_content.winfo_children():
            child.destroy()
        episodes = [x for x in data.get("Items", []) if x.get("Id")]
        self.episodes = episodes
        self._label(self.detail_content, f"Episodes · {len(episodes)} available",
                    17, WHITE, True).pack(anchor="w", pady=(0, 8))
        if error:
            self._label(self.detail_content, "Could not load episodes: " + error,
                        11, MUTED).pack(anchor="w", pady=12)
            return
        if not episodes:
            self._label(self.detail_content, "No episodes found in Jellyfin.",
                        11, MUTED).pack(anchor="w", pady=12)
            return
        strip = tk.Canvas(self.detail_content, bg=BG, height=202,
                          highlightthickness=0)
        strip.pack(fill="x")
        body = tk.Frame(strip, bg=BG)
        strip.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _e: strip.configure(
            scrollregion=strip.bbox("all")))
        strip.bind("<MouseWheel>", lambda e: strip.xview_scroll(
            -int(e.delta / 120), "units"))
        for item in episodes:
            card = tk.Frame(body, bg=SURFACE, width=250, height=184)
            card.pack(side="left", padx=6, pady=4)
            card.pack_propagate(False)
            try:
                number = "S%02dE%02d" % (int(item.get("ParentIndexNumber") or 0),
                                         int(item.get("IndexNumber") or 0))
            except (TypeError, ValueError):
                number = "EPISODE"
            self._label(card, number, 10, ACCENT, True).pack(
                anchor="w", padx=12, pady=(12, 3))
            self._label(card, (item.get("Name") or "Episode")[:65], 11, WHITE, True,
                        wraplength=220, justify="left").pack(anchor="w", padx=12)
            self._label(card, (item.get("Overview") or "")[:105], 9, MUTED,
                        wraplength=222, justify="left").pack(
                            anchor="w", padx=12, pady=(5, 0))
            self._btn(card, "▶  Play", lambda x=item: self._play_item(x), True).pack(
                anchor="w", side="bottom", padx=12, pady=9)

    def play_detail(self):
        if not self.detail_item:
            return
        if self.detail_item.get("Type") == "Series":
            if self.episodes:
                self._play_item(self.episodes[0])
            else:
                self.lab.status.set("No playable episodes have loaded")
            return
        self._play_item(self.detail_item)

    def _request_backdrop(self, generation, key, item):
        if not Image or not self.lab.client:
            return
        def fetch():
            try:
                data = self.lab.client.image_bytes(item, "Backdrop")
                if data:
                    self.lab.dispatch("cinema_backdrop", (generation, key,
                                                           item.get("Id"), data))
            except Exception as exc:
                self.lab.emit("backdrop_fetch_error", {"kind": type(exc).__name__})
        self.executor.submit(fetch)

    def set_backdrop(self, generation, key, item_id, data):
        if self._closed or not Image:
            return
        if key == "hero":
            if (generation != self.home_generation or not self.featured_item or
                    self.featured_item.get("Id") != item_id):
                return
            canvas, reference = self.hero, "hero_photo"
        else:
            if (generation != self.generation or not self.detail_item or
                    self.detail_item.get("Id") != item_id):
                return
            canvas, reference = self.detail_hero, "detail_photo"
        try:
            width = max(780, int(canvas.winfo_width()))
            height = int(canvas.cget("height"))
            original = Image.open(BytesIO(data))
            original.load()
            original = original.convert("RGB")
            factor = max(width / max(1, original.width),
                         height / max(1, original.height))
            scaled = original.resize((max(width, round(original.width * factor)),
                                      max(height, round(original.height * factor))),
                                     Image.Resampling.LANCZOS)
            x = max(0, (scaled.width - width) // 2)
            y = max(0, (scaled.height - height) // 2)
            image = scaled.crop((x, y, x + width, y + height)).convert("RGBA")
            gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            painter = ImageDraw.Draw(gradient)
            for pos in range(width):
                alpha = round(246 * max(0.0, 1.0 - pos / (width * .9)) ** 1.3)
                painter.line((pos, 0, pos, height), fill=(10, 11, 18, alpha))
            for pos in range(height):
                alpha = int(90 * (pos / height) ** 3)
                painter.line((0, pos, width, pos), fill=(10, 11, 18, alpha))
            image.alpha_composite(gradient)
            photo = ImageTk.PhotoImage(image.convert("RGB"), master=self.root)
            setattr(self, reference, photo)
            if getattr(canvas, "_backdrop_id", None):
                canvas.itemconfigure(canvas._backdrop_id, image=photo)
            else:
                canvas._backdrop_id = canvas.create_image(
                    0, 0, image=photo, anchor="nw")
            canvas.tag_lower(canvas._backdrop_id)
        except (OSError, ValueError, RuntimeError) as exc:
            self.lab.emit("backdrop_render_error", {"kind": type(exc).__name__})

    def show_player(self, item):
        self.current_view = "player"
        return super().show_player(item)

    def back_to_library(self):
        if self.root.attributes("-fullscreen"):
            self.root.attributes("-fullscreen", False)
        self.heading.configure(text=self.current_title)
        if self.category == "home":
            self.current_view = "home"
            self.activate("home")
        else:
            self.current_view = "library"
            self.activate("library")
