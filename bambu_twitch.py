#!/usr/bin/env python3
"""
bambu-twitch: Kamerastream + Web-Overlay (z.B. Bambu Buddy) -> ffmpeg -> Twitch

Ablauf:
  1. Headless-Chromium (Playwright) rendert die Overlay-URL mit transparentem Hintergrund
  2. Screenshots (PNG, mit Alpha) gehen per Pipe an ffmpeg
  3. ffmpeg legt das Overlay auf den Kamerastream und sendet per RTMP an Twitch
  4. Bricht etwas ab (Drucker aus, Netz weg), wird automatisch neu gestartet
"""
import asyncio
import os
import signal
import sys
import time
from urllib.parse import urlparse

from playwright.async_api import async_playwright

# ---------- Konfiguration (aus Umgebungsvariablen / config.env) ----------
# Beide kommen aus Bambuddy (alternativ auch CAMERA_URL / OVERLAY_URL)
CAMERA_URL = os.environ.get("BAMBUDDY_CAMERA_URL") or os.environ["CAMERA_URL"]
OVERLAY_URL = os.environ.get("BAMBUDDY_OVERLAY_URL") or os.environ["OVERLAY_URL"]
TWITCH_KEY = os.environ.get("TWITCH_STREAM_KEY", "")
TWITCH_INGEST = os.environ.get("TWITCH_INGEST", "rtmp://live.twitch.tv/app")
TWITCH_ENABLED = os.environ.get("TWITCH_ENABLED", "1") == "1"
YOUTUBE_ENABLED = os.environ.get("YOUTUBE_ENABLED", "1") == "1"
# Optional: gleichzeitig zu YouTube Live senden (das Video wird nur einmal kodiert)
YOUTUBE_KEY = os.environ.get("YOUTUBE_STREAM_KEY", "")
YOUTUBE_INGEST = os.environ.get("YOUTUBE_INGEST", "rtmp://a.rtmp.youtube.com/live2")

WIDTH = int(os.environ.get("WIDTH", "1280"))
HEIGHT = int(os.environ.get("HEIGHT", "720"))
FPS = int(os.environ.get("FPS", "15"))   # Bambuddy-Kamera liefert fps=15
OVERLAY_FPS = int(os.environ.get("OVERLAY_FPS", "5"))   # Overlay muss nicht 30 fps haben
VIDEO_BITRATE = os.environ.get("VIDEO_BITRATE", "3500k")
X264_PRESET = os.environ.get("X264_PRESET", "veryfast")
# Optional: Hardware-Encoder statt libx264 (z.B. h264_vaapi braucht /dev/dri im LXC)
# Diagnose: OUTPUT_URL=/tmp/test.flv schreibt 20 s in eine Datei statt zu Twitch
OUTPUT_URL = os.environ.get("OUTPUT_URL", "")
FFMPEG_LOGLEVEL = os.environ.get("FFMPEG_LOGLEVEL", "warning")
STATS = os.environ.get("STATS", "0") == "1"
# Zusätzliches CSS für die Overlay-Seite, z.B. "#root{background:transparent!important}"
OVERLAY_CSS = os.environ.get("OVERLAY_CSS", "")
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "10"))
# Twitch verlangt Audio; wenn die Kamera keinen Ton liefert, wird Stille eingemischt
SILENT_AUDIO = os.environ.get("SILENT_AUDIO", "1") == "1"


async def wait_for_source(timeout: int = 120) -> None:
    """Wartet, bis der Host der Kamera-URL erreichbar ist (z.B. nach Neustart des Hosts)."""
    u = urlparse(CAMERA_URL)
    if not u.hostname:
        return
    port = u.port or {"https": 443, "rtsp": 554, "rtsps": 322}.get(u.scheme, 80)
    deadline = time.monotonic() + timeout
    while True:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(u.hostname, port), 3)
            w.close()
            return
        except (OSError, asyncio.TimeoutError):
            if time.monotonic() > deadline:
                raise RuntimeError(f"{u.hostname}:{port} nicht erreichbar")
            await asyncio.sleep(2)


def build_ffmpeg_cmd() -> list[str]:
    cam_opts = []
    if CAMERA_URL.startswith("rtsp"):
        cam_opts = ["-rtsp_transport", "tcp"]
    elif CAMERA_URL.startswith("http"):
        # Bambuddy liefert einen MJPEG-Stream über HTTP; bei Abbruch neu verbinden.
        # Falls die Erkennung scheitert, hier ["-f", "mpjpeg"] ergänzen.
        cam_opts = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", FFMPEG_LOGLEVEL,
        *([] if STATS else ["-nostats"]),
        # Eingang 0: Kamera
        "-use_wallclock_as_timestamps", "1",
        *cam_opts,
        "-i", CAMERA_URL,
        # Eingang 1: Overlay-PNGs aus der Pipe
        "-use_wallclock_as_timestamps", "1",
        "-f", "image2pipe", "-framerate", str(OVERLAY_FPS), "-c:v", "png", "-i", "pipe:0",
    ]

    if SILENT_AUDIO:
        cmd += ["-re", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    filter_complex = (
        f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,fps={FPS},setsar=1[bg];"
        f"[1:v]format=rgba,scale={WIDTH}:{HEIGHT}[ov];"
        f"[bg][ov]overlay=0:0:eof_action=repeat:format=auto,format=yuv420p[out]"
    )

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "2:a" if SILENT_AUDIO else "0:a?",
        "-c:v", "libx264", "-preset", X264_PRESET, "-tune", "zerolatency",
        "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_BITRATE, "-bufsize", str(int(VIDEO_BITRATE[:-1]) * 2) + "k",
        "-g", str(FPS * 2), "-keyint_min", str(FPS * 2), "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
    ]
    if OUTPUT_URL:
        cmd += ["-t", "20", "-f", "flv", "-y", OUTPUT_URL]
    else:
        targets = []
        if TWITCH_KEY and TWITCH_ENABLED:
            targets.append(f"{TWITCH_INGEST}/{TWITCH_KEY}")
        if YOUTUBE_KEY and YOUTUBE_ENABLED:
            targets.append(f"{YOUTUBE_INGEST}/{YOUTUBE_KEY}")
        if not targets:
            sys.exit("Kein Stream-Key gesetzt (TWITCH_STREAM_KEY und/oder YOUTUBE_STREAM_KEY)")
        if len(targets) == 1:
            cmd += ["-f", "flv", targets[0]]
        else:
            # tee-Muxer: ein Encoder, mehrere Ziele; fällt eines aus, läuft das andere weiter
            tee = "|".join(f"[f=flv:onfail=ignore]{t}" for t in targets)
            cmd += ["-f", "tee", tee]
    return cmd


async def run_once() -> None:
    await wait_for_source()
    ffmpeg = await asyncio.create_subprocess_exec(
        *build_ffmpeg_cmd(),
        stdin=asyncio.subprocess.PIPE,
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        await page.goto(OVERLAY_URL, wait_until="domcontentloaded", timeout=30000)
        print("Overlay geladen", flush=True)
        # Hintergrund transparent erzwingen (falls die Seite selbst keinen setzt)
        await page.add_style_tag(content="html,body{background:transparent!important}" + OVERLAY_CSS)

        interval = 1.0 / OVERLAY_FPS
        try:
            while ffmpeg.returncode is None:
                t0 = time.monotonic()
                png = await page.screenshot(type="png", omit_background=True)
                try:
                    ffmpeg.stdin.write(png)
                    await ffmpeg.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    break
                await asyncio.sleep(max(0.0, interval - (time.monotonic() - t0)))
        finally:
            # ffmpeg zuerst hart beenden, damit nichts beim Herunterfahren hängt
            if ffmpeg.returncode is None:
                try:
                    ffmpeg.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(browser.close(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
    print(f"ffmpeg beendet (Code {ffmpeg.returncode})", flush=True)


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    while not stop.is_set():
        task = asyncio.create_task(run_once())
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if stopper in done:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=10)
            except BaseException:  # noqa: BLE001
                pass
            break
        try:
            task.result()
        except Exception as e:  # noqa: BLE001
            print(f"Fehler: {e}", file=sys.stderr, flush=True)
        print(f"Neustart in {RETRY_DELAY}s …", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=RETRY_DELAY)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
