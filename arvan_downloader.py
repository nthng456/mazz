"""
ArvanCloud HLS downloader.

Some of the course links are not biomaze iframes but ArvanCloud player URLs of
the form:

    https://player.arvancloud.ir/index.html?config=https://<host>.arvanvod.ir/<a>/<b>/origin_config.json

These need none of the biomaze machinery (no Playwright, no GCM manifest key,
no crypto.subtle hook). The config JSON names an ordinary AES-128 HLS master
playlist whose key is served openly next to the segments, so ffmpeg can fetch,
decrypt and remux the whole thing itself in one call — which is exactly what we
verified by hand before writing this.

This module is deliberately free of any import from biomaze_downloader so the
two can import each other's names without a cycle: biomaze imports this to route
Arvan links, this imports only `dashboard` (which imports neither).
"""

import os
import re
import sys
import time
import shutil
import logging
import subprocess
import threading
from collections import deque
from urllib.parse import urlparse, parse_qs, unquote, urljoin

import requests

try:
    import dashboard as dash
    _HAS_DASH = True
except Exception:
    dash = None
    _HAS_DASH = False

log = logging.getLogger("biomaze")  # share the run's logger so lines interleave

# The player origin is enough to satisfy the CDN; the endpoint returns real
# bytes for these headers (segments and the AES key alike). Kept identical to
# what the manual ffmpeg probe used.
REFERER = "https://player.arvancloud.ir/"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

HEADERS = {"Referer": REFERER, "User-Agent": USER_AGENT}

session = requests.Session()
session.headers.update(HEADERS)


def is_arvan_url(url: str) -> bool:
    """True for any ArvanCloud player / VOD link this module can handle."""
    if not url:
        return False
    u = url.lower()
    return "arvancloud" in u or "arvanvod" in u


def _config_url(url: str) -> str:
    """
    Reduce a link to the resource that actually describes the video.

    A player URL carries the real target in its `config=` query parameter; a
    bare origin_config.json or master.m3u8 URL already is the target.
    """
    if "config=" in url:
        qs = parse_qs(urlparse(url).query)
        if qs.get("config"):
            return unquote(qs["config"][0])
    return url


def _pick_hls(config: dict) -> str | None:
    """Return the HLS (m3u8) source URL from an origin_config.json body."""
    # Arvan uses "source"; accept "sources" too in case a variant config appears.
    sources = config.get("source") or config.get("sources") or []
    if isinstance(sources, dict):
        sources = [sources]

    for s in sources:
        if not isinstance(s, dict):
            continue
        src = s.get("src") or s.get("file") or ""
        typ = (s.get("type") or "").lower()
        if "mpegurl" in typ or src.endswith(".m3u8"):
            return src
    # Last resort: first source that at least looks like a URL.
    for s in sources:
        if isinstance(s, dict) and (s.get("src") or s.get("file")):
            return s.get("src") or s.get("file")
    return None


def _parse_variants(master_url: str, master_text: str) -> dict[int, str]:
    """
    Map each rendition's height to its absolute variant-playlist URL.

    The master lists a RESOLUTION=WxH line immediately followed by the variant
    URL, e.g. RESOLUTION=1280x720 -> index-f5-v1-a1.m3u8. Variant URLs are
    relative, so they are joined onto the master URL.
    """
    variants: dict[int, str] = {}
    lines = master_text.strip().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"RESOLUTION=\d+x(\d+)", line)
            if m and i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if nxt and not nxt.startswith("#"):
                    variants[int(m.group(1))] = urljoin(master_url, nxt)
    return variants


def resolve(url: str) -> dict | None:
    """
    Turn an Arvan link into everything the download step needs.

    Returns {"master_url", "master_text", "variants": {height: url}} or None.
    Fetched once per video and reused across qualities so the config/master are
    not re-requested for each rendition.
    """
    target = _config_url(url)

    try:
        if target.endswith(".m3u8"):
            master_url = target
        else:
            r = session.get(target, timeout=30)
            r.raise_for_status()
            master_url = _pick_hls(r.json())
            if not master_url:
                log.error(f"  [arvan] no HLS source in config: {target}")
                return None

        mr = session.get(master_url, timeout=30)
        mr.raise_for_status()
        master_text = mr.text
    except Exception as e:
        log.error(f"  [arvan] resolve failed for {url}: {type(e).__name__}: {e}")
        return None

    variants = _parse_variants(master_url, master_text)
    if not variants:
        # No master (single-rendition playlist served directly at master_url).
        if "#EXTINF" in master_text:
            variants = {0: master_url}
        else:
            log.error(f"  [arvan] master declared no variants: {master_url}")
            return None

    return {"master_url": master_url, "master_text": master_text,
            "variants": variants}


def variant_for_quality(variants: dict[int, str], quality: str) -> tuple[int, str]:
    """
    Choose the variant to download for a requested quality label.

    Exact height wins; otherwise the tallest rendition still at or below the
    target (never upscale the request); otherwise the shortest available. The
    0-height sentinel (a single-rendition master fed directly) always matches.
    """
    if 0 in variants and len(variants) == 1:
        return 0, variants[0]

    want = int(quality)
    if want in variants:
        return want, variants[want]

    at_or_below = sorted((h for h in variants if h <= want), reverse=True)
    if at_or_below:
        h = at_or_below[0]
        return h, variants[h]

    h = min(variants)
    return h, variants[h]


def _playlist_duration(variant_url: str) -> float:
    """Sum the EXTINF durations of a variant playlist, 0.0 if unreadable."""
    try:
        r = session.get(variant_url, timeout=30)
        r.raise_for_status()
        return sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", r.text))
    except Exception:
        return 0.0


def _verify(path: str, expected_duration: float) -> bool:
    """ffprobe the output: it must have a video stream and the right length."""
    try:
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type", "-of", "json", path],
            capture_output=True, text=True, timeout=60)
        import json
        info = json.loads(pr.stdout or "{}")
    except Exception as e:
        log.error(f"  [arvan] verify failed to probe: {type(e).__name__}: {e}")
        return False

    kinds = {s.get("codec_type") for s in info.get("streams", [])}
    if "video" not in kinds:
        log.error(f"  [arvan] verify failed: no video stream ({kinds or 'none'})")
        return False

    if expected_duration <= 0:
        return True

    try:
        actual = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        log.error("  [arvan] verify failed: ffprobe reported no duration")
        return False

    drift = abs(actual - expected_duration)
    if drift / expected_duration * 100 > 2:
        log.error(f"  [arvan] verify FAILED: {actual:.0f}s vs "
                  f"{expected_duration:.0f}s expected (off {drift:.0f}s)")
        return False
    return True


def _ffmpeg_headers() -> str:
    """CRLF-terminated header block ffmpeg applies to every HTTP request."""
    return "".join(f"{k}: {v}\r\n" for k, v in HEADERS.items())


def download_quality(resolved: dict, out_path: str, quality: str,
                     job_num: int = 0, filename: str = "") -> bool:
    """
    Download one rendition with ffmpeg, decrypting and remuxing in a single call.

    ffmpeg is fed the variant playlist directly (not the master) so ABR cannot
    quietly pick a different rendition — the requested quality is what lands.
    The AES-128 key is a relative URI inside the variant playlist; ffmpeg
    fetches it with the same headers and decrypts transparently.

    Progress is read from `-progress pipe:1`: out_time_us against the playlist's
    total duration drives the dashboard bar. stderr is drained into a small ring
    buffer so a failure can report ffmpeg's last words without deadlocking on a
    full pipe.
    """
    t0 = time.time()
    height, variant_url = variant_for_quality(resolved["variants"], quality)
    if str(height) != quality and height != 0:
        log.warning(f"  [arvan] {quality}p not offered — using {height}p instead")

    duration = _playlist_duration(variant_url)

    _d = dash.get() if _HAS_DASH else None
    if _d and _d.is_active():
        _d.dl_start(filename or os.path.basename(out_path), job_num, quality)

    ts_out = out_path  # ffmpeg writes the final mp4 directly
    cmd = [
        "ffmpeg", "-y",
        "-headers", _ffmpeg_headers(),
        "-user_agent", USER_AGENT,
        "-i", variant_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        ts_out,
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)

    tail: deque[str] = deque(maxlen=40)

    def _drain_stderr():
        for ln in proc.stderr:
            tail.append(ln.rstrip())

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    last_draw = 0.0
    for line in proc.stdout:
        line = line.strip()
        secs = None
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            raw = line.split("=", 1)[1]
            if raw.isdigit():
                # out_time_us is microseconds; out_time_ms is, despite the name,
                # also microseconds in most builds — both divide by 1e6.
                secs = int(raw) / 1_000_000
        if secs is None:
            continue

        now = time.time()
        if now - last_draw < 0.3:
            continue
        last_draw = now
        elapsed = now - t0
        mb = os.path.getsize(ts_out) / 1048576 if os.path.exists(ts_out) else 0.0
        if _d and _d.is_active():
            done = int(secs)
            total = int(duration) or max(done, 1)
            rate = mb / elapsed if elapsed else 0.0
            eta = (duration - secs) / (secs / elapsed) if secs and elapsed else 0.0
            _d.dl_progress(min(done, total), total, mb, rate, max(eta, 0.0), elapsed)
        elif sys.stderr.isatty() and duration:
            frac = min(secs / duration, 1.0)
            bar = "█" * int(24 * frac) + "░" * (24 - int(24 * frac))
            sys.stderr.write(f"\r  [{quality}p] {bar} {frac * 100:3.0f}%  "
                             f"{mb:6.0f} MB  {elapsed:4.0f}s ")
            sys.stderr.flush()

    proc.wait()
    err_thread.join(timeout=5)
    if sys.stderr.isatty() and not (_d and _d.is_active()):
        sys.stderr.write("\r" + " " * 78 + "\r")
        sys.stderr.flush()

    if proc.returncode != 0:
        log.error(f"  [arvan][{quality}p] ffmpeg failed (rc={proc.returncode}): "
                  + " | ".join(list(tail)[-4:] or ["no stderr"]))
        if os.path.exists(ts_out):
            os.remove(ts_out)
        return False

    if not (os.path.exists(ts_out) and os.path.getsize(ts_out) > 1024):
        log.error(f"  [arvan][{quality}p] output missing or too small")
        if os.path.exists(ts_out):
            os.remove(ts_out)
        return False

    if not _verify(ts_out, duration):
        os.remove(ts_out)
        return False

    size_mb = os.path.getsize(ts_out) / 1048576
    log.info(f"  [arvan][{quality}p] 100% — {size_mb:.0f} MB in {time.time() - t0:.0f}s")
    if _d and _d.is_active():
        _d.dl_note(f"✓ {filename or os.path.basename(out_path)} "
                   f"[{quality}p] {size_mb:.0f} MB in {time.time() - t0:.0f}s")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    if len(sys.argv) < 2:
        print("Usage: python arvan_downloader.py <player-or-config-or-m3u8-url> "
              "[out.mp4] [quality]")
        sys.exit(1)

    url = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "arvan_out.mp4"
    q = sys.argv[3] if len(sys.argv) > 3 else "720"

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")

    res = resolve(url)
    if not res:
        sys.exit("could not resolve the Arvan link")
    print("variants:", {h: u.rsplit('/', 1)[-1] for h, u in res["variants"].items()})
    ok = download_quality(res, out, q, filename=os.path.basename(out))
    print("OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)
