// MeshyRenderProbe.cs — render a prefab/GLB/asset from three angles into PNGs that an
// agent (or human) can inspect, without disturbing the user's currently-open scene.
//
// Two ways to drive it:
//   1. Menu:    Window > Meshy > Render Selected   (uses the highlighted Project asset)
//   2. Sentinel: write JSON to <project>/Temp/render_request.json:
//          { "asset_path": "Assets/MeshyImports/sword/sword.glb", "basename": "sword" }
//      The script auto-detects the file, runs the render, and writes the file list to
//      <project>/Temp/render_done.json. PNGs land in <project>/Temp/render_screens/.
//
// Flow per request:
//   1. Save reference to currently-open scene path.
//   2. Open an empty single scene.
//   3. Instantiate the asset; if it's a multi-renderer hierarchy, union all Renderer.bounds
//      to find the bounding sphere; recenter the object at origin.
//   4. Spawn a directional light + raise ambient; spawn a camera with a solid neutral-grey
//      background so the model silhouette is unambiguous.
//   5. Compute camera distance from `radius / sin(fov/2)` so the whole sphere fits in view,
//      then shoot 3 angles (front, three-quarter, side).
//   6. Encode each render-texture frame to PNG and write to Temp/render_screens.
//   7. Tear down spawned objects and reopen the original scene.

using System;
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace MeshyTool
{
    [InitializeOnLoad]
    public static class MeshyRenderProbe
    {
        static readonly string Root        = Directory.GetCurrentDirectory();
        // Sentinel + done live in Temp/ (short-lived; fine if wiped). OUTPUT lives in
        // PythonBridge/ at the project root — outside Unity's Temp/Library control, so
        // the screenshots survive Unity restarts and the user can inspect them later.
        static readonly string Request     = Path.Combine(Root, "Temp", "render_request.json");
        static readonly string Done        = Path.Combine(Root, "Temp", "render_done.json");
        static readonly string Heartbeat   = Path.Combine(Root, "Temp", "render_heartbeat.txt");
        static readonly string OutDir      = Path.Combine(Root, "PythonBridge", "render_screens");

        const int   ImgW   = 1024;
        const int   ImgH   = 1024;
        const float FovDeg = 35f;
        // Pure magenta — Unity's universal "missing texture / missing mesh" sentinel colour,
        // so any hole in the silhouette pops out instantly against the rest of the model.
        static readonly Color BgColor = new Color(1.0f, 0.0f, 1.0f, 1f);

        // (label, direction-the-camera-faces-from)
        // Orbit views + top so holes/missing caps show up on every face of the bounding box.
        static readonly (string name, Vector3 dirFromOrigin)[] Views =
        {
            ("01_front",         new Vector3( 0.0f,  0.10f, -1.0f)),
            ("02_three_quarter", new Vector3( 1.0f,  0.45f, -1.0f)),
            ("03_side_right",    new Vector3( 1.0f,  0.10f,  0.0f)),
            ("04_back",          new Vector3( 0.0f,  0.10f,  1.0f)),
            ("05_side_left",     new Vector3(-1.0f,  0.10f,  0.0f)),
            ("06_top",           new Vector3( 0.001f, 1.0f, 0.001f)),
        };

        static MeshyRenderProbe()
        {
            try { Directory.CreateDirectory(OutDir); } catch {}
            EditorApplication.update += Tick;
        }

        // ---- driver --------------------------------------------------------------------

        static void Tick()
        {
            try { File.WriteAllText(Heartbeat, DateTime.Now.ToString("O")); } catch {}
            if (!File.Exists(Request)) return;
            string json;
            try { json = File.ReadAllText(Request); File.Delete(Request); }
            catch { return; }
            Req req = null;
            try { req = JsonUtility.FromJson<Req>(json); } catch {}
            if (req == null || string.IsNullOrEmpty(req.asset_path))
            { WriteDone(false, "request missing asset_path", null); return; }
            Run(req.asset_path, string.IsNullOrEmpty(req.basename) ? Path.GetFileNameWithoutExtension(req.asset_path) : req.basename);
        }

        [MenuItem("Window/Meshy/Render Selected")]
        static void RenderSelected()
        {
            var obj = Selection.activeObject;
            if (obj == null) { Debug.LogWarning("MeshyRenderProbe: nothing selected."); return; }
            var path = AssetDatabase.GetAssetPath(obj);
            if (string.IsNullOrEmpty(path)) { Debug.LogWarning("MeshyRenderProbe: selection isn't an asset."); return; }
            Run(path, Path.GetFileNameWithoutExtension(path));
        }

        // ---- main render -------------------------------------------------------------

        static void Run(string assetPath, string basename)
        {
            // Save reference to whatever scene the user has open.
            string prevScenePath = SceneManager.GetActiveScene().path;
            try
            {
                // If the user has unsaved changes, ask Unity to dump them to disk first.
                if (!string.IsNullOrEmpty(prevScenePath))
                    EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo();
            }
            catch {}

            var newScene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);

            GameObject inst = null, lightGO = null, camGO = null;
            RenderTexture rt = null;
            var files = new List<string>();
            string err = null;

            try
            {
                var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(assetPath);
                if (prefab == null)
                {
                    err = $"asset not found at {assetPath}";
                    return;
                }

                inst = (PrefabUtility.InstantiatePrefab(prefab) as GameObject) ?? UnityEngine.Object.Instantiate(prefab);
                inst.transform.position = Vector3.zero;
                inst.transform.rotation = Quaternion.identity;
                inst.transform.localScale = Vector3.one;

                Bounds bounds;
                if (!TryGetBounds(inst, out bounds))
                {
                    err = "asset has no renderable bounds";
                    return;
                }

                // Recenter so framing math is symmetric.
                inst.transform.position -= bounds.center;
                bounds.center = Vector3.zero;

                // Lighting that won't blow out highlights but still shows geometry.
                lightGO = new GameObject("ProbeLight");
                var light = lightGO.AddComponent<Light>();
                light.type = LightType.Directional;
                light.intensity = 1.1f;
                light.shadows = LightShadows.None;        // shadows interfere with reading the silhouette
                lightGO.transform.rotation = Quaternion.Euler(50f, -30f, 0f);
                RenderSettings.ambientMode = UnityEngine.Rendering.AmbientMode.Flat;
                RenderSettings.ambientLight = new Color(0.30f, 0.30f, 0.30f, 1f);

                camGO = new GameObject("ProbeCamera");
                var cam = camGO.AddComponent<Camera>();
                cam.clearFlags = CameraClearFlags.SolidColor;
                cam.backgroundColor = BgColor;
                cam.fieldOfView = FovDeg;
                cam.nearClipPlane = 0.01f;
                cam.farClipPlane = 1000f;
                cam.allowHDR = false;
                cam.allowMSAA = true;

                // Bounding-sphere radius around origin.
                float radius = bounds.extents.magnitude;
                float halfFovRad = FovDeg * 0.5f * Mathf.Deg2Rad;
                float dist = (radius / Mathf.Sin(halfFovRad)) * 1.15f;  // 15% padding

                rt = new RenderTexture(ImgW, ImgH, 24, RenderTextureFormat.ARGB32) { antiAliasing = 4 };
                rt.Create();
                cam.targetTexture = rt;

                foreach (var v in Views)
                {
                    Vector3 dir = v.dirFromOrigin.normalized;
                    camGO.transform.position = dir * dist;
                    camGO.transform.LookAt(Vector3.zero, Vector3.up);

                    cam.Render();

                    var prevActive = RenderTexture.active;
                    RenderTexture.active = rt;
                    var tex = new Texture2D(ImgW, ImgH, TextureFormat.RGB24, false);
                    tex.ReadPixels(new Rect(0, 0, ImgW, ImgH), 0, 0);
                    tex.Apply();
                    RenderTexture.active = prevActive;

                    byte[] png = tex.EncodeToPNG();
                    string outPath = Path.Combine(OutDir, $"{basename}_{v.name}.png");
                    File.WriteAllBytes(outPath, png);
                    UnityEngine.Object.DestroyImmediate(tex);
                    files.Add(outPath);
                }

                // Extra diagnostic passes from the three-quarter angle: wireframe (shows
                // topology + holes) and UV-checker (shows stretched/seam UVs).
                Vector3 diag = Views[1].dirFromOrigin.normalized;
                camGO.transform.position = diag * dist;
                camGO.transform.LookAt(Vector3.zero, Vector3.up);

                // --- WIREFRAME pass ---
                string wirePath = Path.Combine(OutDir, $"{basename}_07_wireframe.png");
                RenderPass(cam, rt, wirePath, useWireframe: true);
                files.Add(wirePath);

                // --- UV CHECKER pass ---
                var checkerTex = CreateUVChecker(512);
                var checkerMat = new Material(Shader.Find("Unlit/Texture")) { mainTexture = checkerTex };
                var savedMats = SwapMaterials(inst, checkerMat);
                string uvPath = Path.Combine(OutDir, $"{basename}_08_uv_checker.png");
                RenderPass(cam, rt, uvPath, useWireframe: false);
                RestoreMaterials(inst, savedMats);
                UnityEngine.Object.DestroyImmediate(checkerMat);
                UnityEngine.Object.DestroyImmediate(checkerTex);
                files.Add(uvPath);
            }
            catch (Exception e) { err = e.ToString(); }
            finally
            {
                if (rt != null)        { rt.Release(); UnityEngine.Object.DestroyImmediate(rt); }
                if (inst != null)      UnityEngine.Object.DestroyImmediate(inst);
                if (lightGO != null)   UnityEngine.Object.DestroyImmediate(lightGO);
                if (camGO != null)     UnityEngine.Object.DestroyImmediate(camGO);

                // Restore whatever scene was open. If there was no saved scene, just
                // leave the empty one Unity gave us.
                if (!string.IsNullOrEmpty(prevScenePath))
                {
                    try { EditorSceneManager.OpenScene(prevScenePath, OpenSceneMode.Single); }
                    catch (Exception e) { Debug.LogWarning($"MeshyRenderProbe: could not reopen {prevScenePath}: {e.Message}"); }
                }

                WriteDone(err == null, err, files.ToArray());
            }
        }

        // ---- helpers ----------------------------------------------------------------

        static void RenderPass(Camera cam, RenderTexture rt, string outPath, bool useWireframe)
        {
            if (useWireframe) GL.wireframe = true;
            try { cam.Render(); }
            finally { if (useWireframe) GL.wireframe = false; }
            var prev = RenderTexture.active;
            RenderTexture.active = rt;
            var t = new Texture2D(ImgW, ImgH, TextureFormat.RGB24, false);
            t.ReadPixels(new Rect(0, 0, ImgW, ImgH), 0, 0);
            t.Apply();
            RenderTexture.active = prev;
            File.WriteAllBytes(outPath, t.EncodeToPNG());
            UnityEngine.Object.DestroyImmediate(t);
        }

        // High-contrast checker: red/green tiles with blue gridlines every 4 cells.
        // Distorted UVs warp the tiles obviously; seams break the gridlines.
        static Texture2D CreateUVChecker(int size = 512, int tiles = 16)
        {
            var tex = new Texture2D(size, size, TextureFormat.RGB24, false);
            tex.filterMode = FilterMode.Point;
            tex.wrapMode = TextureWrapMode.Repeat;
            int tile = Mathf.Max(1, size / tiles);
            var cols = new Color[size * size];
            for (int y = 0; y < size; y++)
            for (int x = 0; x < size; x++)
            {
                int tx = x / tile, ty = y / tile;
                bool dark = ((tx + ty) & 1) == 0;
                Color c = dark ? new Color(0.85f, 0.20f, 0.20f) : new Color(0.20f, 0.85f, 0.20f);
                // Blue guide lines every 4 cells.
                bool onGuide = (tx % 4 == 0 && (x % tile) == 0) || (ty % 4 == 0 && (y % tile) == 0);
                if (onGuide) c = new Color(0.15f, 0.25f, 0.95f);
                cols[y * size + x] = c;
            }
            tex.SetPixels(cols);
            tex.Apply();
            return tex;
        }

        static Dictionary<Renderer, Material[]> SwapMaterials(GameObject root, Material with)
        {
            var saved = new Dictionary<Renderer, Material[]>();
            foreach (var r in root.GetComponentsInChildren<Renderer>(true))
            {
                saved[r] = r.sharedMaterials;
                var arr = new Material[r.sharedMaterials.Length];
                for (int i = 0; i < arr.Length; i++) arr[i] = with;
                r.sharedMaterials = arr;
            }
            return saved;
        }

        static void RestoreMaterials(GameObject root, Dictionary<Renderer, Material[]> saved)
        {
            foreach (var kv in saved) kv.Key.sharedMaterials = kv.Value;
        }

        static bool TryGetBounds(GameObject go, out Bounds bounds)
        {
            var renderers = go.GetComponentsInChildren<Renderer>(true);
            bounds = default;
            bool first = true;
            foreach (var r in renderers)
            {
                if (!r.enabled) continue;
                if (first) { bounds = r.bounds; first = false; }
                else bounds.Encapsulate(r.bounds);
            }
            return !first;
        }

        static void WriteDone(bool ok, string err, string[] files)
        {
            var sb = new System.Text.StringBuilder();
            sb.Append("{\"ok\":").Append(ok ? "true" : "false");
            if (!string.IsNullOrEmpty(err))
                sb.Append(",\"error\":\"").Append(err.Replace("\\", "\\\\").Replace("\"", "\\\"")).Append("\"");
            sb.Append(",\"files\":[");
            if (files != null)
            {
                for (int i = 0; i < files.Length; i++)
                {
                    if (i > 0) sb.Append(",");
                    sb.Append("\"").Append(files[i].Replace("\\", "/")).Append("\"");
                }
            }
            sb.Append("]}");
            try { File.WriteAllText(Done, sb.ToString()); }
            catch (Exception e) { Debug.LogError("MeshyRenderProbe done-write failed: " + e); }
        }

        [Serializable] class Req { public string asset_path; public string basename; }
    }
}
