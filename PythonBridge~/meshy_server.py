#!/usr/bin/env python3
"""Local bridge server between the Unity editor tool and the Meshy BigQuery RAG.

Endpoints (all GET, JSON responses):

  /search?q=<prompt>&max_tris=<int>&min_tris=<int>&tag=<str>&printable=1
          &min_downloads=<int>&limit=<int>
      Runs the `meshy.search` table function in BigQuery and returns the top matches
      with thumbnail + metadata + the public_url used for import.

  /import?result_id=<uuid>&name=<slug>&dest=<abs Assets path>&format=glb|obj|both
      Captures the model's GLB + textures from its live public page, converts to OBJ
      if asked, renames textures to Unity conventions, and writes everything into
      <dest>/<name>/. Returns the folder and the files written.

  /health   -> {"ok": true}

Run:  python meshy_server.py --project <your-gcp-project> [--port 8765]
      (or:  set MESHY_GCP_PROJECT=<your-gcp-project> ; python meshy_server.py)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# Captures go to system temp, NOT next to this script — otherwise binary buffers
# land inside Unity's Assets/ and trigger constant re-imports.
CAPTURE_DIR = os.path.join(tempfile.gettempdir(), "meshy_captures")
# GCP project that hosts the BigQuery RAG (dataset `meshy`, model `meshy.text_embed`,
# TVF `meshy.search`, table `meshy.models_embedded`). Override with the MESHY_GCP_PROJECT
# env var or `--project <id>` at launch.
PROJECT = os.environ.get("MESHY_GCP_PROJECT", "your-gcp-project-id")

# Meshy texture filename -> Unity-style suffix (URP Lit conventions)
UNITY_TEX = {
    "texture_0.png": "_BaseMap.png",
    "texture_0_normal.png": "_Normal.png",
    "texture_0_metallic.png": "_Metallic.png",
    "texture_0_roughness.png": "_Roughness.png",
    "texture_0_emission.png": "_Emission.png",
    "texture_0_ao.png": "_Occlusion.png",
}


def slugify(s, maxlen=48):
    s = re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip()).strip("_")
    return (s or "MeshyModel")[:maxlen]


# --------------------------------------------------------------------------- search

def run_search(client, q, tag, max_tris, min_tris, printable, min_downloads, limit):
    from google.cloud import bigquery
    extra = []
    if printable:
        extra.append("is_3d_printable")
    if min_downloads:
        extra.append("downloads >= @min_downloads")
    where = ("WHERE " + " AND ".join(extra)) if extra else ""
    sql = f"""
        SELECT result_id, public_url, prompt, triangle_count, tags, license,
               is_3d_printable, views, downloads, thumbnail_url, distance
        FROM `{PROJECT}.meshy.search`(@q, @tag, @max_tris, @min_tris)
        {where}
        ORDER BY distance
        LIMIT @limit
    """
    params = [
        bigquery.ScalarQueryParameter("q", "STRING", q),
        bigquery.ScalarQueryParameter("tag", "STRING", tag),
        bigquery.ScalarQueryParameter("max_tris", "INT64", max_tris),
        bigquery.ScalarQueryParameter("min_tris", "INT64", min_tris),
        bigquery.ScalarQueryParameter("min_downloads", "INT64", min_downloads),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    out = []
    for r in job.result():
        out.append({
            "result_id": r.result_id,
            "public_url": r.public_url,
            "prompt": r.prompt,
            "triangle_count": r.triangle_count,
            "tags": list(r.tags) if r.tags else [],
            "license": r.license,
            "is_3d_printable": r.is_3d_printable,
            "views": r.views,
            "downloads": r.downloads,
            "thumbnail_url": r.thumbnail_url,
            "distance": r.distance,
        })
    return out


# --------------------------------------------------------------------------- import

def run_import(result_id, name, dest, fmt, timeout=120, target_tris=15000,
               texture_max_size=1024, simplify_quality=0.5):
    """Capture + decimate + extract clean textures, then write into <dest>/<name>/.

    Decimation + PBR-texture extraction are done by glb_postprocess.py against the captured
    GLB. We do NOT use the /output/*.png Meshy serves separately — those secondary maps
    arrive as ~200-byte placeholders. The real textures live inside the GLB.
    """
    name = slugify(name or result_id)
    workdir = CAPTURE_DIR
    os.makedirs(workdir, exist_ok=True)
    cap = subprocess.run(
        [sys.executable, os.path.join(HERE, "meshy_capture.py"), result_id,
         "--out", workdir, "--timeout", str(timeout)],
        capture_output=True, text=True)
    try:
        info = json.loads(cap.stdout.strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "error": "capture failed", "stderr": cap.stderr[-800:]}
    if not info.get("ok") or not info.get("glb"):
        return {"ok": False, "error": info.get("error", "no glb captured"),
                "stderr": cap.stderr[-800:]}

    out_dir = os.path.join(dest, name)
    os.makedirs(out_dir, exist_ok=True)
    raw_glb = info["glb"]

    # Decimate + extract textures from the GLB itself (replaces the placeholder /output PNGs)
    pp_args = [sys.executable, os.path.join(HERE, "glb_postprocess.py"), raw_glb,
               "--out-dir", out_dir, "--basename", name,
               "--target-tris", str(target_tris),
               "--texture-max-size", str(texture_max_size)]
    pp = subprocess.run(pp_args, capture_output=True, text=True)
    if pp.returncode != 0:
        return {"ok": False, "error": "postprocess failed", "stderr": pp.stderr[-800:]}
    try:
        pp_info = json.loads(pp.stdout.strip())
    except Exception:
        pp_info = {}

    written = []
    decim_glb = os.path.join(out_dir, name + ".glb")
    if fmt in ("glb", "both"):
        written.append(decim_glb)
    elif os.path.exists(decim_glb):
        # OBJ-only requested: keep the GLB as input for the converter, then drop it.
        pass

    # Pick up the PNGs glb_postprocess wrote (paths come back in pp_info["textures"]).
    basecolor = None
    for suffix, tex_path in (pp_info.get("textures") or {}).items():
        if os.path.exists(tex_path):
            written.append(tex_path)
            if suffix == "_BaseMap":
                basecolor = os.path.basename(tex_path)

    if fmt in ("obj", "both"):
        obj_path = os.path.join(out_dir, name + ".obj")
        conv = subprocess.run(
            [sys.executable, os.path.join(HERE, "glb_to_obj.py"), decim_glb, "--out", obj_path]
            + (["--basecolor", basecolor] if basecolor else []),
            capture_output=True, text=True)
        if conv.returncode == 0:
            written.append(obj_path)
            mtl = os.path.splitext(obj_path)[0] + ".mtl"
            if os.path.exists(mtl):
                written.append(mtl)
        else:
            return {"ok": False, "error": "obj conversion failed", "stderr": conv.stderr[-800:]}

    # Caller chose obj-only? Remove the GLB we used as the converter's input.
    if fmt == "obj" and os.path.exists(decim_glb):
        try: os.remove(decim_glb)
        except OSError: pass

    meta = {"result_id": result_id, "name": name,
            "public_url": f"https://www.meshy.ai/3d-models/{result_id}",
            "decimate": pp_info.get("decimate"),
            "simplify_quality": float(simplify_quality),  # read by MeshyMeshSimplifyPostprocessor
            "files": [os.path.basename(p) for p in written]}
    with open(os.path.join(out_dir, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    written.append(os.path.join(out_dir, "metadata.json"))

    return {"ok": True, "folder": out_dir,
            "files": [os.path.basename(p) for p in written],
            "decimate": pp_info.get("decimate")}


# ----------------------------------------------------------------------------- HTTP

def make_handler(client):
    class H(BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # quieter console
            sys.stderr.write("  " + (a[0] % a[1:]) + "\n")

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            g = lambda k, d=None: (qs.get(k, [d])[0])
            gi = lambda k: (int(qs[k][0]) if k in qs and qs[k][0] not in ("", "null") else None)
            try:
                if u.path == "/health":
                    return self._send({"ok": True})
                if u.path == "/search":
                    res = run_search(client, g("q", ""), g("tag") or None,
                                     gi("max_tris"), gi("min_tris"),
                                     g("printable") in ("1", "true"), gi("min_downloads"),
                                     gi("limit") or 10)
                    return self._send({"ok": True, "results": res})
                if u.path == "/import":
                    sq = g("simplify_quality")
                    try: sq = float(sq) if sq else 0.5
                    except: sq = 0.5
                    res = run_import(g("result_id"), g("name"), g("dest"),
                                     g("format", "both"),
                                     timeout=gi("timeout") or 120,
                                     target_tris=gi("target_tris") or 15000,
                                     texture_max_size=gi("texture_max_size") or 1024,
                                     simplify_quality=sq)
                    return self._send(res, 200 if res.get("ok") else 500)
                self._send({"ok": False, "error": "unknown endpoint"}, 404)
            except Exception as e:  # noqa: BLE001
                self._send({"ok": False, "error": str(e)}, 500)
    return H


def main(argv):
    global PROJECT
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=PROJECT)
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args(argv)
    PROJECT = args.project

    from google.cloud import bigquery
    client = bigquery.Client(project=PROJECT, location="US")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(client))
    print(f"Meshy bridge on http://127.0.0.1:{args.port}  (project {PROJECT})")
    print("  /search?q=...&max_tris=...   /import?result_id=...&dest=...&name=...")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
