"""
Biomaze Video Downloader
Downloads videos from stream.biomaze.ir using Playwright
Segments downloaded directly with requests, decrypted with pycryptodome
Uploads to Hugging Face Bucket, then deletes local file to save disk
"""

from playwright.sync_api import sync_playwright
from Crypto.Cipher import AES
from huggingface_hub import get_bucket_paths_info, sync_bucket
from concurrent.futures import ThreadPoolExecutor
import multiprocessing as mp
import subprocess
import requests
import threading
import logging
import logging.handlers
import base64
import queue
import json
import sys
import os
import re
import shutil
import tempfile
import time

# Rich-based live dashboard. Optional: if rich (or a TTY) is unavailable the
# downloader falls back to plain line logging and the old stderr progress bar.
try:
    import dashboard as dash
    _HAS_DASH = True
except Exception:
    dash = None
    _HAS_DASH = False

REFERER = "https://stream.biomaze.ir"
QUALITIES = ["720"]
HF_BUCKET = "hf://buckets/nthng454/Bucket"
HF_BUCKET_ID = HF_BUCKET.split("hf://buckets/", 1)[-1]  # "nthng454/Bucket"

# Segments are fetched this many at a time. Each is an independent ranged
# request, so they parallelise cleanly; the .ts is still assembled in order.
WORKERS = int(os.environ.get("BIOMAZE_WORKERS", "40"))

# A segment the CDN has stored truncated is refilled from another rendition,
# but never from one below this: a 360p fill is more noticeable than the same
# 4 s at 480p or above.
FALLBACK_FLOOR = 480

# Hard ceiling on one extraction. A healthy one finishes in ~10-25 s, so this
# is loose enough not to cut off a slow-but-working page and tight enough that
# a wedged player costs a minute instead of the whole run.
EXTRACT_CEILING = int(os.environ.get("BIOMAZE_EXTRACT_CEILING", "90"))

# Extraction failures are usually a transient throttle rather than a bad video,
# so the same URL is given another go before the video is written off.
EXTRACT_ATTEMPTS = 3
RETRY_BACKOFF = 10

HEADERS = {
    "Referer": REFERER,
    "Origin": REFERER,
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("downloader.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("biomaze")

# huggingface_hub logs every HTTP call at INFO, which would bury the run's own
# output; same for urllib3's connection-pool chatter.
for noisy in ("httpx", "urllib3", "huggingface_hub", "filelock"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _attach_dashboard_logging(dashboard) -> None:
    """
    Replace the stdout StreamHandler with the dashboard's handler so log lines
    stop racing with Rich's own terminal writes. The dashboard shows only
    WARNING+ (as a sticky alert line); routine INFO results are folded into the
    panels via dl_note()/up_note(). The file handler is kept, so downloader.log
    still gets the full, unabridged history.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout:
            root.removeHandler(h)
    dh = dash.DashboardLogHandler(dashboard)
    dh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(dh)

session = requests.Session()
session.headers.update(HEADERS)
# Match the pool to the worker count: past the default of 10, urllib3 discards
# and reopens connections on every request, which costs a TLS handshake each time.
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=WORKERS * 2, pool_maxsize=WORKERS * 2, max_retries=0)
session.mount("https://", _adapter)
session.mount("http://", _adapter)


# Hooks crypto.subtle to lift the per-page-load AES-256-GCM manifest key, and
# records the raw (still encrypted) hex bodies of every .m3u8 the player fetches.
INIT_SCRIPT = r"""
window.__C = { decrypts: [], raw: [] };
const b64 = (b) => { const u=new Uint8Array(b); let s=''; for(let i=0;i<u.length;i++) s+=String.fromCharCode(u[i]); return btoa(s); };
const buf = (x) => !x ? null : (x instanceof ArrayBuffer ? x : (x.buffer ? x.buffer.slice(x.byteOffset,x.byteOffset+x.byteLength) : null));

const oOpen = XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open = function(m,u,...r){ this.__u=String(u); return oOpen.call(this,m,u,...r); };
const oSend = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.send = function(...a){
    this.addEventListener('load', () => {
        if (this.__u && this.__u.indexOf('.m3u8')!==-1 && typeof this.response==='string')
            window.__C.raw.push({ url:this.__u, full:this.response });
    });
    return oSend.apply(this,a);
};

const S = crypto.subtle, km = new WeakMap();
const oImp = S.importKey;
S.importKey = function(f,kd){ const kb=buf(kd);
    return oImp.apply(this,arguments).then(k=>{ km.set(k,{b64:kb?b64(kb):null}); return k; }); };
const oDec = S.decrypt;
S.decrypt = function(algo,key,data){
    const rec = { keyB64:(km.get(key)||{}).b64 };
    window.__C.decrypts.push(rec);
    return oDec.apply(this,arguments).then(o=>{
        try { rec.isM3u8 = new TextDecoder().decode(o).indexOf('#EXTM3U')!==-1; } catch(e){}
        return o;
    });
};
"""


def decrypt_manifest(raw_hex: str, key: bytes) -> str | None:
    """
    Wire format: hex(IV 12B) || hex(ciphertext||GCM tag 16B) || trailer.
    The trailer length is not constant between manifests, so sweep it and let
    the GCM tag confirm the correct boundary.
    """
    for trailer in range(0, 80):
        end = len(raw_hex) - trailer
        if (end - 24) % 2:
            continue
        try:
            iv = bytes.fromhex(raw_hex[:24])
            blob = bytes.fromhex(raw_hex[24:end])
            out = AES.new(key, AES.MODE_GCM, nonce=iv).decrypt_and_verify(blob[:-16], blob[-16:])
        except Exception:
            continue
        return out.decode("utf-8")
    return None


def _extract_m3u8_uncapped(video_url: str) -> dict | None:
    """
    Returns {quality: playlist_text}, mapped explicitly from the master
    playlist's NAME= attribute — never guessed from segment sizes.

    Runs unbounded; call extract_m3u8_per_quality instead, which caps it.
    """
    if not video_url.endswith("/iframe"):
        video_url = video_url.rstrip("/") + "/iframe"

    t0 = time.time()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(extra_http_headers={"Referer": REFERER})
            page = context.new_page()
            page.add_init_script(INIT_SCRIPT)

            page.goto(video_url, wait_until="domcontentloaded", timeout=60000)

            def try_play():
                """Click whatever starts playback. Silent no-op if nothing matches."""
                for selector in [".media", "video", "[class*='play']"]:
                    try:
                        page.click(selector, timeout=1500)
                        return True
                    except Exception:
                        continue
                return False

            # Let the player's JS attach its handlers before clicking: a click
            # that lands first is silently swallowed and playback never starts.
            page.wait_for_timeout(1500)
            try_play()

            # Poll for what is actually needed — the manifest key and at least
            # one master playlist — rather than waiting out the worst case, and
            # re-click periodically since a click can still land too early on a
            # slow mount. Without the retry a missed click means a dead 20 s and
            # a failed extraction.
            deadline = time.time() + 25
            next_retry = time.time() + 4
            cap = None
            while time.time() < deadline:
                cap = page.evaluate("() => window.__C")
                have_key = any(d.get("isM3u8") and d.get("keyB64")
                               for d in cap["decrypts"])
                if have_key and cap["raw"]:
                    break
                if time.time() >= next_retry:
                    try_play()
                    next_retry = time.time() + 4
                page.wait_for_timeout(250)

            key_b64 = next((d["keyB64"] for d in (cap or {}).get("decrypts", [])
                            if d.get("isM3u8") and d.get("keyB64")), None)
            if not key_b64:
                log.warning(f"No manifest key captured for {video_url}")
                browser.close()
                return None

            key = base64.b64decode(key_b64)

            master = None
            for r in (cap or {}).get("raw", []):
                text = decrypt_manifest(r["full"], key)
                if text and "#EXT-X-STREAM-INF" in text:
                    master = text
                    break

            if not master:
                log.warning(f"Master playlist not found for {video_url}")
                browser.close()
                return None

            # The master playlist states each variant's URL outright.
            variants = {}
            lines = master.strip().splitlines()
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    name = re.search(r'NAME="(\d+)p"', line)
                    res = re.search(r"RESOLUTION=\d+x(\d+)", line)
                    q = name.group(1) if name else (res.group(1) if res else None)
                    if q and i + 1 < len(lines):
                        variants[q] = lines[i + 1].strip()

            # Fetch every rendition the master offers, not only the ones in
            # QUALITIES: the lower ones are the recovery path for segments the
            # CDN has stored truncated. Which ones get saved is decided later.
            wanted = dict(variants)
            if not wanted:
                log.warning("Master declared no usable variants")
                browser.close()
                return None

            # Fetch from inside the page: the endpoint returns an empty body
            # unless the request carries a Referer/Origin for the site.
            #
            # Every fetch carries its own AbortSignal. Without one, a server
            # that accepts the connection and then never answers leaves the
            # promise pending forever — and page.evaluate has no timeout of its
            # own, so the whole run hangs on that single await.
            fetched = page.evaluate("""async (urls) => {
                const one = async (u) => {
                    try {
                        const r = await fetch(u, { signal: AbortSignal.timeout(20000) });
                        return await r.text();
                    } catch (e) { return ''; }
                };
                const texts = await Promise.all(urls.map(one));
                return Object.fromEntries(urls.map((u, i) => [u, texts[i]]));
            }""", list(wanted.values()))

            browser.close()

            result = {}
            summary = []
            for q, u in sorted(wanted.items(), key=lambda kv: int(kv[0]), reverse=True):
                raw = fetched.get(u) or ""
                if not raw:
                    log.warning(f"  [{q}p] empty manifest response")
                    continue
                text = decrypt_manifest(raw, key)
                if not text:
                    log.warning(f"  [{q}p] decrypt failed ({len(raw)} raw chars)")
                    continue

                if "#EXT-X-ENDLIST" not in text:
                    log.warning(f"  [{q}p] playlist has no #EXT-X-ENDLIST — may be truncated")

                result[q] = text
                size = sum(int(a) for a, _ in
                           re.findall(r"#EXT-X-BYTERANGE:(\d+)(?:@(\d+))?", text))
                summary.append(f"{q}p:{size / 1048576:.0f}MB")

            # Distinct playlists are the guard against silently mapping the
            # same variant to several quality labels.
            if len(set(result.values())) != len(result):
                log.error("  Duplicate playlists across qualities — aborting")
                return None

            if result:
                top = max(result, key=int)
                segs = result[top].count("#EXTINF")
                dur = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", result[top]))
                log.info(f"  {segs} segments, {dur / 60:.0f}min — "
                         f"{' '.join(summary)} ({time.time() - t0:.0f}s)")
            return result if result else None

    except Exception as e:
        log.error(f"Extraction failed for {video_url}: {e}", exc_info=True)
        return None


def _extract_into(result_q, log_q, video_url: str) -> None:
    """
    Child-process entry point: run the uncapped body, hand back the result.

    The child must not touch the terminal the parent's Live display owns — two
    processes writing cursor-control codes to one terminal is what shredded the
    panels. So every log handler is stripped and replaced with a QueueHandler
    that ships records to the parent over log_q; the parent (single process,
    Live-safe) replays them on its own handlers. The result goes on result_q.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(logging.handlers.QueueHandler(log_q))
    root.setLevel(logging.INFO)
    try:
        result_q.put(_extract_m3u8_uncapped(video_url))
    except Exception:
        result_q.put(None)


def _drain_child_logs(log_q) -> None:
    """Replay every log record the child has queued on the parent's handlers."""
    while True:
        try:
            record = log_q.get_nowait()
        except (queue.Empty, EOFError, OSError):
            return
        if record is not None:
            logging.getLogger(record.name).handle(record)


def extract_m3u8_per_quality(video_url: str) -> dict | None:
    """
    Run extraction under a hard deadline, retrying once.

    Inside the page each await is bounded individually, but page.evaluate
    itself takes no timeout, so a player wedged on its main thread can still
    hold the run forever — which is what stalled it at 41-1.mp4. A thread
    cannot be killed and Playwright's sync API will not run under asyncio, so
    the ceiling is enforced with a child process that gets terminated.

    A retry is worth it because the stall is a server-side throttle, not a
    property of the video: the same URL usually extracts fine moments later.
    """
    for attempt in range(1, EXTRACT_ATTEMPTS + 1):
        result_q = mp.Queue()
        log_q = mp.Queue()
        proc = mp.Process(target=_extract_into,
                          args=(result_q, log_q, video_url), daemon=True)
        proc.start()

        result = None
        got = False
        deadline = time.time() + EXTRACT_CEILING
        try:
            # Poll for the result while continuously replaying the child's logs,
            # so extraction progress shows in the parent (and its dashboard/file
            # handlers) without the child ever writing to the terminal itself.
            while True:
                _drain_child_logs(log_q)
                try:
                    result = result_q.get(timeout=0.25)
                    got = True
                    break
                except queue.Empty:
                    pass
                if time.time() >= deadline:
                    log.warning(f"  extraction stalled past {EXTRACT_CEILING}s "
                                f"(attempt {attempt}/{EXTRACT_ATTEMPTS})")
                    break
        finally:
            # terminate() unconditionally: on the success path the child is
            # already exiting, and on the stall path it owns a wedged browser
            # that will not come back on its own.
            proc.terminate()
            proc.join(timeout=10)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)
            _drain_child_logs(log_q)   # flush anything logged just before exit
            result_q.close()
            log_q.close()

        if got and result:
            return result
        if attempt < EXTRACT_ATTEMPTS:
            time.sleep(RETRY_BACKOFF * attempt)

    return None


def parse_segments(m3u8_content: str):
    """
    Parse an m3u8 playlist into segments.

    Per the HLS spec, #EXT-X-BYTERANGE without an @offset continues from the
    end of the previous range on the SAME resource, so the running offset is
    tracked per segment URL here.
    """
    lines = m3u8_content.strip().split("\n")
    segments = []
    current_key_url = None
    current_iv = None
    current_byterange = None
    last_url = None
    running_offset = 0

    for i, line in enumerate(lines):
        line = line.strip()

        if line.startswith("#EXT-X-KEY"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                current_key_url = m.group(1)
            iv_m = re.search(r'IV=0x([0-9a-fA-F]+)', line)
            if iv_m:
                current_iv = bytes.fromhex(iv_m.group(1))

        elif line.startswith("#EXT-X-BYTERANGE"):
            br = line.split(":")[1]
            parts = br.split("@")
            length = int(parts[0])
            offset = int(parts[1]) if len(parts) > 1 else None
            current_byterange = (length, offset)

        elif line.startswith("http") and not line.startswith("#"):
            if current_byterange:
                length, offset = current_byterange
                if offset is None:
                    if last_url == line:
                        offset = running_offset
                    else:
                        offset = 0
                segments.append({
                    "url": line,
                    "byterange": (length, offset),
                    "key_url": current_key_url,
                    "iv": current_iv,
                })
                running_offset = offset + length
                last_url = line
                current_byterange = None
            else:
                segments.append({
                    "url": line,
                    "byterange": None,
                    "key_url": current_key_url,
                    "iv": current_iv,
                })

    return segments


def download_segment(seg: dict, seg_idx: int, attempts: int = 3) -> bytes | None:
    """
    Fetch one segment, verifying it arrived whole.

    A short body is retried: a network hiccup is transient, but a segment the
    CDN has stored truncated returns the same short body every time (observed:
    1080p segment 499 of evgaf5jqtgot serves 144 B of 457792 B for any request
    shape). Retrying twice distinguishes the two cheaply; a persistent short
    read is the caller's cue to fall back to another rendition.
    """
    url = seg["url"]
    headers = dict(HEADERS)

    expected = None
    if seg["byterange"]:
        length, offset = seg["byterange"]
        if offset is None:
            log.error(f"    Segment {seg_idx} has an unresolved byterange offset")
            return None
        headers["Range"] = f"bytes={offset}-{offset + length - 1}"
        expected = length

    last = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            if expected is not None and len(resp.content) != expected:
                last = (f"got {len(resp.content)}B of {expected}B "
                        f"(HTTP {resp.status_code}, "
                        f"Content-Range={resp.headers.get('Content-Range')})")
            else:
                return resp.content
        except Exception as e:
            last = f"{type(e).__name__}: {e}"

        if attempt < attempts:
            time.sleep(attempt)

    log.warning(f"    Segment {seg_idx} incomplete after {attempts} tries — {last}")
    return None


def record_missing(output_path: str, missing: list[int],
                   substituted: list[tuple[int, str]]) -> None:
    """
    Append a gap report next to the output tree.

    Segments the CDN cannot serve in ANY rendition are a defect upstream, so
    the indices are written out for whoever can purge and re-cache them.
    """
    report_path = os.path.join(os.path.dirname(output_path) or ".",
                               "missing_segments.json")
    entry = {
        "file": os.path.basename(output_path),
        "missing": missing,
        "substituted": [{"index": i, "from": q} for i, q in substituted],
    }
    try:
        existing = []
        if os.path.exists(report_path):
            with open(report_path, encoding="utf-8") as f:
                existing = json.load(f)
        existing = [e for e in existing if e.get("file") != entry["file"]]
        existing.append(entry)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        log.warning(f"  Could not write {report_path}: {type(e).__name__}: {e}")


_key_lock = threading.Lock()


def fetch_key(key_url: str, key_cache: dict) -> bytes:
    """Fetch an AES-128 key once. Called from worker threads, hence the lock."""
    with _key_lock:
        if key_url not in key_cache:
            kr = session.get(key_url, headers=HEADERS, timeout=15)
            kr.raise_for_status()
            key_cache[key_url] = kr.content
        return key_cache[key_url]


def fetch_decrypted(seg: dict, seg_idx: int, key_cache: dict) -> bytes | None:
    """Download one segment and undo its AES-128-CBC layer."""
    data = download_segment(seg, seg_idx)
    if data is None:
        return None

    if seg["key_url"]:
        key = fetch_key(seg["key_url"], key_cache)
        iv = seg["iv"] if seg["iv"] else seg_idx.to_bytes(16, "big")
        data = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
        pad_len = data[-1]
        if 0 < pad_len <= 16:
            data = data[:-pad_len]

    return data


def verify_output(path: str, expected_duration: float, dropped: int) -> bool:
    """
    Sanity-check the remuxed file against what the playlist promised.

    A container probe is nearly free and catches the failure that matters here:
    segments missing from the middle of the file. ffprobe reads headers only —
    it does not decode — so it will not notice a corrupt frame; the per-segment
    size check during download is what guards that.
    """
    try:
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type", "-of", "json", path],
            capture_output=True, text=True, timeout=60)
        info = json.loads(pr.stdout or "{}")
    except Exception as e:
        log.error(f"  Verify failed to probe: {type(e).__name__}: {e}")
        return False

    kinds = {s.get("codec_type") for s in info.get("streams", [])}
    if "video" not in kinds:
        log.error(f"  Verify failed: no video stream (streams: {kinds or 'none'})")
        return False

    try:
        actual = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        log.error("  Verify failed: ffprobe reported no duration")
        return False

    if expected_duration <= 0:
        return True

    drift = abs(actual - expected_duration)
    pct = drift / expected_duration * 100
    if pct > 2:
        log.error(f"  Verify FAILED: {actual:.0f}s vs {expected_duration:.0f}s "
                  f"expected — off by {drift:.0f}s ({pct:.1f}%)"
                  + (f", {dropped} segment(s) were dropped" if dropped else ""))
        return False

    return True


def build_fallback_chain(target: str, playlists: dict) -> list[tuple[str, str]]:
    """
    Order the renditions a truncated segment may be refilled from.

    Higher renditions are tried first, nearest first: filling 4 s of 720p from
    1080p costs only bytes, while filling it from below is visible. Lower ones
    come next, again nearest first, and stop at FALLBACK_FLOOR.

    So a 720p target reaches for 1080p, then 480p, and never 360p.
    """
    t = int(target)
    higher = sorted((q for q in playlists if int(q) > t), key=int)
    lower = sorted((q for q in playlists if FALLBACK_FLOOR <= int(q) < t),
                   key=int, reverse=True)
    return [(q, playlists[q]) for q in higher + lower]


def progress_bar(done: int, total: int, mb: float, elapsed: float,
                 label: str) -> None:
    """
    Redraw a single-line progress bar in place.

    Writes straight to stderr rather than through the logger: the log file
    would otherwise fill with thousands of half-finished lines, and stderr
    keeps the bar off stdout where the real log lines go. Skipped entirely
    when stderr is not a terminal, so redirected output stays clean.
    """
    if not sys.stderr.isatty():
        return

    frac = done / total if total else 0
    width = 24
    filled = int(width * frac)
    bar = "█" * filled + "░" * (width - filled)
    rate = mb / elapsed if elapsed else 0
    eta = (total - done) / (done / elapsed) if done and elapsed else 0
    sys.stderr.write(
        f"\r  [{label}] {bar} {frac * 100:3.0f}%  "
        f"{mb:6.0f} MB  {rate:4.0f} MB/s  ETA {eta:3.0f}s "
    )
    sys.stderr.flush()


def clear_progress_bar() -> None:
    """Wipe the bar so the next log line starts on a clean row."""
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 78 + "\r")
        sys.stderr.flush()


def download_video(m3u8_content: str, output_path: str, label: str = "video",
                   fallbacks: list[tuple[str, str]] | None = None,
                   job_num: int = 0, filename: str = "") -> bool:
    """
    Assemble one rendition into a .ts, then remux to MP4.

    `fallbacks` is [(quality, playlist_text), ...] tried in order — see
    build_fallback_chain. When the CDN has a segment stored truncated, that
    byte range is unrecoverable from this rendition no matter how often it is
    retried, so the same timestamp is taken from another rendition instead. All
    renditions share a segment count and timing, so index i is the same 4 s of
    video, and MPEG-TS tolerates a mid-stream resolution change — which is
    exactly what the site's own player does when it hits the defect (verified:
    it dropped 1080p->720p at t=1996 s rather than stalling).
    """
    t0 = time.time()
    segments = parse_segments(m3u8_content)
    total = len(segments)

    if total == 0:
        log.error(f"  [{label}] no segments in playlist")
        return False

    _d = dash.get() if _HAS_DASH else None
    if _d and _d.is_active():
        _d.dl_start(filename or os.path.basename(output_path), job_num, label.rstrip("p"))

    # Parse fallback playlists lazily — most videos never touch them.
    alts = []
    for alt_q, alt_text in (fallbacks or []):
        alt_segs = parse_segments(alt_text)
        if len(alt_segs) == total:
            alts.append((alt_q, alt_segs))
        else:
            log.warning(f"  [{alt_q}p] {len(alt_segs)} segments vs {total} — "
                        f"not usable as a fallback")

    key_cache = {}
    substituted = []
    missing = []

    # Fetch the AES key up front: with several threads running, doing it lazily
    # would have them all race to fetch the same key on the first segment.
    if segments[0]["key_url"]:
        fetch_key(segments[0]["key_url"], key_cache)

    def get(i):
        """Resolve segment i, falling back across renditions if truncated."""
        data = fetch_decrypted(segments[i], i, key_cache)
        if data is not None:
            return i, data, None

        for alt_q, alt_segs in alts:
            data = fetch_decrypted(alt_segs[i], i, key_cache)
            if data is not None:
                return i, data, alt_q
        return i, None, None

    with open(output_path + ".ts", "wb") as out:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            # map keeps results in submission order, so the .ts stays correctly
            # ordered while up to WORKERS requests are in flight. It also bounds
            # memory: results are consumed as they are produced, not buffered whole.
            last_draw = 0.0
            for i, data, from_q in pool.map(get, range(total)):
                if data is None:
                    # Dropping 4 s keeps the rest of the video usable; the gap
                    # is reported so the objects can be purged upstream.
                    missing.append(i)
                    continue

                if from_q:
                    substituted.append((i, from_q))
                out.write(data)

                # Throttled so a fast download does not spend its time on
                # terminal writes; the final state is drawn after the loop.
                now = time.time()
                if now - last_draw >= 0.2:
                    mb = out.tell() / 1048576
                    elapsed = now - t0
                    if _d and _d.is_active():
                        rate = mb / elapsed if elapsed else 0
                        eta = (total - (i + 1)) / ((i + 1) / elapsed) if i and elapsed else 0
                        _d.dl_progress(i + 1, total, mb, rate, eta, elapsed)
                    else:
                        progress_bar(i + 1, total, mb, elapsed, label)
                    last_draw = now

        if _d and _d.is_active():
            el = time.time() - t0
            mb = out.tell() / 1048576
            _d.dl_progress(total, total, mb, mb / el if el else 0, 0, el)
        else:
            progress_bar(total, total, out.tell() / 1048576, time.time() - t0, label)
    if not (_d and _d.is_active()):
        clear_progress_bar()

    if substituted:
        by_q = {}
        for idx, q in substituted:
            by_q.setdefault(q, []).append(idx)
        detail = ", ".join(f"{len(v)} from {k}p" for k, v in by_q.items())
        log.warning(f"  [{label}] {len(substituted)} segment(s) substituted ({detail})")

    if missing:
        log.error(f"  [{label}] {len(missing)} segment(s) lost entirely: {missing[:20]}"
                  f"{' ...' if len(missing) > 20 else ''}")
        record_missing(output_path, missing, substituted)

    # Remux TS → MP4 with ffmpeg
    ts_path = output_path + ".ts"
    cmd = [
        "ffmpeg", "-y",
        "-i", ts_path,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    os.remove(ts_path)

    if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
        size_mb = os.path.getsize(output_path) / (1024 * 1024)

        # The playlist states the duration outright, so compare against it: a
        # file short by whole segments is the failure this catches.
        expected = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", m3u8_content))
        if not verify_output(output_path, expected, len(missing)):
            os.remove(output_path)
            return False

        log.info(f"  [{label}] 100% — {size_mb:.0f} MB in {time.time() - t0:.0f}s")
        if _d and _d.is_active():
            _d.dl_note(f"✓ {filename or os.path.basename(output_path)} "
                       f"[{label}] {size_mb:.0f} MB in {time.time() - t0:.0f}s")
        return True
    else:
        stderr = result.stderr[-300:] if result.stderr else "no stderr"
        log.error(f"  [{label}] remux failed: {stderr}")
        if os.path.exists(output_path):
            os.remove(output_path)
        return False


def bucket_inventory(folder_name: str, filenames: list[str]) -> set[str]:
    """
    Ask the bucket once which of these files it already holds.

    get_bucket_paths_info accepts many paths per call, so the whole run's
    inventory costs one request instead of one per file. Returns the set of
    "quality/filename" keys present and complete.

    On failure this returns an empty set: nothing is skipped, so a bad lookup
    costs re-downloads rather than silently marking videos as done.
    """
    paths = [f"{folder_name}/{q}/{fn}" for q in QUALITIES for fn in filenames]
    if not paths:
        return set()

    try:
        info = list(get_bucket_paths_info(HF_BUCKET_ID, paths))
    except Exception as e:
        log.warning(f"  Bucket inventory failed ({type(e).__name__}: {e}) — "
                    f"nothing will be skipped")
        return set()

    prefix = f"{folder_name}/"
    present, partial = set(), 0
    for entry in info:
        path = getattr(entry, "path", None)
        if not path:
            continue
        # Objects at or under 1 KB are leftovers from an interrupted upload.
        if (getattr(entry, "size", 0) or 0) <= 1024:
            partial += 1
            continue
        present.add(path[len(prefix):] if path.startswith(prefix) else path)

    log.info(f"  {len(present)}/{len(paths)} already on HF"
             + (f", {partial} incomplete will be redone" if partial else ""))
    return present


def upload_file_to_hf(local_path: str, folder_name: str, quality: str, filename: str) -> bool:
    """
    Upload one file into the bucket via huggingface_hub.

    Uses sync_bucket rather than shelling out to `hf upload`: the CLI treats
    both of its positional args as local paths, so an `hf://buckets/...`
    destination is rejected. sync_bucket works on a directory, so the file is
    staged alone in a temp dir to avoid re-uploading siblings.
    """
    hf_dest_dir = f"{HF_BUCKET}/{folder_name}/{quality}"
    log.info(f"  [{quality}p] {filename} — uploading to HF")
    t0 = time.time()

    staging = None
    try:
        staging = tempfile.mkdtemp(prefix="hfup_")
        shutil.copy2(local_path, os.path.join(staging, filename))

        plan = sync_bucket(
            staging, hf_dest_dir,
            quiet=True,
            token=os.environ.get("HF_TOKEN") or True,  # reuse stored login if no env token
        )
        elapsed = time.time() - t0

        uploaded = [op for op in plan.operations if op.action == "upload"]
        if not uploaded:
            skipped = [op for op in plan.operations if op.action == "skip"]
            if skipped:
                log.info(f"  Already present — upload skipped ({elapsed:.0f}s)")
                return True
            log.error(f"  Upload produced no operations — nothing was sent")
            return False

        # Confirm the object is actually in the bucket rather than trusting the plan.
        # get_bucket_paths_info returns the logical size (metadata's `size` is the
        # deduplicated xet size, which differs and would false-negative).
        remote = f"{folder_name}/{quality}/{filename}"
        try:
            remote_info = list(get_bucket_paths_info(HF_BUCKET_ID, [remote]))
            if not remote_info:
                log.error(f"  Upload verification failed — {remote} not found in bucket")
                return False
            local_size = os.path.getsize(local_path)
            if remote_info[0].size != local_size:
                log.error(f"  Upload size mismatch: remote {remote_info[0].size}B vs "
                          f"local {local_size}B")
                return False
            log.info(f"  Uploaded in {elapsed:.0f}s "
                     f"({remote_info[0].size / 1048576:.0f} MB verified)")
            _d = dash.get() if _HAS_DASH else None
            if _d and _d.is_active():
                _d.up_note(f"✓ {filename} [{quality}p] "
                           f"{remote_info[0].size / 1048576:.0f} MB in {elapsed:.0f}s")
            return True
        except Exception as e:
            log.error(f"  Upload verification failed for {remote}: "
                      f"{type(e).__name__}: {e}")
            return False

    except Exception as e:
        log.error(f"  Upload FAILED in {time.time() - t0:.0f}s — {type(e).__name__}: {e}")
        return False
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def process_json(json_path: str, output_base: str = "./data"):
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # An optional "subject" nests the course under a top-level folder, e.g.
    # subject "فیزیک" + folder_name "شجاعی - سالیانه جامع 405" →
    # "فیزیک/شجاعی - سالیانه جامع 405". It is folded into folder_name here so
    # every downstream path (local dirs, HF bucket, inventory) stays two-level
    # without further changes. JSONs without a subject keep the flat layout.
    folder_name = data["folder_name"]
    subject = data.get("subject", "").strip()
    if subject:
        folder_name = f"{subject}/{folder_name}"
    files = data["files"]

    folder_path = os.path.join(output_base, *folder_name.split("/"))
    for q in QUALITIES:
        os.makedirs(os.path.join(folder_path, q), exist_ok=True)

    total_links = sum(len(f["links"]) for f in files)
    t0 = time.time()
    log.info(f"=== {os.path.basename(json_path)} — {len(files)} sessions, "
             f"{total_links} videos → {folder_name} ===")

    # Flatten to (filename, url) first so the bucket can be queried in one call.
    jobs = []
    for item in files:
        order = item["order"]
        links = item["links"]
        for part_idx, link in enumerate(links, start=1):
            # Zero-pad the session order to 3 digits so the files sort in
            # session order (001.mp4 < 010.mp4 < 100.mp4) instead of lexically
            # (1 < 10 < 100 …). The part index stays unpadded — at most a
            # handful per session.
            stem = f"{int(order):03d}"
            name = f"{stem}.mp4" if len(links) == 1 else f"{stem}-{part_idx}.mp4"
            jobs.append((name, link))

    present = bucket_inventory(folder_name, [name for name, _ in jobs])

    # --- Live dashboard (falls back to plain logging when not a TTY) ---
    board = None
    if _HAS_DASH:
        board = dash.init(total_links)
        board.start()
        if board.is_active():
            _attach_dashboard_logging(board)

    # --- Concurrent download + upload infrastructure ---
    # Unbounded queue: downloads never wait for uploads to catch up.
    upload_queue = queue.Queue()

    # A display-only mirror of what is waiting in (or moving through) the queue,
    # so the dashboard can list queued names without draining the real Queue.
    pending_names: list[str] = []
    pending_lock = threading.Lock()

    def _push_queue_view():
        if board and board.is_active():
            with pending_lock:
                # Stored as "filename|quality" for exact removal; show just the
                # filename in the queue list.
                board.set_queue([t.split("|", 1)[0] for t in pending_names])

    # Per-video accounting kept correct despite async uploads. A video is only
    # tallied once, after every rendition it spawned has finished uploading, so
    # a single video can never land in both "ok" and "failed".
    results = {"success": 0, "failed": 0, "skipped": 0}
    results_lock = threading.Lock()
    video_states = {}       # filename -> {ok, pending, download_done, finalized}
    vs_lock = threading.Lock()

    def maybe_finalize(filename):
        """Tally a video once its downloads are done and no uploads remain."""
        with vs_lock:
            st = video_states.get(filename)
            if not st or st["finalized"]:
                return
            if not (st["download_done"] and st["pending"] == 0):
                return
            st["finalized"] = True
            ok = st["ok"]
        with results_lock:
            results["success" if ok else "failed"] += 1

    # Upload worker: pulls from queue and uploads until the None sentinel.
    def upload_worker(worker_idx):
        while True:
            item = upload_queue.get()
            if item is None:  # shutdown signal
                upload_queue.task_done()
                break

            out_path, fld, quality, filename = item
            tag = f"{filename}|{quality}"
            with pending_lock:
                if tag in pending_names:
                    pending_names.remove(tag)
            if board and board.is_active():
                mb = os.path.getsize(out_path) / 1048576 if os.path.exists(out_path) else 0.0
                board.up_start(worker_idx, filename, quality, mb, time.monotonic())
            _push_queue_view()

            try:
                up_ok = upload_file_to_hf(out_path, fld, quality, filename)
                if up_ok:
                    os.remove(out_path)
                else:
                    log.error(f"  [{quality}p] upload failed — keeping local file")
                    with vs_lock:
                        video_states[filename]["ok"] = False
            except Exception as e:
                log.error(f"  [{quality}p] upload exception: {type(e).__name__}: {e}")
                with vs_lock:
                    video_states[filename]["ok"] = False
            finally:
                if board and board.is_active():
                    board.up_idle(worker_idx)
                with vs_lock:
                    video_states[filename]["pending"] -= 1
                upload_queue.task_done()
                maybe_finalize(filename)

    # Two upload workers pull from the queue in parallel.
    UPLOAD_WORKERS = int(os.environ.get("BIOMAZE_UPLOAD_WORKERS", "2"))
    upload_threads = []
    for idx in range(UPLOAD_WORKERS):
        t = threading.Thread(target=upload_worker, args=(idx,), daemon=True)
        t.start()
        upload_threads.append(t)

    done = 0

    # --- Main loop: extract + download, then hand each file to the queue ---
    for filename, link in jobs:
        done += 1

        # Decide what is left BEFORE launching a browser: extraction costs
        # ~25 s per video, so checking first turns a fully-uploaded run from
        # an hour of browser startups into a few seconds.
        todo = [q for q in QUALITIES if f"{q}/{filename}" not in present]
        if not todo:
            with results_lock:
                results["skipped"] += 1
            log.info(f"[{done}/{total_links}] ✓ {filename} — already on HF")
            if board and board.is_active():
                board.dl_note(f"✓ {filename} — already on HF")
            continue

        with vs_lock:
            video_states[filename] = {
                "ok": True, "pending": 0,
                "download_done": False, "finalized": False,
            }

        log.info(f"[{done}/{total_links}] ▶ {filename} "
                 f"(need: {', '.join(todo)})")

        if board and board.is_active():
            board.dl_extracting(filename, done)
        playlists = extract_m3u8_per_quality(link)
        if not playlists:
            log.error(f"  extraction failed: {link}")
            with vs_lock:
                video_states[filename]["ok"] = False
                video_states[filename]["download_done"] = True
            maybe_finalize(filename)
            continue

        for q in todo:
            if q not in playlists:
                log.warning(f"  [{q}p] not offered by the master playlist")
                with vs_lock:
                    video_states[filename]["ok"] = False
                continue

            out_path = os.path.join(folder_path, q, filename)

            # Higher renditions first, then lower ones down to FALLBACK_FLOOR.
            # Built from every rendition the manifest offers, not just those in
            # QUALITIES, so narrowing QUALITIES does not remove the recovery path.
            fallbacks = build_fallback_chain(q, playlists)
            dl_ok = download_video(playlists[q], out_path, label=f"{q}p",
                                   fallbacks=fallbacks, job_num=done,
                                   filename=filename)
            if not dl_ok:
                log.error(f"  [{q}p] download failed")
                with vs_lock:
                    video_states[filename]["ok"] = False
                continue

            # Download succeeded — hand off to the upload queue and move on to
            # the next download immediately, without waiting for the upload.
            with vs_lock:
                video_states[filename]["pending"] += 1
            with pending_lock:
                pending_names.append(f"{filename}|{q}")
            upload_queue.put((out_path, folder_name, q, filename))
            _push_queue_view()

        # All renditions for this video have been downloaded (or failed); mark
        # so the worker can finalize once its uploads drain.
        with vs_lock:
            video_states[filename]["download_done"] = True
        maybe_finalize(filename)
        if board and board.is_active():
            board.dl_idle()

    # --- Wait for all queued uploads to finish, then stop the workers ---
    if board and board.is_active():
        board.dl_done_all()
    log.info("  Download phase complete — waiting for uploads to finish...")
    upload_queue.join()
    for _ in range(UPLOAD_WORKERS):
        upload_queue.put(None)
    for t in upload_threads:
        t.join()

    summary = (f"=== DONE — ok {results['success']}, failed {results['failed']}, "
               f"skipped {results['skipped']} in {(time.time() - t0) / 60:.0f} min ===")
    was_active = bool(board and board.is_active())
    log.info(summary)

    if board:
        board.stop()

    # The dashboard ran on the alternate screen, which stop() just tore down —
    # so its suppressed-to-file summary never hit the screen. Echo it once to the
    # restored screen. (When the dashboard was inactive the normal stdout handler
    # already printed it, so don't print twice.)
    if was_active:
        print(summary)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python biomaze_downloader.py <json_path> [output_dir]")
        print("Example: python biomaze_downloader.py physics.json ./data")
        sys.exit(1)

    json_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "./data"
    process_json(json_path, output_dir)
