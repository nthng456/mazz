"""
Biomaze Video Downloader
Downloads videos from stream.biomaze.ir using Playwright
Segments downloaded directly with requests, decrypted with pycryptodome
Uploads to Hugging Face Bucket, then deletes local file to save disk
"""

from playwright.sync_api import sync_playwright
from Crypto.Cipher import AES
import subprocess
import requests
import logging
import json
import sys
import os
import re
import time

REFERER = "https://stream.biomaze.ir"
QUALITIES = ["1080", "720", "480"]
HF_BUCKET = "hf://buckets/nthng454/Bucket"

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


def extract_m3u8_per_quality(video_url: str) -> dict | None:
    if not video_url.endswith("/iframe"):
        video_url = video_url.rstrip("/") + "/iframe"

    log.info(f"Extracting m3u8 from: {video_url}")
    t0 = time.time()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(extra_http_headers={"Referer": REFERER})
            page = context.new_page()

            page.add_init_script("""
                window.__decryptedM3u8 = [];
                const origDecode = TextDecoder.prototype.decode;
                TextDecoder.prototype.decode = function(...args) {
                    const result = origDecode.apply(this, args);
                    if (result && typeof result === 'string' && result.includes('#EXTM3U')) {
                        window.__decryptedM3u8.push(result);
                    }
                    return result;
                };
            """)

            page.goto(video_url, wait_until="networkidle")
            page.wait_for_timeout(2000)

            for selector in ["video", "[class*='play']", ".media"]:
                try:
                    page.click(selector, timeout=3000)
                    break
                except:
                    continue

            page.wait_for_timeout(8000)

            decrypted = page.evaluate("() => window.__decryptedM3u8")
            browser.close()

            if not decrypted:
                log.warning(f"No m3u8 captured for {video_url}")
                return None

            master = decrypted[0]
            log.info(f"Master playlist captured ({len(master)} bytes)")

            quality_urls = {}
            lines = master.strip().split("\n")
            for i, line in enumerate(lines):
                if "#EXT-X-STREAM-INF" in line:
                    name_match = re.search(r'NAME="(\d+)p"', line)
                    if name_match and i + 1 < len(lines):
                        q = name_match.group(1)
                        quality_urls[q] = lines[i + 1].strip()

            log.info(f"Master has qualities: {list(quality_urls.keys())}")

            decrypted_playlists = [d for d in decrypted[1:] if "#EXTINF" in d]
            log.info(f"Captured {len(decrypted_playlists)} decrypted playlists")

            def avg_byterange(m3u8_text):
                ranges = re.findall(r'#EXT-X-BYTERANGE:(\d+)', m3u8_text)
                if not ranges:
                    return 0
                return sum(int(r) for r in ranges) / len(ranges)

            sorted_playlists = sorted(decrypted_playlists, key=avg_byterange, reverse=True)

            available_q = sorted(
                [q for q in QUALITIES if q in quality_urls],
                key=lambda x: int(x), reverse=True,
            )

            result = {}
            for i, q in enumerate(available_q):
                if i < len(sorted_playlists):
                    result[q] = sorted_playlists[i]
                    log.info(f"  Mapped {q}p (avg byterange: {avg_byterange(sorted_playlists[i]):.0f})")

            elapsed = time.time() - t0
            log.info(f"Extraction done in {elapsed:.1f}s — got: {list(result.keys())}")
            return result if result else None

    except Exception as e:
        log.error(f"Extraction failed for {video_url}: {e}", exc_info=True)
        return None


def parse_segments(m3u8_content: str):
    """Parse m3u8 playlist and return list of (segment_url, byterange, key_url, iv)."""
    lines = m3u8_content.strip().split("\n")
    segments = []
    current_key_url = None
    current_iv = None
    current_byterange = None

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
            segments.append({
                "url": line,
                "byterange": current_byterange,
                "key_url": current_key_url,
                "iv": current_iv,
            })
            current_byterange = None

    return segments


def download_segment(seg: dict, seg_idx: int) -> bytes | None:
    url = seg["url"]
    headers = dict(HEADERS)

    if seg["byterange"]:
        length, offset = seg["byterange"]
        if offset is not None:
            end = offset + length - 1
            headers["Range"] = f"bytes={offset}-{end}"

    try:
        resp = session.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        log.error(f"    Segment {seg_idx} download failed: {e}")
        return None


def download_video(m3u8_content: str, output_path: str) -> bool:
    t0 = time.time()
    segments = parse_segments(m3u8_content)
    total = len(segments)
    log.info(f"  Downloading {total} segments...")

    if total == 0:
        log.error("  No segments found in playlist")
        return False

    # Download encryption key once
    key_cache = {}

    with open(output_path + ".ts", "wb") as out:
        last_pct = -1
        for i, seg in enumerate(segments):
            data = download_segment(seg, i)
            if data is None:
                log.error(f"  Aborting at segment {i}/{total}")
                return False

            # Decrypt if needed
            if seg["key_url"]:
                if seg["key_url"] not in key_cache:
                    kr = session.get(seg["key_url"], headers=HEADERS, timeout=15)
                    kr.raise_for_status()
                    key_cache[seg["key_url"]] = kr.content
                    log.info(f"  Encryption key fetched ({len(kr.content)} bytes)")

                key = key_cache[seg["key_url"]]
                iv = seg["iv"] if seg["iv"] else i.to_bytes(16, "big")
                cipher = AES.new(key, AES.MODE_CBC, iv)
                data = cipher.decrypt(data)
                # Remove PKCS7 padding
                pad_len = data[-1]
                if 0 < pad_len <= 16:
                    data = data[:-pad_len]

            out.write(data)

            # Progress every 5%
            pct = int((i + 1) / total * 100)
            if pct >= last_pct + 5:
                elapsed = time.time() - t0
                mb = out.tell() / (1024 * 1024)
                log.info(f"  Progress: {pct}% ({i+1}/{total}) — {mb:.1f} MB in {elapsed:.0f}s")
                last_pct = pct

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
        log.info(f"  Done: {size_mb:.1f} MB in {elapsed:.0f}s")
        return True
    else:
        stderr = result.stderr[-300:] if result.stderr else "no stderr"
        log.error(f"  Remux failed: {stderr}")
        if os.path.exists(output_path):
            os.remove(output_path)
        return False


def upload_file_to_hf(local_path: str, hf_dest: str) -> bool:
    cmd = ["hf", "upload", local_path, hf_dest]
    log.info(f"  Uploading: {os.path.basename(local_path)} → {hf_dest}")
    t0 = time.time()

    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0

    if result.returncode == 0:
        log.info(f"  Upload OK in {elapsed:.1f}s")
        return True
    else:
        stderr_tail = result.stderr[-300:] if result.stderr else "no stderr"
        log.error(f"  Upload FAILED (code {result.returncode}) in {elapsed:.1f}s\n{stderr_tail}")
        return False


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

                log.info(f"  [{q}p] Downloading...")
                dl_ok = download_video(playlists[q], out_path)
                if not dl_ok:
                    all_ok = False
                    log.error(f"  [{q}p] Download failed!")
                    continue

                hf_dest = f"{HF_BUCKET}/{folder_name}/{q}/{filename}"
                up_ok = upload_file_to_hf(out_path, hf_dest)

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
