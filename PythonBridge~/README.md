# Python bridge for `com.meshy.cache`

This folder ships inside the Unity package but is invisible to Unity (the
trailing `~` in the parent folder's name tells Unity to skip it). It contains
the local HTTP bridge the Meshy Browser editor window talks to.

## Why this exists

- BigQuery vector search needs Application Default Credentials and the BigQuery
  Python client — easier than wiring GCP auth into C#.
- Meshy's geometry is DRM-encrypted (`model.meshy`) and only their in-browser
  WASM viewer can decrypt it. A headless Chromium (Playwright) loads the public
  model page, intercepts the decrypted glTF buffer + texture PNGs via JS hooks
  (XHR + Blob + Worker + fetch), and saves them.
- `gltf-transform` runs the texture-resize + Draco compression pipeline.

## One-time setup

```powershell
# 1. Python deps
pip install -r requirements.txt

# 2. Chromium for Playwright
python -m playwright install chromium

# 3. gcloud (for BigQuery + Vertex embeddings)
gcloud auth application-default login

# 4. Node — for the gltf-transform CLI (used via npx)
#    https://nodejs.org/en/download   (16+ is fine)
node --version

# 5. Optional: confirm your BigQuery project is reachable
bq query --location=US --use_legacy_sql=false "SELECT 1"
```

Set your BigQuery project ID via either:

- env var: `set MESHY_GCP_PROJECT=<your-gcp-project>` (Windows) or
  `export MESHY_GCP_PROJECT=<your-gcp-project>` (POSIX)
- CLI flag: `python meshy_server.py --project <your-gcp-project>`

The bridge then expects the following objects in that project:

- dataset `meshy`
- model `meshy.text_embed` (Vertex text-embedding-005 remote model)
- table  `meshy.models_embedded` (id, embedding, plus your filter columns)
- TVF    `meshy.search(query_text, want_tag, max_tris, min_tris)` returning
  `id, result_id, public_url, prompt, triangle_count, tags, license,
   is_3d_printable, views, downloads, thumbnail_url, distance`

See the parent project's BigQuery RAG notebook for how to provision those.

## Running

```powershell
python meshy_server.py --port 8765
```

Leave this running while you use the Unity tool. It listens on `127.0.0.1`
only — never exposes BigQuery credentials externally.

## What each file does

| File | Purpose |
|---|---|
| `meshy_server.py` | HTTP bridge. `/health`, `/search`, `/import`. |
| `meshy_capture.py` | Playwright headless capture: GLB + texture PNGs from a live meshy.ai model page. |
| `glb_postprocess.py` | gltf-transform pipeline: `weld → resize → draco`. Texture extraction from the GLB's BIN chunk. |
| `glb_to_obj.py` | Optional OBJ export from a GLB (handles Draco + KHR_mesh_quantization). |
