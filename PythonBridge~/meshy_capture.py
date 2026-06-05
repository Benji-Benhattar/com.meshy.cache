#!/usr/bin/env python3
"""Capture a Meshy community model's GLB + textures from its live public page.

Meshy ships geometry as an encrypted `model.meshy` blob that only its in-browser WASM
viewer can decrypt, and the textures as separate PNGs behind short-lived signed URLs.
The robust way to get both, with fresh URLs, is to load the public page in a headless
browser and intercept:
  * the decrypted glTF buffer (magic 'glTF') -> model.glb
  * every texture PNG the viewer fetches    -> texture_*.png

Public page URL is `https://www.meshy.ai/3d-models/<resultId>` (the slug is ignored).

Usage:
  python meshy_capture.py <resultId|url> --out DIR [--timeout 90] [--headed]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time

BASE = "https://www.meshy.ai/3d-models/"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Hook every place the decrypted glTF buffer can surface; forward matches to Python.
INIT_JS = r"""
(() => {
  const GLTF_MAGIC = 0x46546c67; // 'glTF' little-endian
  const seen = new Set();
  function isGLB(u8){ try { return new DataView(u8.buffer,u8.byteOffset,4).getUint32(0,true)===GLTF_MAGIC; } catch(e){ return false; } }
  function emit(buf){
    try {
      const u8 = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
      if (u8.byteLength < 12 || !isGLB(u8)) return;
      let bin=""; for (let i=0;i<u8.length;i++) bin+=String.fromCharCode(u8[i]);
      const b64=btoa(bin); const key=u8.byteLength+":"+b64.slice(0,24);
      if (seen.has(key)) return; seen.add(key);
      if (window.__glbCaptured) window.__glbCaptured(b64);
    } catch(e){}
  }
  function scan(d){ if(!d)return; if(d instanceof ArrayBuffer)emit(d); else if(ArrayBuffer.isView(d))emit(d.buffer); else if(d instanceof Blob)d.arrayBuffer().then(emit).catch(()=>{}); }
  const oc=URL.createObjectURL; URL.createObjectURL=function(o){ try{ if(o instanceof Blob)o.arrayBuffer().then(emit).catch(()=>{});}catch(e){} return oc.apply(this,arguments); };
  const OB=window.Blob; window.Blob=function(p,o){ const b=new OB(p,o); try{ b.arrayBuffer().then(emit).catch(()=>{});}catch(e){} return b; }; window.Blob.prototype=OB.prototype;
  const OW=window.Worker; window.Worker=function(u,o){ const w=new OW(u,o); w.addEventListener("message",e=>scan(e&&e.data)); const op=w.postMessage; w.postMessage=function(m){try{scan(m);}catch(e){} return op.apply(this,arguments);}; return w; }; window.Worker.prototype=OW.prototype;
  const of=window.fetch; window.fetch=function(){ return of.apply(this,arguments).then(r=>{ try{ const u=r.url||""; if(/\.meshy|\.glb|\/output\//.test(u)) r.clone().arrayBuffer().then(emit).catch(()=>{}); }catch(e){} return r; }); };
  // XHR (independent from fetch) — Meshy's viewer delivers the decrypted glTF through
  // XHR.response. Without this hook only the placeholder scene buffer surfaces.
  const OX=window.XMLHttpRequest; const opOpen=OX.prototype.open, opSend=OX.prototype.send;
  OX.prototype.open=function(){ this.__url=arguments[1]||""; return opOpen.apply(this,arguments); };
  OX.prototype.send=function(){
    const self=this;
    this.addEventListener("load", function(){
      try {
        const r=self.response;
        if (r instanceof ArrayBuffer) emit(r);
        else if (ArrayBuffer.isView(r)) emit(r.buffer);
        else if (r instanceof Blob) r.arrayBuffer().then(emit).catch(()=>{});
      } catch(e){}
    });
    return opSend.apply(this,arguments);
  };
})();
"""

# Map Meshy's texture filenames to the slot we care about (Unity-style names handled later).
TEX_PATTERNS = {
    "texture_0.png": "color",
    "texture_0_normal.png": "normal",
    "texture_0_metallic.png": "metallic",
    "texture_0_roughness.png": "roughness",
    "texture_0_emission.png": "emission",
    "texture_0_ao.png": "occlusion",
}


def resolve_url(s: str) -> str:
    if s.startswith("http"):
        return s
    return BASE + s


def capture(result_id_or_url: str, out_dir: str, timeout: int, headed: bool):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright missing. Install: pip install playwright && python -m playwright install chromium")

    url = resolve_url(result_id_or_url)
    m = UUID_RE.search(url)
    uid = m.group(0) if m else "model"
    target = os.path.join(out_dir, uid)
    os.makedirs(target, exist_ok=True)

    glbs: list[bytes] = []
    textures: dict[str, bytes] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        page = ctx.new_page()
        page.expose_function("__glbCaptured", lambda b64: glbs.append(base64.b64decode(b64)))
        page.add_init_script(INIT_JS)

        def on_response(resp):
            u = resp.url
            if "/output/" in u and u.split("?")[0].endswith(".png"):
                fname = os.path.basename(u.split("?")[0])
                if fname not in textures:
                    try:
                        textures[fname] = resp.body()
                    except Exception:
                        pass
        page.on("response", on_response)

        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        # Wait for a large (>1MB) glTF to appear and captures to settle.
        model_min, settle = 1_000_000, 6.0
        start = last_change = time.monotonic()
        last_n = 0
        while time.monotonic() - start < timeout:
            page.wait_for_timeout(500)
            n = len(glbs) + len(textures)
            if n != last_n:
                last_n = n
                last_change = time.monotonic()
            if any(len(b) >= model_min for b in glbs) and (time.monotonic() - last_change) >= settle:
                break
        browser.close()

    result = {"id": uid, "dir": target, "glb": None, "textures": {}}
    if glbs:
        glb = max(glbs, key=len)
        glb_path = os.path.join(target, "model.glb")
        with open(glb_path, "wb") as f:
            f.write(glb)
        result["glb"] = glb_path
    for fname, data in textures.items():
        p = os.path.join(target, fname)
        with open(p, "wb") as f:
            f.write(data)
        slot = TEX_PATTERNS.get(fname, "other")
        result["textures"][slot] = p
    return result


def main(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", help="resultId or full /3d-models/ URL")
    p.add_argument("--out", default="captures")
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--headed", action="store_true")
    args = p.parse_args(argv)

    res = capture(args.target, args.out, args.timeout, args.headed)
    if not res["glb"]:
        print(json.dumps({"ok": False, "error": "no glTF captured", **res}))
        return 1
    print(json.dumps({"ok": True, **res}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
