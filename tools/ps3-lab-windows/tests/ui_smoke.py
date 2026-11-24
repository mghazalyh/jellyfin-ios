"""GitHub-hosted Windows cinematic GUI smoke with mock Jellyfin catalog.

Uses no actual server, PS3, iPad or other user-owned machine.
"""
from __future__ import annotations
import io
import os
from pathlib import Path
import sys
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import Ps3Lab
from PIL import Image, ImageDraw, ImageGrab

MOVIE = {
    "Id": "movie-one", "Type": "Movie", "Name": "The Celestial Archive",
    "Overview": "A deep space mission investigates an ancient radio signal.",
    "ProductionYear": 2026, "Genres": ["Science Fiction", "Adventure"],
    "RunTimeTicks": 60000000000,
    "UserData": {"PlaybackPositionTicks": 2500000000, "PlayedPercentage": 10},
    "ImageTags": {"Primary": "fixture"}, "BackdropImageTags": ["fixture"],
}
SERIES = {
    "Id": "series-one", "Type": "Series", "Name": "Signals from Orion",
    "ProductionYear": 2025, "Genres": ["Mystery"],
    "Overview": "A crew loses contact with the outer colonies.",
    "ImageTags": {"Primary": "fixture"}, "BackdropImageTags": ["fixture"],
}
EPISODE = {
    "Id": "episode-one", "Type": "Episode", "Name": "First Contact",
    "ParentIndexNumber": 1, "IndexNumber": 1,
    "Overview": "The signal reaches a distant station.",
    "RunTimeTicks": 18000000000,
    "ImageTags": {"Primary": "fixture"},
}
SURF = {
    "Id": "surf-all", "Type": "TvChannel", "Name": "Surf: All Channels",
    "ImageTags": {"Primary": "fixture"},
}


def widget_texts(widget):
    found = []
    for child in widget.winfo_children():
        try:
            text = child.cget("text")
        except tk.TclError:
            text = ""
        if text:
            found.append(text)
        found.extend(widget_texts(child))
    return found


class Fixture:
    user_id = "fixture-user"

    def browse(self, category, start=0, limit=48, search="", parent_id=""):
        rows = {"resume": [MOVIE], "home": [MOVIE, EPISODE],
                "recent": [MOVIE], "movies": [MOVIE],
                "shows": [SERIES], "live": [{
                    "Id": "channel-one", "Type": "TvChannel",
                    "Name": "Science Fiction TV",
                    "ImageTags": {"Primary": "fixture"}}]}.get(category, [])
        return {"Items": rows[:limit], "TotalRecordCount": len(rows)}

    def series_episodes(self, series_id):
        assert series_id == SERIES["Id"]
        return {"Items": [EPISODE], "TotalRecordCount": 1}

    def image_bytes(self, item, image_type="Primary"):
        width, height = ((1000, 350) if image_type == "Backdrop" else (300, 425))
        img = Image.new("RGB", (width, height), (18, 19, 34))
        draw = ImageDraw.Draw(img)
        for x in range(width):
            draw.line((x, 0, x, height), fill=(
                18 + int(55 * x / width),
                18 + int(32 * x / width),
                34 + int(90 * x / width)))
        draw.ellipse((width * 0.62, height * 0.15, width * 0.84, height * 0.88),
                     fill="#9c63df")
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=75)
        return output.getvalue()


def pump_until(root, predicate, timeout=10):
    """Use Tk's own event loop instead of root.update() on old Cocoa/Tk 8.5.

    The macOS system Python 3.9 Tk 8.5 on macOS 15 can permanently block in
    Tk.update() even when root.mainloop() works. The release app uses mainloop,
    so the smoke test must match that execution model.
    """
    deadline = time.monotonic() + timeout
    completed = {"ok": False}

    def check():
        try:
            if predicate():
                completed["ok"] = True
                root.quit()
            elif time.monotonic() >= deadline:
                root.quit()
            else:
                root.after(50, check)
        except Exception:
            root.quit()
            raise

    root.after(0, check)
    root.mainloop()
    if not completed["ok"]:
        raise AssertionError("GUI operation did not complete within test deadline")


def main():
    root = tk.Tk()
    root.geometry("1280x800+0+0")
    lab = Ps3Lab(root)
    lab.client = Fixture()
    try:
        print("GUI_STAGE=load_home", flush=True)
        lab.ui.navigate("home")
        pump_until(root, lambda: not lab.ui.home_pending)
        pump_until(root, lambda: lab.ui.hero_photo is not None)
        assert lab.ui.featured_item["Id"] == MOVIE["Id"]
        assert len(lab.ui.cards) >= 3
        assert lab.ui.video_host.winfo_id() > 0
        print("GUI_STAGE=series_details", flush=True)
        lab.ui.show_details(SERIES)
        pump_until(root, lambda: bool(lab.ui.episodes))
        assert lab.ui.episodes[0]["Id"] == EPISODE["Id"]
        print("GUI_STAGE=return_home", flush=True)
        lab.ui.navigate("home")
        pump_until(root, lambda: not lab.ui.home_pending)
        print("GUI_STAGE=surf_controls", flush=True)
        lab.ui.show_player(SURF)
        lab.ui.show_surf("all")
        assert lab.ui.surf_panel.winfo_manager() == "pack"
        labels = widget_texts(lab.ui.surf_panel)
        for label in ("Pause Surf Looping", "Resume Surf Looping",
                      "Favorite Current Channel", "Select Previous Channel",
                      "Favorite Selected Previous", "Select Current Channel",
                      "Show Surf Source Status"):
            assert label in labels, label
        lab.ui.set_surf_selection(type("Selection", (), {"source": "Fixture"})())
        assert "Previous selection: Fixture" in lab.ui.surf_selection_text.get()
        lab.ui.hide_surf()
        assert lab.ui.surf_panel.winfo_manager() == ""
        lab.ui.navigate("home")
        pump_until(root, lambda: not lab.ui.home_pending)
        if sys.platform != "darwin":
            root.update_idletasks()
        print("GUI_STAGE=ready", flush=True)
        # Only capture the ephemeral GitHub-hosted Windows runner. macOS
        # screen capture can prompt for Screen Recording consent and can show
        # a human operator's desktop instead of just this test window.
        if sys.platform == "win32" and os.environ.get("GITHUB_ACTIONS") == "true":
            try:
                target = Path(__file__).resolve().parent.parent / "ci-theater-screenshot.png"
                image = ImageGrab.grab(bbox=(
                    root.winfo_rootx(), root.winfo_rooty(),
                    root.winfo_rootx() + root.winfo_width(),
                    root.winfo_rooty() + root.winfo_height(),
                ))
                image.save(target)
                print("GUI_SCREENSHOT=" + str(target), flush=True)
            except (OSError, RuntimeError, ValueError) as exc:
                print("GUI_SCREENSHOT_UNAVAILABLE=" + type(exc).__name__, flush=True)
        else:
            print("GUI_SCREENSHOT_SKIPPED=not_windows_ci", flush=True)
        assert not [e for e in lab.events if e["event"] == "ui_pump_error"]
        print("GUI_SMOKE_PASS: home, cinematic backdrops, posters, episodes, "
              "video host, Surf panel")
    finally:
        lab.close()


if __name__ == "__main__":
    main()
