# Meshy Cache (`com.meshy.cache`)

Browse [Meshy.ai](https://www.meshy.ai) community 3D models from a BigQuery
vector-search RAG and import them into Unity with automatic mesh simplification
(via [UnityMeshSimplifier](https://github.com/Whinarn/UnityMeshSimplifier)),
texture compression, and correct per-suffix `TextureImporter` settings.

```
Prompt + max polys
        │
        ▼
 ┌──────────────┐    /search    ┌───────────────────────────┐
 │ Unity editor │ ─────────────▶│  Python bridge (localhost) │  BigQuery VECTOR_SEARCH
 │   window     │ ◀─────────────│   + headless GLB capture   │  → top-N results + thumbnails
 └──────────────┘    results    └───────────────────────────┘
        │ Import(result_id)            │ /import
        ▼                              ▼
  Assets/MeshyImports/<name>/   Playwright headless Chromium loads the live
   <name>.glb                   meshy.ai page; intercepts the decrypted glTF
   <name>_simplified.prefab     buffer via XHR hook + texture PNGs from /output/
   <name>_simplified_meshes     gltf-transform: weld → resize → draco
   <name>_BaseMap.png           UnityMeshSimplifier: quality-driven simplify
   <name>_Normal.png            AssetPostprocessor: correct TextureImporter per slot
   <name>_MetallicRoughness.png
   metadata.json
```

## Install

### 1. Install UnityMeshSimplifier first (Unity Package Manager has no transitive Git deps)

In your Unity project, open **Window ▸ Package Manager**, click the `+` button,
choose **Add package from Git URL**, paste:

```
https://github.com/Whinarn/UnityMeshSimplifier.git
```

### 2. Install this package

Same dialog, paste:

```
https://github.com/<your-fork>/com.meshy.cache.git
```

— OR for local testing, drop this folder into `<project>/Packages/com.meshy.cache/`.

Unity will pull `com.unity.cloud.gltfast` (6.19+) and `com.unity.cloud.draco`
(5.4+) automatically as transitive deps.

### 3. Set up the Python bridge

The bridge runs locally on `127.0.0.1:8765`. It needs ~5 Python deps,
Node 16+ (for `gltf-transform` via npx), and gcloud Application Default
Credentials.

```powershell
# Get the Python files out of the package (the ~ folder is invisible to Unity)
cd <project>\Packages\com.meshy.cache\PythonBridge~

pip install -r requirements.txt
python -m playwright install chromium
gcloud auth application-default login
# Edit meshy_server.py if your BigQuery project is not your-gcp-project

python meshy_server.py --port 8765
```

Leave the bridge running while you use the Unity tool.

## Use

In Unity: **Window ▸ Meshy ▸ Model Browser**

- Type a prompt (`fierce armored dragon`, `low poly sword`, …)
- Set **max polygons** and **min polygons** filters for the BigQuery search
- **Simplify quality** (0.05–1.0) controls the UnityMeshSimplifier post-import step
- **Texture size** controls the JPEG re-bake size baked into the GLB

Hit **Search**, then **Import** on any result. Files land under
`Assets/MeshyImports/<name>/`. The simplified prefab `<name>_simplified.prefab`
is the one you drop into scenes.

### Render Probe — quick visual diagnostic

**Window ▸ Meshy ▸ Render Selected** with any prefab / GLB / Mesh asset
selected. Renders front / three-quarter / side / back / left / top + a
wireframe pass + a UV-checker pass into
`<project>/PythonBridge/render_screens/` (persistent — Unity won't wipe it
like `Temp/`). Useful for verifying simplification didn't break the silhouette
and the UV layout is sane.

The probe also takes file sentinels for scripted use:

```
echo '{ "asset_path": "Assets/Foo/Bar.prefab" }' > Temp/render_request.json
# wait briefly; reports at Temp/render_done.json
```

## Architecture

| Layer | Files |
|---|---|
| **C# Editor** (this package) | `Editor/MeshyBrowser.cs` (the window), `MeshyMeshSimplifyPostprocessor.cs` (auto-simplify on GLB import), `MeshyAssetPostprocessor.cs` (sRGB + normal-map detection), `MeshyRenderProbe.cs` (3-view diagnostic) |
| **Python bridge** (`PythonBridge~/`) | `meshy_server.py` HTTP, `meshy_capture.py` headless-browser GLB+texture capture, `glb_postprocess.py` gltf-transform pipeline, `glb_to_obj.py` optional OBJ export |
| **External** | UnityMeshSimplifier (Git package), glTFast + Draco (Unity packages), Node + `gltf-transform` (npx), BigQuery RAG + Vertex embeddings |

## Customising

- **Default simplify quality** when no metadata is present: edit
  `Editor/MeshyMeshSimplifyPostprocessor.cs` →
  `const float DefaultQuality = 0.50f`.
- **Texture compression formats** per platform: edit
  `Editor/MeshyAssetPostprocessor.cs` (`ApplyPlatformOverride` calls).
- **BigQuery project / dataset**: edit
  `PythonBridge~/meshy_server.py` → `PROJECT = "your-gcp-project"`. The RAG
  setup itself is documented in the parent project's README.

## License

MIT.
