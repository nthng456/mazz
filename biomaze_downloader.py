"""
Biomaze Video Downloader
Downloads videos from stream.biomaze.ir using Playwright + ffmpeg
Uploads to Hugging Face Bucket, then deletes local file to save disk
"""

from playwright.sync_api import sync_playwright
from http.server import HTTPServer, BaseHTTPRequestHandler
import subprocess
import threading
import logging
import json
import sys
import os
import re

import time
import urllib.request

REFERER = "https://stream.biomaze.ir"
QUALITIES = ["1080", "720", "480"]
HF_BUCKET = "hf://buckets/nthng454/Bucket"

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


def extract_m3u8_per_quality(video_url: str) -> dict | None:
    """
    Opens the video page, captures the master m3u8,
    then fetches each quality's playlist via network intercept.
    Returns {quality: m3u8_content} or None on failure.
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

            captured_playlists = {}
            master_content = [None]

            def on_response(response):
                url = response.url
                if ".m3u8" in url:
                    try:
                        body = response.text()
                        if "#EXT-X-STREAM-INF" in body:
                            master_content[0] = body
                        elif "#EXTINF" in body:
                            captured_playlists[url] = body
                    except:
                        pass

            page.on("response", on_response)

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

            # Get decrypted m3u8 from TextDecoder hook
            decrypted = page.evaluate("() => window.__decryptedM3u8")

            if not decrypted:
                log.warning(f"No m3u8 captured for {video_url}")
                browser.close()
                return None

            # First one is the master
            master = decrypted[0]
            log.info(f"Master playlist captured ({len(master)} bytes)")

            # Parse quality URLs from master
            quality_urls = {}
            lines = master.strip().split("\n")
            for i, line in enumerate(lines):
                if "#EXT-X-STREAM-INF" in line:
                    name_match = re.search(r'NAME="(\d+)p"', line)
                    if name_match and i + 1 < len(lines):
                        q = name_match.group(1)
                        quality_urls[q] = lines[i + 1].strip()

            log.info(f"Master has qualities: {list(quality_urls.keys())}")

            # The remaining decrypted m3u8s are quality playlists (decrypted)
            # We need to match them to the right quality
            # Strategy: the byte ranges / segment sizes differ per quality
            # Bigger byteranges = higher quality
            decrypted_playlists = [d for d in decrypted[1:] if "#EXTINF" in d]
            log.info(f"Captured {len(decrypted_playlists)} decrypted playlists")

            # For each quality URL in master, try to fetch it directly via page
            result = {}
            for q in QUALITIES:
                if q not in quality_urls:
                    log.warning(f"  {q}p not in master, skipping")
                    continue

                q_url = quality_urls[q]
                log.info(f"  Fetching {q}p playlist: {q_url}")

                try:
                    resp = page.evaluate(f"""async () => {{
                        const r = await fetch("{q_url}", {{
                            headers: {{"Referer": "{REFERER}"}}
                        }});
                        return await r.text();
                    }}""")

                    if resp and "#EXTINF" in resp:
                        # This is the encrypted version from server
                        # But the decrypted version from TextDecoder is what we need
                        # The server response is the actual valid m3u8 with proper URLs
                        result[q] = resp
                        log.info(f"  {q}p playlist fetched OK ({len(resp)} bytes)")
                    else:
                        log.warning(f"  {q}p fetch returned invalid content")
                except Exception as e:
                    log.warning(f"  {q}p fetch failed: {e}")

            # Fallback: if fetch didn't work, use decrypted ones
            if not result and decrypted_playlists:
                log.info("  Falling back to decrypted playlists")
                # Sort by avg byterange size (bigger = higher quality)
                def avg_byterange(m3u8_text):
                    ranges = re.findall(r'#EXT-X-BYTERANGE:(\d+)', m3u8_text)
                    if not ranges:
                        return 0
                    return sum(int(r) for r in ranges) / len(ranges)

                sorted_playlists = sorted(decrypted_playlists, key=avg_byterange, reverse=True)

                available_q = sorted([q for q in QUALITIES if q in quality_urls],
                                     key=lambda x: int(x), reverse=True)

                for i, q in enumerate(available_q):
                    if i < len(sorted_playlists):
                        result[q] = sorted_playlists[i]
                        avg_br = avg_byterange(sorted_playlists[i])
                        log.info(f"  Mapped {q}p ← decrypted playlist (avg byterange: {avg_br:.0f})")

            browser.close()
            elapsed = time.time() - t0
            log.info(f"Extraction done in {elapsed:.1f}s — got qualities: {list(result.keys())}")
            return result if result else None

    except Exception as e:
        log.error(f"Extraction failed for {video_url}: {e}", exc_info=True)
        return None


class M3U8ProxyHandler(BaseHTTPRequestHandler):
    """Local proxy that serves m3u8 content and proxies segment/key requests with Referer."""
    m3u8_content = ""

    def do_GET(self):
        if self.path == "/playlist.m3u8":
            data = self.server.m3u8_content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/proxy?url="):
            target_url = self.path[len("/proxy?url="):]
            try:
                req = urllib.request.Request(target_url, headers={
                    "Referer": REFERER,
                    "Origin": REFERER,
                    "User-Agent": "Mozilla/5.0",
                })
                if "Range" in self.headers:
                    req.add_header("Range", self.headers["Range"])
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read()
                    self.send_response(resp.status)
                    for h in ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"]:
                        val = resp.getheader(h)
                        if val:
                            self.send_header(h, val)
                    self.end_headers()
                    self.wfile.write(body)
            except Exception as e:
                self.send_response(502)
                self.end_headers()
                self.wfile.write(str(e).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def rewrite_m3u8_urls(m3u8_content: str, proxy_base: str) -> str:
    """Rewrite all https:// URLs in m3u8 to go through our local proxy."""
    def replace_url(match):
        url = match.group(0)
        return f"{proxy_base}/proxy?url={url}"
    return re.sub(r'https?://[^\s\r\n]+', replace_url, m3u8_content)


def download_with_ffmpeg(m3u8_content: str, output_path: str) -> bool:
    port = 18899
    server = HTTPServer(("127.0.0.1", port), M3U8ProxyHandler)
    server.m3u8_content = rewrite_m3u8_urls(m3u8_content, f"http://127.0.0.1:{port}")
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    log.info(f"  ffmpeg → {output_path} (via proxy :{port})")
    t0 = time.time()

    cmd = [
        "ffmpeg", "-y",
        "-i", f"http://127.0.0.1:{port}/playlist.m3u8",
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    server.shutdown()
    elapsed = time.time() - t0

    if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        log.info(f"  ffmpeg done: {size_mb:.1f} MB in {elapsed:.1f}s")
        return True
    else:
        stderr_tail = result.stderr[-500:] if result.stderr else "no stderr"
        log.error(f"  ffmpeg FAILED (code {result.returncode}) in {elapsed:.1f}s\n{stderr_tail}")
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


def sync_folder_to_hf(local_dir: str) -> bool:
    cmd = ["hf", "sync", local_dir, HF_BUCKET]
    log.info(f"Syncing folder: {local_dir} → {HF_BUCKET}")
    t0 = time.time()

    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0

    if result.returncode == 0:
        log.info(f"Sync OK in {elapsed:.1f}s")
        return True
    else:
        stderr_tail = result.stderr[-300:] if result.stderr else "no stderr"
        log.error(f"Sync FAILED (code {result.returncode}) in {elapsed:.1f}s\n{stderr_tail}")
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
                dl_ok = download_with_ffmpeg(playlists[q], out_path)
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
