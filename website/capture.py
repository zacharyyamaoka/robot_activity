"""Headless captures of real pages: screenshots, element crops, phone renders and the hero video's frames.

One headless Chrome (Playwright, the system's Chrome channel, no window) serves every capture of a build. Nothing here
draws: every pixel comes from a page the package or the site builder wrote, rendered by the browser.

    with Browser() as browser:
        browser.shot(page_path, out_png, width=1280, height=900)

Needs `--with playwright` and a local Chrome; `ROBOT_ACTIVITY_CHROME` points at another Chromium binary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

EAGER_IMAGES = ("Promise.all([...document.images].map(image => { image.loading = 'eager'; "
                "return image.complete ? 0 : new Promise(done => { image.onload = image.onerror = done; }); }))")


class Browser:
    """A headless Chrome kept open for many captures; console errors are collected per page load."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self.console_errors: list[tuple[str, str]] = []   # (page, message)

    def __enter__(self) -> Browser:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        binary = os.environ.get("ROBOT_ACTIVITY_CHROME")
        launch = {"executable_path": binary} if binary else {"channel": "chrome"}
        # WHY headless: nothing may take Zach's focus; a headless browser never makes a window
        self._browser = self._playwright.chromium.launch(headless=True, args=["--disable-gpu"], **launch)
        return self

    def __exit__(self, *_exc) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def open(self, url: str, *, width: int, height: int, dark: bool = False, scale: float = 1.0):
        """A loaded page at this viewport; the caller closes it."""
        page = self._browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=scale,
                                      color_scheme="dark" if dark else "light")
        page.on("console", lambda message: self.console_errors.append((url, message.text))
                if message.type == "error" else None)
        page.on("pageerror", lambda error: self.console_errors.append((url, str(error))))
        page.goto(url, wait_until="load")
        page.evaluate("document.fonts ? document.fonts.ready : null")
        return page

    def shot(self, url: str, out: Path, *, width: int = 1280, height: int = 900, full_page: bool = False,
             clip: dict | None = None, dark: bool = False, css: str | None = None, scale: float = 1.0) -> Path:
        """A screenshot of the viewport (or the full page, or a clip) after optional extra CSS."""
        page = self.open(url, width=width, height=height, dark=dark, scale=scale)
        try:
            if css:
                page.add_style_tag(content=css)
            if full_page:  # lazy images below the fold only load when scrolled to: load them all first
                page.evaluate(EAGER_IMAGES)
            page.screenshot(path=str(out), full_page=full_page, clip=clip)
        finally:
            page.close()
        return out

    def element(self, url: str, selector: str, out: Path, *, width: int = 1280, height: int = 900, pad: int = 0,
                css: str | None = None, nth: int = 0) -> Path:
        """A crop around one element (its bounding box plus `pad` px), from a full-page render."""
        page = self.open(url, width=width, height=height)
        try:
            if css:
                page.add_style_tag(content=css)
            box = page.locator(selector).nth(nth).bounding_box()
            if box is None:
                raise SystemExit(f"{selector!r} is not on {url}")
            clip = {"x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad),
                    "width": min(width, box["width"] + 2 * pad), "height": box["height"] + 2 * pad}
            page.screenshot(path=str(out), full_page=True, clip=clip)
        finally:
            page.close()
        return out

    def measure(self, url: str, script: str, *, width: int, height: int = 900, dark: bool = False):
        """Evaluate a script on the loaded page (layout checks: overflow, boxes)."""
        page = self.open(url, width=width, height=height, dark=dark)
        try:
            return page.evaluate(script)
        finally:
            page.close()


def encode_video(frames: list[Path], out_mp4: Path, out_gif: Path, *, fps: float, hold_last_s: float,
                 gif_width: int) -> None:
    """Stitch frames into a muted H.264 mp4 (yuv420p, plays everywhere) and a palette GIF fallback."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg is needed to stitch the hero video")
    listing = out_mp4.with_suffix(".frames.txt")
    lines = []
    for index, frame in enumerate(frames):
        duration = 1.0 / fps + (hold_last_s if index == len(frames) - 1 else 0.0)
        lines += [f"file '{frame.resolve()}'", f"duration {duration:.4f}"]
    lines.append(f"file '{frames[-1].resolve()}'")   # the concat demuxer needs the last file twice to honour its time
    listing.write_text("\n".join(lines) + "\n")
    common = [ffmpeg, "-y", "-loglevel", "error", "-threads", "2", "-f", "concat", "-safe", "0", "-i", str(listing)]
    subprocess.run([*common, "-vf", "fps=12,scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "slow",
                    "-crf", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(out_mp4)], check=True)
    subprocess.run([*common, "-vf", f"fps={fps},scale={gif_width}:-1:flags=lanczos,split[a][b];"
                    "[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
                    str(out_gif)], check=True)
    listing.unlink()
