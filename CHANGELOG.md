# Changelog

## [0.1.0] — initial release

### Added
- Meshy Browser editor window (Window ▸ Meshy ▸ Model Browser): prompt-based
  semantic search over a BigQuery RAG, thumbnail grid, per-result import.
- Per-import controls: format (glb / obj / both), texture size (512–4096),
  simplify quality (0.05–1.0).
- `MeshyMeshSimplifyPostprocessor`: auto-runs UnityMeshSimplifier on every
  GLB imported under `Assets/MeshyImports/`, with PreserveBorderEdges +
  PreserveUVSeamEdges + PreserveSurfaceCurvature. Reads per-import quality
  from `metadata.json`.
- `MeshyAssetPostprocessor`: detects PBR texture suffix (`_BaseMap`,
  `_Normal`, `_MetallicRoughness`, `_Occlusion`, `_Emission`) and sets
  TextureImporter type + sRGB flag accordingly. Adds BC7 / ASTC / DXT5
  platform overrides.
- `MeshyRenderProbe` editor tool: renders any prefab / GLB / mesh from 6
  angles + wireframe + UV-checker into PNGs, on a magenta background, for
  visual verification. Saves currently-open scene and restores it.
- Python bridge (`PythonBridge~/`): `meshy_server.py` HTTP server,
  `meshy_capture.py` headless-browser GLB+texture capture (XHR hook for
  Draco-compressed models), `glb_postprocess.py` gltf-transform pipeline,
  `glb_to_obj.py` optional OBJ exporter.

### Required dependencies
- `com.unity.cloud.gltfast` 6.19.0 (transitive)
- `com.unity.cloud.draco` 5.4.0 (transitive)
- `com.whinarn.unitymeshsimplifier` — install separately as a Git package
- Node 16+, Python 3.10+ for the bridge
