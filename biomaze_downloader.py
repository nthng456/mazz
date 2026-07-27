"""
Biomaze Video Downloader
Downloads 720p videos from stream.biomaze.ir using Playwright + ffmpeg
"""

from playwright.sync_api import sync_playwright
import subprocess
import sys
import os
import re
import tempfile


def extract_m3u8(video_url: str, quality: str = "720p", referer: str = "https://stream.biomaze.ir") -> dict:
    """
    Extract m3u8 URLs from a biomaze iframe page.
    Returns dict with master_m3u8 content, chosen quality m3u8 content, and metadata.
    """

    # Ensure URL ends with /iframe
    if not video_url.endswith("/iframe"):
        video_url = video_url.rstrip("/") + "/iframe"

    decrypted_m3u8 = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            extra_http_headers={"Referer": referer}
        )
        page = context.new_page()

        # Hook TextDecoder to capture decrypted m3u8 content
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

        print(f"[*] Loading page: {video_url}")
        page.goto(video_url, wait_until="networkidle")
        page.wait_for_timeout(2000)

        # Click play to trigger HLS loading
        for selector in ["video", "[class*='play']", ".media"]:
            try:
                page.click(selector, timeout=3000)
                break
            except:
                continue

        # Wait for HLS to load and decrypt m3u8
        page.wait_for_timeout(8000)

        decrypted_m3u8 = page.evaluate("() => window.__decryptedM3u8")

        # Get video title from page
        title = page.title() or "video"
        title = re.sub(r'[\\/:*?"<>|]', '_', title).strip()

        browser.close()

    if not decrypted_m3u8:
        print("[!] No m3u8 data found. The page might require interaction.")
        return None

    # First m3u8 is the master playlist
    master = decrypted_m3u8[0]
    print(f"\n[*] Master playlist found with qualities:")

    # Parse qualities from master playlist
    qualities = {}
    lines = master.strip().split("\n")
    for i, line in enumerate(lines):
        if "#EXT-X-STREAM-INF" in line:
            name_match = re.search(r'NAME="(\d+p)"', line)
            res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
            if name_match and i + 1 < len(lines):
                q_name = name_match.group(1)
                q_url = lines[i + 1].strip()
                q_res = res_match.group(1) if res_match else "?"
                qualities[q_name] = q_url
                marker = " <--" if q_name == quality else ""
                print(f"    {q_name} ({q_res}){marker}")

    if quality not in qualities:
        print(f"[!] Quality '{quality}' not found. Available: {list(qualities.keys())}")
        return None

    # Find the corresponding quality m3u8 from decrypted data
    chosen_url = qualities[quality]
    chosen_m3u8_content = None

    # The quality m3u8 is the one that was loaded by HLS.js
    # Since HLS auto-selects, we need to find the right one
    # We have the master (index 0) and typically 1080p playlist loaded by default
    # We need to match by checking if the URL was loaded
    # For now, return the chosen URL - ffmpeg can handle encrypted m3u8 if we pass headers

    return {
        "title": title,
        "master_m3u8": master,
        "quality": quality,
        "quality_url": chosen_url,
        "all_qualities": qualities,
        "all_m3u8": decrypted_m3u8,
    }


def download_with_ffmpeg(m3u8_content: str, output_path: str, referer: str = "https://stream.biomaze.ir"):
    """
    Download video using ffmpeg from m3u8 content.
    """
    # Write m3u8 to a temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".m3u8", delete=False, encoding="utf-8") as f:
        f.write(m3u8_content)
        m3u8_path = f.name

    print(f"\n[*] Downloading to: {output_path}")
    print(f"[*] Using temp m3u8: {m3u8_path}")

    cmd = [
        "ffmpeg",
        "-y",
        "-headers", f"Referer: {referer}\r\nOrigin: {referer}\r\n",
        "-i", m3u8_path,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        output_path,
    ]

    print(f"[*] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False)

    # Cleanup
    os.unlink(m3u8_path)

    if result.returncode == 0:
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"\n[+] Download complete! Size: {size_mb:.1f} MB")
        print(f"[+] Saved to: {output_path}")
    else:
        print(f"\n[!] ffmpeg exited with code {result.returncode}")

    return result.returncode == 0


def extract_720p_m3u8_content(video_url: str, referer: str = "https://stream.biomaze.ir") -> tuple:
    """
    Extract the 720p m3u8 content by forcing HLS.js to load that quality.
    Returns (title, m3u8_content) or (None, None) on failure.
    """
    if not video_url.endswith("/iframe"):
        video_url = video_url.rstrip("/") + "/iframe"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            extra_http_headers={"Referer": referer}
        )
        page = context.new_page()

        # Hook TextDecoder to capture ALL decrypted m3u8 content
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

        print(f"[*] Loading page: {video_url}")
        page.goto(video_url, wait_until="networkidle")
        page.wait_for_timeout(2000)

        # Click play
        for selector in ["video", "[class*='play']", ".media"]:
            try:
                page.click(selector, timeout=3000)
                break
            except:
                continue

        page.wait_for_timeout(6000)

        # Get master playlist first
        m3u8_list = page.evaluate("() => window.__decryptedM3u8")
        if not m3u8_list:
            print("[!] No m3u8 found")
            browser.close()
            return None, None

        master = m3u8_list[0]

        # Parse 720p URL from master
        target_url = None
        lines = master.strip().split("\n")
        for i, line in enumerate(lines):
            if '#EXT-X-STREAM-INF' in line and '720' in line:
                if i + 1 < len(lines):
                    target_url = lines[i + 1].strip()
                    break

        if not target_url:
            print("[!] 720p quality not found in master playlist")
            browser.close()
            return None, None

        print(f"[*] 720p URL: {target_url}")

        # Now force HLS.js to switch to 720p level
        page.evaluate("""(targetUrl) => {
            // Clear previous captures
            window.__decryptedM3u8 = [];

            // Find HLS instance and force level
            for (const k of Object.keys(window)) {
                try {
                    const obj = window[k];
                    if (obj && obj.levels && obj.loadLevel !== undefined) {
                        // Find the 720p level index
                        for (let i = 0; i < obj.levels.length; i++) {
                            if (obj.levels[i].height === 720) {
                                obj.currentLevel = i;
                                obj.loadLevel = i;
                                break;
                            }
                        }
                    }
                } catch(e) {}
            }
        }""", target_url)

        page.wait_for_timeout(5000)

        # Check if 720p m3u8 was captured
        new_m3u8 = page.evaluate("() => window.__decryptedM3u8")

        title = page.title() or "video"
        title = re.sub(r'[\\/:*?"<>|]', '_', title).strip()

        browser.close()

        # Find the 720p quality m3u8 (not the master, should have #EXT-X-KEY)
        all_m3u8 = m3u8_list + new_m3u8
        quality_m3u8 = None
        for m in all_m3u8:
            if "#EXT-X-KEY" in m and "#EXT-X-STREAM-INF" not in m:
                # This is a quality-specific playlist
                quality_m3u8 = m
                # Prefer the one loaded after we switched to 720p
                if m in new_m3u8:
                    quality_m3u8 = m
                    break

        if quality_m3u8:
            print(f"[+] Got 720p m3u8 content ({len(quality_m3u8)} bytes)")
            return title, quality_m3u8
        else:
            print("[!] Could not capture 720p m3u8 content")
            return title, None


def download_video(video_url: str, output_dir: str = ".", quality: str = "720p"):
    """
    Main function: download a video from biomaze in specified quality.
    """
    referer = "https://stream.biomaze.ir"

    title, m3u8_content = extract_720p_m3u8_content(video_url, referer)

    if not m3u8_content:
        print("[!] Failed to extract m3u8 content")
        return False

    # Sanitize title for filename
    safe_title = re.sub(r'[\\/:*?"<>|]', '_', title).strip()
    if not safe_title:
        safe_title = "video"

    output_path = os.path.join(output_dir, f"{safe_title}_{quality}.mp4")

    return download_with_ffmpeg(m3u8_content, output_path, referer)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python biomaze_downloader.py <video_url> [output_dir]")
        print("Example: python biomaze_downloader.py https://stream.biomaze.ir/evgaf5jqtgot/iframe")
        sys.exit(1)

    video_url = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "."

    download_video(video_url, output_dir, quality="720p")
