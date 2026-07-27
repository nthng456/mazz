from playwright.sync_api import sync_playwright
import json

url = "https://stream.biomaze.ir/evgaf5jqtgot/iframe"
referer = "https://stream.biomaze.ir"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(
        extra_http_headers={"Referer": referer}
    )
    page = context.new_page()

    # Intercept the HLS.js pLoader to capture decrypted m3u8 content
    page.add_init_script("""
        window.__decryptedM3u8 = [];
        window.__hlsConfig = [];

        // Hook into HLS.js - intercept the manifest parsing
        const origTextDecoder = TextDecoder.prototype.decode;
        TextDecoder.prototype.decode = function(...args) {
            const result = origTextDecoder.apply(this, args);
            if (result && typeof result === 'string' && result.includes('#EXTM3U')) {
                window.__decryptedM3u8.push(result);
            }
            return result;
        };
    """)

    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(2000)

    # Click play
    try:
        page.click("video", timeout=3000)
    except:
        pass
    try:
        page.click('[class*="play"]', timeout=3000)
    except:
        pass
    try:
        page.click('.media', timeout=3000)
    except:
        pass

    page.wait_for_timeout(8000)

    # Get decrypted m3u8 content
    decrypted = page.evaluate("() => window.__decryptedM3u8")
    print(f"=== DECRYPTED M3U8 ({len(decrypted)} files) ===")
    for i, d in enumerate(decrypted):
        print(f"\n--- M3U8 #{i} ---")
        print(d[:2000])

    # Also get Vis3 player config
    vis_config = page.evaluate("""() => {
        // The embed page pushes config to window.__vis3 or similar
        const results = {};
        if (window.__vis3) results.__vis3 = JSON.stringify(window.__vis3);
        if (window.Vis3) results.Vis3 = 'exists';

        // Find the config array that was pushed
        for (const k of Object.keys(window)) {
            try {
                const v = window[k];
                if (Array.isArray(v) && v.length > 0 && typeof v[0] === 'object') {
                    const first = v[0];
                    if (first && (first.sources || first.file || first.src)) {
                        results['array_' + k] = JSON.stringify(v).substring(0, 2000);
                    }
                }
            } catch(e) {}
        }

        return results;
    }""")
    print("\n=== VIS3 CONFIG ===")
    for k, v in vis_config.items():
        print(f"  {k}: {v}")

    browser.close()
