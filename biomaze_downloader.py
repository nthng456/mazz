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
import subprocess
import requests
import threading
import logging
import base64
import json
import sys
import os
import re
import shutil
import tempfile
import time

REFERER = "https://stream.biomaze.ir"
QUALITIES = ["720"]
HF_BUCKET = "hf://buckets/nthng454/Bucket"
HF_BUCKET_ID = HF_BUCKET.split("hf://buckets/", 1)[-1]  # "nthng454/Bucket"

# Segments are fetched this many at a time. Each is an independent ranged
# request, so they parallelise cleanly; the .ts is still assembled in order.
WORKERS = int(os.environ.get("BIOMAZE_WORKERS", "24"))

HEADERS = {
    "Referer": REFERER,
    "Origin": REFERER,
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("downloader.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("biomaze")

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


def extract_m3u8_per_quality(video_url: str) -> dict | None:
    """
    Returns {quality: playlist_text}, mapped explicitly from the master
    playlist's NAME= attribute — never guessed from segment sizes.
    """
    if not video_url.endswith("/iframe"):
        video_url = video_url.rstrip("/") + "/iframe"

    log.info(f"Extracting m3u8 from: {video_url}")
    t0 = time.time()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(extra_http_headers={"Referer": REFERER})
            page = context.new_page()
            page.add_init_script(INIT_SCRIPT)

            page.goto(video_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            for selector in [".media", "video", "[class*='play']"]:
                try:
                    page.click(selector, timeout=3000)
                    break
                except Exception:
                    continue

            page.wait_for_timeout(9000)

            cap = page.evaluate("() => window.__C")

            key_b64 = next((d["keyB64"] for d in cap["decrypts"]
                            if d.get("isM3u8") and d.get("keyB64")), None)
            if not key_b64:
                log.warning(f"No manifest key captured for {video_url}")
                browser.close()
                return None

            key = base64.b64decode(key_b64)
            log.info(f"Session manifest key captured ({len(key)} bytes)")

            master = None
            for r in cap["raw"]:
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

            log.info(f"Master declares qualities: {sorted(variants, key=int, reverse=True)}")

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
            fetched = page.evaluate("""async (urls) => {
                const out = {};
                for (const u of urls) {
                    try {
                        const r = await fetch(u);
                        out[u] = await r.text();
                    } catch (e) { out[u] = ''; }
                }
                return out;
            }""", list(wanted.values()))

            browser.close()

            result = {}
            for q, u in sorted(wanted.items(), key=lambda kv: int(kv[0]), reverse=True):
                raw = fetched.get(u) or ""
                if not raw:
                    log.warning(f"  [{q}p] empty manifest response")
                    continue
                text = decrypt_manifest(raw, key)
                if not text:
                    log.warning(f"  [{q}p] decrypt failed ({len(raw)} raw chars)")
                    continue

                segs = text.count("#EXTINF")
                dur = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text))
                size = sum(int(a) for a, _ in re.findall(r"#EXT-X-BYTERANGE:(\d+)(?:@(\d+))?", text))
                if "#EXT-X-ENDLIST" not in text:
                    log.warning(f"  [{q}p] playlist has no #EXT-X-ENDLIST — may be truncated")

                result[q] = text
                log.info(f"  [{q}p] {segs} segments, {dur:.0f}s, {size / 1048576:.0f} MB")

            # Distinct playlists are the guard against silently mapping the
            # same variant to several quality labels.
            if len(set(result.values())) != len(result):
                log.error("  Duplicate playlists across qualities — aborting")
                return None

            elapsed = time.time() - t0
            log.info(f"Extraction done in {elapsed:.1f}s — got: {list(result.keys())}")
            return result if result else None

    except Exception as e:
        log.error(f"Extraction failed for {video_url}: {e}", exc_info=True)
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
            log.info(f"  Encryption key fetched ({len(kr.content)} bytes)")
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
        log.info(f"  Verified: {actual:.0f}s, streams={sorted(kinds)}")
        return True

    drift = abs(actual - expected_duration)
    pct = drift / expected_duration * 100
    if pct > 2:
        log.error(f"  Verify FAILED: {actual:.0f}s vs {expected_duration:.0f}s "
                  f"expected — off by {drift:.0f}s ({pct:.1f}%)"
                  + (f", {dropped} segment(s) were dropped" if dropped else ""))
        return False

    log.info(f"  Verified: {actual:.0f}s vs {expected_duration:.0f}s expected "
             f"({pct:.1f}% drift), streams={sorted(kinds)}")
    return True


def download_video(m3u8_content: str, output_path: str,
                   fallbacks: list[tuple[str, str]] | None = None) -> bool:
    """
    Assemble one rendition into a .ts, then remux to MP4.

    `fallbacks` is [(quality, playlist_text), ...] in descending quality. When
    the CDN has a segment stored truncated, the byte range is unrecoverable
    from this rendition no matter how often it is retried, so the same
    timestamp is taken from the next rendition down. All renditions here share
    a segment count and timing, so index i maps to the same 4 s of video, and
    MPEG-TS tolerates a mid-stream resolution change — this is exactly what the
    site's own player does when it hits the defect (verified: it dropped
    1080p->720p at t=1996 s rather than stalling).
    """
    t0 = time.time()
    segments = parse_segments(m3u8_content)
    total = len(segments)
    log.info(f"  Downloading {total} segments...")

    if total == 0:
        log.error("  No segments found in playlist")
        return False

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
        """Resolve segment i, falling back down the renditions if truncated."""
        data = fetch_decrypted(segments[i], i, key_cache)
        if data is not None:
            return i, data, None

        for alt_q, alt_segs in alts:
            log.info(f"    Segment {i}: trying {alt_q}p instead")
            data = fetch_decrypted(alt_segs[i], i, key_cache)
            if data is not None:
                log.info(f"    Segment {i}: recovered from {alt_q}p ({len(data)}B)")
                return i, data, alt_q
        return i, None, None

    with open(output_path + ".ts", "wb") as out:
        last_pct = -1
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            # map keeps results in submission order, so the .ts stays correctly
            # ordered while up to WORKERS requests are in flight. It also bounds
            # memory: results are consumed as they are produced, not buffered whole.
            for i, data, from_q in pool.map(get, range(total)):
                if data is None:
                    # Dropping 4 s keeps the rest of the video usable; the gap
                    # is reported so the objects can be purged upstream.
                    missing.append(i)
                    want = segments[i]["byterange"][0] if segments[i]["byterange"] else "?"
                    log.error(f"    Segment {i}: unavailable in every rendition — "
                              f"skipping {want}B")
                    continue

                if from_q:
                    substituted.append((i, from_q))
                out.write(data)

                pct = int((i + 1) / total * 100)
                if pct >= last_pct + 5:
                    elapsed = time.time() - t0
                    mb = out.tell() / (1024 * 1024)
                    rate = mb / elapsed if elapsed else 0
                    log.info(f"  Progress: {pct}% ({i+1}/{total}) — "
                             f"{mb:.1f} MB in {elapsed:.0f}s ({rate:.1f} MB/s)")
                    last_pct = pct

    if substituted:
        by_q = {}
        for idx, q in substituted:
            by_q.setdefault(q, []).append(idx)
        detail = ", ".join(f"{len(v)} from {k}p" for k, v in by_q.items())
        log.warning(f"  {len(substituted)} segment(s) substituted ({detail})")

    if missing:
        log.error(f"  {len(missing)} segment(s) lost entirely: {missing[:20]}"
                  f"{' ...' if len(missing) > 20 else ''}")
        record_missing(output_path, missing, substituted)

    # Remux TS → MP4 with ffmpeg
    ts_path = output_path + ".ts"
    log.info(f"  Remuxing TS → MP4...")
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
        elapsed = time.time() - t0

        # The playlist states the duration outright, so compare against it: a
        # file short by whole segments is the failure this catches.
        expected = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", m3u8_content))
        if not verify_output(output_path, expected, len(missing)):
            os.remove(output_path)
            return False

        log.info(f"  Done: {size_mb:.1f} MB in {elapsed:.0f}s")
        return True
    else:
        stderr = result.stderr[-300:] if result.stderr else "no stderr"
        log.error(f"  Remux failed: {stderr}")
        if os.path.exists(output_path):
            os.remove(output_path)
        return False


def already_on_hf(folder_name: str, quality: str, filename: str) -> bool:
    """
    True if this file is already in the bucket, so the download can be skipped.

    Failures here return False: a network blip should cost a re-download, not
    silently mark a video as done when nothing was ever uploaded.
    """
    remote = f"{folder_name}/{quality}/{filename}"
    try:
        info = list(get_bucket_paths_info(HF_BUCKET_ID, [remote]))
    except Exception as e:
        log.warning(f"  [{quality}p] Could not check the bucket "
                    f"({type(e).__name__}: {e}) — downloading anyway")
        return False

    if not info:
        return False

    size = getattr(info[0], "size", 0) or 0
    if size <= 1024:
        log.warning(f"  [{quality}p] {remote} exists but is only {size}B "
                    f"— treating as incomplete, re-downloading")
        return False

    log.info(f"  [{quality}p] Already on HF ({size / 1048576:.1f} MB) — skipping")
    return True


def upload_file_to_hf(local_path: str, folder_name: str, quality: str, filename: str) -> bool:
    """
    Upload one file into the bucket via huggingface_hub.

    Uses sync_bucket rather than shelling out to `hf upload`: the CLI treats
    both of its positional args as local paths, so an `hf://buckets/...`
    destination is rejected. sync_bucket works on a directory, so the file is
    staged alone in a temp dir to avoid re-uploading siblings.
    """
    hf_dest_dir = f"{HF_BUCKET}/{folder_name}/{quality}"
    log.info(f"  Uploading: {filename} → {hf_dest_dir}/")
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
                log.info(f"  Upload skipped (already present) in {elapsed:.1f}s")
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
            log.info(f"  Upload OK in {elapsed:.1f}s "
                     f"({remote_info[0].size / 1048576:.1f} MB verified)")
            return True
        except Exception as e:
            log.error(f"  Upload verification failed for {remote}: "
                      f"{type(e).__name__}: {e}")
            return False

    except Exception as e:
        log.error(f"  Upload FAILED in {time.time() - t0:.1f}s — {type(e).__name__}: {e}")
        return False
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def process_json(json_path: str, output_base: str = "./data"):
    log.info(f"{'='*60}")
    log.info(f"Processing: {json_path}")
    log.info(f"Output base: {output_base}")
    log.info(f"HF Bucket: {HF_BUCKET}")
    log.info(f"{'='*60}")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    folder_name = data["folder_name"]
    files = data["files"]

    folder_path = os.path.join(output_base, folder_name)

    for q in QUALITIES:
        qpath = os.path.join(folder_path, q)
        os.makedirs(qpath, exist_ok=True)
        log.info(f"Directory ready: {qpath}")

    total_links = sum(len(f["links"]) for f in files)
    log.info(f"Total items: {len(files)} sessions, {total_links} video parts")

    done = 0
    success = 0
    failed = 0

    for item in files:
        order = item["order"]
        links = item["links"]

        for part_idx, link in enumerate(links, start=1):
            done += 1

            if len(links) == 1:
                filename = f"{order}.mp4"
            else:
                filename = f"{order}-{part_idx}.mp4"

            log.info(f"")
            log.info(f"[{done}/{total_links}] ▶ {filename}")
            log.info(f"  URL: {link}")

            playlists = extract_m3u8_per_quality(link)
            if not playlists:
                log.error(f"  SKIP — m3u8 extraction failed")
                failed += 1
                continue

            log.info(f"  Extracted qualities: {list(playlists.keys())}")

            all_ok = True

            for q in QUALITIES:
                if q not in playlists:
                    log.warning(f"  [{q}p] Not available")
                    continue

                out_path = os.path.join(folder_path, q, filename)

                if already_on_hf(folder_name, q, filename):
                    continue

                log.info(f"  [{q}p] Downloading...")

                # Every lower rendition the manifest offers, best first — not
                # just the ones in QUALITIES, so narrowing QUALITIES to a single
                # quality does not also remove the recovery path.
                fallbacks = [
                    (fq, playlists[fq])
                    for fq in sorted(playlists, key=int, reverse=True)
                    if int(fq) < int(q)
                ]
                dl_ok = download_video(playlists[q], out_path, fallbacks=fallbacks)
                if not dl_ok:
                    all_ok = False
                    log.error(f"  [{q}p] Download failed!")
                    continue

                up_ok = upload_file_to_hf(out_path, folder_name, q, filename)

                if up_ok:
                    os.remove(out_path)
                    log.info(f"  [{q}p] Local file deleted after upload")
                else:
                    all_ok = False
                    log.error(f"  [{q}p] Upload failed — keeping local file")

            if all_ok:
                success += 1
            else:
                failed += 1

            log.info(f"  Progress: {done}/{total_links} | ok: {success} | failed: {failed}")

    log.info(f"")
    log.info(f"{'='*60}")
    log.info(f"FINISHED: {json_path}")
    log.info(f"  Total: {total_links} | Success: {success} | Failed: {failed}")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python biomaze_downloader.py <json_path> [output_dir]")
        print("Example: python biomaze_downloader.py physics.json ./data")
        sys.exit(1)

    json_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "./data"
    process_json(json_path, output_dir)
