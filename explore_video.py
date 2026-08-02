"""
Definitive answer: can all four qualities be retrieved, complete?
Key facts to establish:
  1. trailer length varies PER manifest -> sweep it, verify with the GCM tag
  2. what the manifest layer actually requires (Referer? session? expiry?)
  3. whether CDN segment URLs are protected at all
"""
from playwright.sync_api import sync_playwright
from Crypto.Cipher import AES
import subprocess
import requests
import base64
import json
import os
import re

url = "https://stream.biomaze.ir/evgaf5jqtgot/iframe"
referer = "https://stream.biomaze.ir"

INIT = r"""
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
    const ct=buf(data);
    const rec={ ctLen:ct&&ct.byteLength, keyB64:(km.get(key)||{}).b64 };
    window.__C.decrypts.push(rec);
    return oDec.apply(this,arguments).then(o=>{
        try{ rec.isM3u8 = new TextDecoder().decode(o).indexOf('#EXTM3U')!==-1; }catch(e){}
        return o;
    });
};
"""

with sync_playwright() as p:
    br = p.chromium.launch(headless=True)
    ctx = br.new_context(extra_http_headers={"Referer": referer})
    pg = ctx.new_page()
    pg.add_init_script(INIT)
    pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(3000)
    for sel in [".media", "video", "[class*='play']"]:
        try:
            pg.click(sel, timeout=3000); break
        except Exception:
            continue
    pg.wait_for_timeout(9000)
    cap = pg.evaluate("() => window.__C")

    KEY = next(base64.b64decode(d["keyB64"]) for d in cap["decrypts"] if d.get("isM3u8"))

    def decrypt(t):
        """Sweep the trailer length; confirm the boundary with the GCM tag."""
        for trailer in range(0, 80):
            end = len(t) - trailer
            if (end - 24) % 2:
                continue
            try:
                iv = bytes.fromhex(t[:24])
                blob = bytes.fromhex(t[24:end])
                out = AES.new(KEY, AES.MODE_GCM, nonce=iv).decrypt_and_verify(
                    blob[:-16], blob[-16:])
            except Exception:
                continue
            return out.decode("utf-8"), trailer
        return None, None

    master, mt = next((decrypt(r["full"])[0], decrypt(r["full"])[1]) for r in cap["raw"]
                      if decrypt(r["full"])[0] and "#EXT-X-STREAM-INF" in decrypt(r["full"])[0])

    print("=" * 74)
    print("SCHEME (GCM-tag verified, so the layout is exact)")
    print("=" * 74)
    print(f"  session key : {KEY.decode()}   AES-256-GCM")
    print(f"  wire format : hex(IV 12B) || hex(ct||tag16) || variable-length trailer")

    variants = []
    L = master.strip().splitlines()
    for i, ln in enumerate(L):
        if ln.startswith("#EXT-X-STREAM-INF"):
            nm = re.search(r'NAME="([^"]+)"', ln)
            rs = re.search(r"RESOLUTION=(\d+x\d+)", ln)
            variants.append({"q": nm.group(1) if nm else "?",
                             "res": rs.group(1) if rs else "?", "url": L[i + 1].strip()})

    fetched = pg.evaluate("""async (urls) => {
        const o = {};
        for (const u of urls) {
            try { const r = await fetch(u); o[u] = { status: r.status, text: await r.text() }; }
            catch (e) { o[u] = { status: -1, text: '' }; }
        }
        return o;
    }""", [v["url"] for v in variants])

    # same URL twice -> is the ciphertext deterministic?
    twice = pg.evaluate("""async (u) => {
        const a = await (await fetch(u)).text();
        const b = await (await fetch(u)).text();
        return { same: a === b, lenA: a.length, lenB: b.length };
    }""", variants[0]["url"])

    cookies = ctx.cookies()
    ctx.close(); br.close()

print("\n" + "=" * 74)
print("ALL FOUR QUALITIES")
print("=" * 74)
print(f"{'qual':>7} {'resolution':>11} {'segs':>6} {'duration':>9} {'size':>8} {'trail':>6}  end")
print("-" * 66)

out = []
for v in variants:
    t = fetched[v["url"]]["text"]
    m, trail = decrypt(t) if t else (None, None)
    if not m:
        print(f"{v['q']:>7} {v['res']:>11}  DECRYPT FAILED (raw {len(t)} bytes)")
        continue
    ext = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", m)]
    brs = re.findall(r"#EXT-X-BYTERANGE:(\d+)(?:@(\d+))?", m)
    sz = sum(int(a) for a, _ in brs)
    out.append({"quality": v["q"], "resolution": v["res"], "url": v["url"],
                "segments": len(ext), "duration": round(sum(ext), 1), "bytes": sz,
                "trailer": trail, "endlist": "#EXT-X-ENDLIST" in m,
                "br_no_offset": sum(1 for a, o in brs if not o), "m": m})
    print(f"{v['q']:>7} {v['res']:>11} {len(ext):>6} {sum(ext):>8.0f}s "
          f"{sz/1048576:>6.0f}MB {trail:>6}  {'Y' if out[-1]['endlist'] else 'N'}")

print("\nintegrity")
print(f"  retrieved              : {len(out)}/{len(variants)}")
print(f"  playlists distinct     : {len({r['m'] for r in out}) == len(out)}")
print(f"  durations              : {sorted({r['duration'] for r in out})}")
print(f"  segment counts         : {sorted({r['segments'] for r in out})}")
szs = [r["bytes"] for r in out]
print(f"  sizes descending       : {szs == sorted(szs, reverse=True)}  {[round(x/1048576) for x in szs]}")
print(f"  byteranges w/o offset  : {sum(r['br_no_offset'] for r in out)}")
print(f"  trailer lengths        : {[r['trailer'] for r in out]}  (varies => must sweep)")
print(f"  ciphertext stable      : {twice}")

# ---- what the manifest layer requires ----
bare = requests.Session()
u0 = variants[0]["url"]
n = len(bare.get(u0, timeout=20).content)
wr = len(bare.get(u0, headers={"Referer": referer, "Origin": referer}, timeout=20).content)
print("\nmanifest access control")
print(f"  no headers             : {n} bytes")
print(f"  Referer + Origin       : {wr} bytes")
print(f"  cookies set by site    : {[c['name'] for c in cookies] or 'none'}")

# ---- CDN layer: fully open? persistent? ----
print("\nCDN segment layer (zero headers) — decrypt + ffprobe real resolution")
for r in out:
    ku = re.search(r'URI="([^"]+)"', r["m"]).group(1)
    iv = bytes.fromhex(re.search(r"IV=0x([0-9a-f]+)", r["m"]).group(1))
    su = re.search(r"^(https://\S+)$", r["m"], re.M).group(1)
    ln, off = re.search(r"#EXT-X-BYTERANGE:(\d+)@(\d+)", r["m"]).groups()
    k = bare.get(ku, timeout=20).content
    dat = bare.get(su, headers={"Range": f"bytes={off}-{int(off)+int(ln)-1}"}, timeout=30).content
    ts = AES.new(k, AES.MODE_CBC, iv).decrypt(dat)
    pad = ts[-1]
    if 0 < pad <= 16:
        ts = ts[:-pad]
    fn = f"probe_{r['quality']}.ts"
    with open(fn, "wb") as fh:
        fh.write(ts)
    pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                         "stream=width,height", "-of", "csv=p=0", fn],
                        capture_output=True, text=True)
    got = pr.stdout.strip().splitlines()[0].replace(",", "x") if pr.stdout.strip() else "?"
    print(f"  {r['quality']:>6}: key={len(k)}B seg={len(dat)}B TS={'OK' if ts[0]==0x47 else 'BAD'} "
          f"probed={got:>9} vs declared={r['resolution']:>9} "
          f"{'MATCH' if got == r['resolution'] else 'MISMATCH'}")
    os.remove(fn)

# segment URL from a session recorded much earlier — still alive?
old_seg = ("https://biomaze-iii.c-d-n.io/js-0b438146b8f7dba2d3c8d1268b87b62e"
           "b02f246c32a05c8a62579cf3e899386d8d41fcefa7c0f8f50db50e51a0ac12a8"
           "-23e4a05243c16f75e14230c6e8c02aad")
rr = bare.get(old_seg, headers={"Range": "bytes=0-1023"}, timeout=20)
print(f"\n  stale segment URL from an earlier session: HTTP {rr.status_code} {len(rr.content)}B")

with open("variants.json", "w", encoding="utf-8") as f:
    json.dump({"key": KEY.decode(),
               "variants": [{k: v for k, v in r.items() if k != "m"} for r in out]}, f, indent=2)
print("\nSaved -> variants.json")
