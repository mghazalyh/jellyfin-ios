"""GitHub-hosted Windows cinematic GUI smoke with mock Jellyfin catalog.

Uses no actual server, PS3, iPad or other user-owned machine.
"""
from __future__ import annotations
import io
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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("GUI operation did not complete within test deadline")


def main():
    root = tk.Tk()
    root.geometry("1280x800+0+0")
    lab = Ps3Lab(root)
    lab.client = Fixture()
    try:
        lab.ui.navigate("home")
        pump_until(root, lambda: not lab.ui.home_pending)
        pump_until(root, lambda: lab.ui.hero_photo is not None)
        assert lab.ui.featured_item["Id"] == MOVIE["Id"]
        assert len(lab.ui.cards) >= 3
        assert lab.ui.video_host.winfo_id() > 0
        lab.ui.show_details(SERIES)
        pump_until(root, lambda: bool(lab.ui.episodes))
        assert lab.ui.episodes[0]["Id"] == EPISODE["Id"]
        lab.ui.navigate("home")
        pump_until(root, lambda: not lab.ui.home_pending)
        root.update_idletasks()
        try:
            target = Path(__file__).resolve().parent.parent / "ci-theater-screenshot.png"
            image = ImageGrab.grab(bbox=(
                root.winfo_rootx(), root.winfo_rooty(),
                root.winfo_rootx() + root.winfo_width(),
                root.winfo_rooty() + root.winfo_height(),
            ))
            image.save(target)
            print("GUI_SCREENSHOT=" + str(target))
        except (OSError, RuntimeError, ValueError) as exc:
            print("GUI_SCREENSHOT_UNAVAILABLE=" + type(exc).__name__)
        assert not [e for e in lab.events if e["event"] == "ui_pump_error"]
        print("GUI_SMOKE_PASS: home, cinematic backdrops, posters, episodes, video HWND")
    finally:
        lab.close()


if __name__ == "__main__":
    main()
