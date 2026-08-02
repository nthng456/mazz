"""Deep recon: does the site's OWN player get past 1080p segment 499?

Established so far, offline, from the raw manifest text:
  - the playlist is well-formed: 1 EXT-X-KEY, 1279 EXTINF/BYTERANGE pairs, ENDLIST
  - lines 1505/1506 really do say 457792@235354992 for segment 499
  - our parser reproduces that byte-for-byte, so this is not a parsing bug
  - every segment URL serves only its own range; the range is not routable
  - that range returns 144 B for any request shape, repeatably

Never tested until now: what the real player does at that timestamp. If the
browser sails past t=1996 s on the 1080p rendition, our download method is
wrong. If the browser stalls or silently drops down a rendition, the stored
object really is truncated.

Network capture uses Playwright's own response events rather than JS hooks,
so requests issued from inside a Worker are still observed.
"""
import re
import sys
import time

from playwright.sync_api import sync_playwright

URL = "https://stream.biomaze.ir/evgaf5jqtgot/iframe"
REFERER = "https://stream.biomaze.ir"
CDN = "c-d-n.io"

# The suspect segment: index 499 of 1279, 4 s each -> covers t=1996..2000 s.
TOKEN = "a410d628a89559e8e3ee6430a8a7d70f"
SEEK_TO = 1990.0
BAD_START = 235354992
BAD_LEN = 457792

hits = []


def on_response(resp):
    if CDN not in resp.url:
        return
    try:
        rng = resp.request.headers.get("range")
        clen = resp.headers.get("content-length")
        crange = resp.headers.get("content-range")
    except Exception:
        rng = clen = crange = None
    hits.append({
        "url": resp.url,
        "status": resp.status,
        "range": rng,
        "content_range": crange,
        "length": int(clen) if clen and clen.isdigit() else None,
        "t": time.time(),
    })


def short_by(h):
    """Bytes missing from a ranged response, or 0 if it was served whole."""
    m = re.match(r"bytes=(\d+)-(\d+)", h["range"] or "")
    if not m or h["length"] is None:
        return 0
    want = int(m.group(2)) - int(m.group(1)) + 1
    return want - h["length"]


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(extra_http_headers={"Referer": REFERER})
    page = context.new_page()
    page.on("response", on_response)

    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    for sel in [".media", "video", "[class*='play']"]:
        try:
            page.click(sel, timeout=3000)
            break
        except Exception:
            continue

    page.wait_for_timeout(8000)

    state0 = page.evaluate("""() => {
        const v = document.querySelector('video');
        if (!v) return null;
        return {t: v.currentTime, dur: v.duration, paused: v.paused,
                w: v.videoWidth, h: v.videoHeight};
    }""")
    print("after initial play:", state0)

    if not state0:
        browser.close()
        sys.exit("no <video> element found")

    # Pin the top rendition, otherwise ABR can quietly drop to a variant whose
    # segment 499 is intact and the test proves nothing.
    pinned = page.evaluate("""() => {
        const seen = [];
        for (const k of Object.keys(window)) {
            let o;
            try { o = window[k]; } catch (e) { continue; }
            if (o && o.levels && typeof o.currentLevel === 'number') {
                const top = o.levels.length - 1;
                try {
                    o.autoLevelEnabled = false;
                    o.currentLevel = top;
                    o.loadLevel = top;
                    o.nextLevel = top;
                    seen.push({key: k, levels: o.levels.map(l => l.height), pinned: top});
                } catch (e) { seen.push({key: k, error: String(e)}); }
            }
        }
        return seen;
    }""")
    print("hls handles:", pinned)

    n_before = len(hits)
    print(f"\nseeking to t={SEEK_TO}s — segment 499 covers 1996-2000s ...")
    page.evaluate(
        "(t) => { const v = document.querySelector('video');"
        " v.currentTime = t; v.play().catch(() => {}); }", SEEK_TO)

    print("\n  wall  currentTime  readyState  buffered-end  height")
    passed = False
    last_t = None
    for i in range(25):
        page.wait_for_timeout(1000)
        st = page.evaluate("""() => {
            const v = document.querySelector('video');
            let be = null;
            if (v.buffered.length) be = v.buffered.end(v.buffered.length - 1);
            return {t: v.currentTime, rs: v.readyState, be: be,
                    err: v.error ? v.error.code : null, h: v.videoHeight};
        }""")
        be = f"{st['be']:.2f}" if st["be"] is not None else "-"
        err = f"  ERR={st['err']}" if st["err"] else ""
        print(f"  {i + 1:>4}s  {st['t']:>10.2f}  {st['rs']:>10}  {be:>12}  "
              f"{st['h']:>5}{err}")
        last_t = st["t"]
        if st["t"] >= 2004:
            passed = True
            print(f"\n  -> playhead passed segment 499 (reached {st['t']:.2f}s)")
            break

    final_h = page.evaluate("() => document.querySelector('video').videoHeight")
    browser.close()

print(f"\nfinal videoHeight={final_h}   last currentTime={last_t}")

after = hits[n_before:]
print(f"\nCDN responses: total={len(hits)} after-seek={len(after)}")

print("\n--- ranged responses after the seek (SHORT = server under-delivered) ---")
shown = 0
for h in after:
    miss = short_by(h)
    if h["range"] is None and h["status"] == 200:
        continue
    flag = f"   <<< SHORT by {miss}" if miss > 0 else ""
    if miss > 0 or shown < 25:
        print(f"  {h['status']}  range={h['range']}  got={h['length']}{flag}")
        shown += 1
print(f"  ({len(after)} responses total)")

tok = [h for h in hits if TOKEN in h["url"]]
print(f"\n--- the player's own requests for the SUSPECT segment: {len(tok)} ---")
for h in tok:
    print(f"  status={h['status']} range={h['range']} got={h['length']} "
          f"content-range={h['content_range']}")
if not tok:
    print("  the player never requested it")

overlap = []
for h in hits:
    m = re.match(r"bytes=(\d+)-(\d+)", h["range"] or "")
    if m:
        s, e = int(m.group(1)), int(m.group(2))
        if s < BAD_START + BAD_LEN and e >= BAD_START:
            overlap.append((s, e, h["length"], h["status"]))
print(f"\n--- requests overlapping bytes {BAD_START}..{BAD_START + BAD_LEN - 1} ---")
for s, e, ln, st in overlap:
    print(f"  status={st} bytes={s}-{e} wanted={e - s + 1} got={ln}")
if not overlap:
    print("  none — the player never asked for that byte region")

n_short = sum(1 for h in hits if short_by(h) > 0)
print("\n" + "=" * 62)
print("VERDICT")
print("=" * 62)
print(f"  played through segment 499 : {passed}")
print(f"  rendition height at the end: {final_h}")
print(f"  short responses seen by the browser: {n_short}")
if passed and n_short == 0 and final_h and final_h < 1080:
    print("  -> player survived by dropping rendition; 1080p still suspect")
elif passed and n_short == 0:
    print("  -> the player got complete data where we got 144 B: OUR METHOD")
elif n_short:
    print("  -> the browser was under-served too: SERVER-SIDE truncation")
else:
    print("  -> the player stalled at the same place: SERVER-SIDE truncation")
