"""
Biomaze Video Downloader
Downloads videos from stream.biomaze.ir using Playwright + ffmpeg
Uploads to Hugging Face Bucket, then deletes local file to save disk
"""

from playwright.sync_api import sync_playwright
import subprocess
import logging
import json
import sys
import os
import re
import tempfile
import time

REFERER = "https://stream.biomaze.ir"
QUALITIES = ["1080", "720", "480"]
HF_BUCKET = "hf://buckets/StellarWeight/Bucket"

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


def extract_all_m3u8(video_url: str) -> dict | None:
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

            page.wait_for_timeout(6000)

            m3u8_list = page.evaluate("() => window.__decryptedM3u8")
            if not m3u8_list:
                log.warning(f"No m3u8 captured for {video_url}")
                browser.close()
                return None

            master = m3u8_list[0]
            log.debug(f"Master playlist:\n{master[:300]}")

            qualities = {}
            lines = master.strip().split("\n")
            for i, line in enumerate(lines):
                if "#EXT-X-STREAM-INF" in line:
                    name_match = re.search(r'NAME="(\d+)p"', line)
                    if name_match and i + 1 < len(lines):
                        q = name_match.group(1)
                        qualities[q] = lines[i + 1].strip()

            log.info(f"Master has qualities: {list(qualities.keys())}")

            default_m3u8 = m3u8_list[1] if len(m3u8_list) > 1 else None
            result = {"master": master, "qualities": qualities, "playlists": {}}

            for q in QUALITIES:
                if q not in qualities:
                    continue

                page.evaluate("() => { window.__decryptedM3u8 = []; }")

                page.evaluate(f"""() => {{
                    for (const k of Object.keys(window)) {{
                        try {{
                            const obj = window[k];
                            if (obj && obj.levels && obj.loadLevel !== undefined) {{
                                for (let i = 0; i < obj.levels.length; i++) {{
                                    if (obj.levels[i].height === {q}) {{
                                        obj.currentLevel = i;
                                        obj.loadLevel = i;
                                        break;
                                    }}
                                }}
                            }}
                        }} catch(e) {{}}
                    }}
                }}""")

                page.wait_for_timeout(4000)

                new_m3u8 = page.evaluate("() => window.__decryptedM3u8")
                for m in new_m3u8:
                    if "#EXT-X-KEY" in m and "#EXT-X-STREAM-INF" not in m:
                        result["playlists"][q] = m
                        log.info(f"  Captured {q}p playlist ({len(m)} bytes)")
                        break

            if default_m3u8 and "#EXT-X-KEY" in default_m3u8:
                for q in QUALITIES:
                    if q not in result["playlists"] and q in qualities:
                        result["playlists"][q] = default_m3u8
                        log.info(f"  Using default playlist for {q}p")

            browser.close()
            elapsed = time.time() - t0
            log.info(f"Extraction done in {elapsed:.1f}s — got {list(result['playlists'].keys())}")
            return result

    except Exception as e:
        log.error(f"Extraction failed for {video_url}: {e}", exc_info=True)
        return None


def download_with_ffmpeg(m3u8_content: str, output_path: str) -> bool:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".m3u8", delete=False, encoding="utf-8") as f:
        f.write(m3u8_content)
        m3u8_path = f.name

    log.info(f"  ffmpeg → {output_path}")
    t0 = time.time()

    cmd = [
        "ffmpeg", "-y",
        "-headers", f"Referer: {REFERER}\r\nOrigin: {REFERER}\r\n",
        "-i", m3u8_path,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(m3u8_path)
    elapsed = time.time() - t0

    if result.returncode == 0:
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        log.info(f"  ffmpeg done: {size_mb:.1f} MB in {elapsed:.1f}s")
        return True
    else:
        stderr_tail = result.stderr[-500:] if result.stderr else "no stderr"
        log.error(f"  ffmpeg FAILED (code {result.returncode}) in {elapsed:.1f}s\n{stderr_tail}")
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

            # Step 1: Extract m3u8 for all qualities
            m3u8_data = extract_all_m3u8(link)
            if not m3u8_data:
                log.error(f"  SKIP — m3u8 extraction failed")
                failed += 1
                continue

            available = list(m3u8_data["playlists"].keys())
            log.info(f"  Extracted playlists: {available}")

            all_ok = True

            for q in QUALITIES:
                if q not in m3u8_data["playlists"]:
                    log.warning(f"  [{q}p] Not available")
                    continue

                out_path = os.path.join(folder_path, q, filename)

                # Step 2: Download with ffmpeg
                log.info(f"  [{q}p] Downloading...")
                dl_ok = download_with_ffmpeg(m3u8_data["playlists"][q], out_path)
                if not dl_ok:
                    all_ok = False
                    log.error(f"  [{q}p] Download failed!")
                    continue

                # Step 3: Upload to HF bucket
                hf_dest = f"{HF_BUCKET}/{folder_name}/{q}/{filename}"
                up_ok = upload_file_to_hf(out_path, hf_dest)

                if up_ok:
                    # Step 4: Delete local file after successful upload
                    os.remove(out_path)
                    log.info(f"  [{q}p] Local file deleted: {out_path}")
                else:
                    all_ok = False
                    log.error(f"  [{q}p] Upload failed — keeping local file: {out_path}")

            if all_ok:
                success += 1
            else:
                failed += 1

            log.info(f"  Progress: {done}/{total_links} done | {success} ok | {failed} failed")

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
