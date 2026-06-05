// MeshyMeshSimplifyPostprocessor.cs — runs UnityMeshSimplifier on every Meshy-imported
// GLB and emits a sibling "<name>_simplified.prefab" backed by simplified Mesh assets.
//
// Why on the Unity side: UnityMeshSimplifier reads the Mesh's vertex attributes (UVs,
// normals, tangents, colors) directly and has explicit "preserve UV seams" + "preserve
// borders" knobs. meshoptimizer's simplify (what we were using via gltf-transform) only
// has a binary lock-border flag and treats every per-triangle UV island as a border on
// Meshy meshes — which is why it produced slivers + holes earlier.
//
// Behaviour:
//   - Triggers on import of any `.glb` directly under Assets/MeshyImports/<name>/.
//   - Skips its own outputs (anything ending in _simplified.glb / _simplified.prefab).
//   - Uses EditorApplication.delayCall so glTFast's ScriptedImporter has finished
//     creating Mesh sub-assets before we read them.
//   - For each Mesh in the source GLB, runs MeshSimplifier with quality = TargetQuality
//     (configurable below), preserveUVSeamEdges = true, preserveBorderEdges = true.
//   - Writes <folder>/<name>_simplified_meshes.asset (all meshes in one .asset file).
//   - Writes <folder>/<name>_simplified.prefab — a clone of the source prefab with each
//     MeshFilter rebound to its simplified mesh; renderers + materials kept intact.

using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEngine;
using UnityMeshSimplifier;

namespace MeshyTool
{
    public class MeshyMeshSimplifyPostprocessor : AssetPostprocessor
    {
        // Default quality if the per-import metadata.json doesn't carry one.
        // Each Meshy Browser import overrides this via metadata.json:simplify_quality.
        const float DefaultQuality = 0.50f;

        // Quality threshold below which we give up trying to hit the target — the
        // simplifier will stop early rather than wreck topology.
        const float AggressivenessGuard = 7.0f;

        const string PrefixDir = "Assets/MeshyImports/";

        static void OnPostprocessAllAssets(string[] imported, string[] _, string[] __, string[] ___)
        {
            // Queue work for AFTER the current import wave finishes — glTFast finishes
            // setting up Mesh sub-assets during the import callback, and our
            // AssetDatabase.LoadAllAssetsAtPath needs them resolved.
            var pending = new List<string>();
            foreach (var path in imported)
            {
                if (!path.StartsWith(PrefixDir)) continue;
                if (!path.EndsWith(".glb")) continue;
                if (path.Contains("_simplified")) continue;
                pending.Add(path);
            }
            if (pending.Count == 0) return;

            EditorApplication.delayCall += () =>
            {
                foreach (var p in pending)
                {
                    try { ProcessGLB(p); }
                    catch (System.Exception e) { Debug.LogError($"MeshyMeshSimplify: failed on {p}: {e}"); }
                }
                AssetDatabase.SaveAssets();
            };
        }

        static void ProcessGLB(string glbPath)
        {
            string folder = Path.GetDirectoryName(glbPath).Replace("\\", "/");
            string baseName = Path.GetFileNameWithoutExtension(glbPath);
            string meshAssetPath  = $"{folder}/{baseName}_simplified_meshes.asset";
            string prefabAssetPath = $"{folder}/{baseName}_simplified.prefab";
            float quality = ReadQualityFromMetadata(folder, DefaultQuality);

            // If we already wrote a simplified prefab newer than the GLB, skip.
            if (File.Exists(prefabAssetPath) &&
                File.GetLastWriteTimeUtc(prefabAssetPath) > File.GetLastWriteTimeUtc(glbPath))
                return;

            var sourcePrefab = AssetDatabase.LoadAssetAtPath<GameObject>(glbPath);
            if (sourcePrefab == null) { Debug.LogWarning($"MeshyMeshSimplify: no prefab at {glbPath}"); return; }

            // Instantiate a working copy so we can rebind its MeshFilters.
            var instance = (GameObject)PrefabUtility.InstantiatePrefab(sourcePrefab);
            try
            {
                // Simplify every unique source Mesh exactly once.
                var simplifiedFor = new Dictionary<Mesh, Mesh>();
                int totalBefore = 0, totalAfter = 0;
                foreach (var mf in instance.GetComponentsInChildren<MeshFilter>(true))
                {
                    var src = mf.sharedMesh;
                    if (src == null) continue;
                    if (simplifiedFor.ContainsKey(src)) { mf.sharedMesh = simplifiedFor[src]; continue; }

                    var simplifier = new MeshSimplifier();
                    simplifier.PreserveBorderEdges = true;
                    simplifier.PreserveUVSeamEdges = true;
                    simplifier.PreserveUVFoldoverEdges = false;
                    simplifier.PreserveSurfaceCurvature = true;
                    simplifier.Initialize(src);
                    simplifier.SimplifyMesh(quality);
                    var simplified = simplifier.ToMesh();
                    simplified.name = src.name + "_simplified";
                    simplified.RecalculateBounds();
                    simplified.RecalculateTangents();   // UnityMeshSimplifier doesn't always carry tangents

                    totalBefore += src.triangles.Length / 3;
                    totalAfter  += simplified.triangles.Length / 3;
                    simplifiedFor[src] = simplified;
                    mf.sharedMesh = simplified;
                }

                if (simplifiedFor.Count == 0)
                {
                    Debug.LogWarning($"MeshyMeshSimplify: no MeshFilters under {glbPath}");
                    return;
                }

                // Persist all simplified meshes into ONE .asset file (so the prefab can reference them).
                // Replace any existing file rather than appending.
                if (File.Exists(meshAssetPath)) AssetDatabase.DeleteAsset(meshAssetPath);
                var meshes = new List<Mesh>(simplifiedFor.Values);
                AssetDatabase.CreateAsset(meshes[0], meshAssetPath);
                for (int i = 1; i < meshes.Count; i++)
                    AssetDatabase.AddObjectToAsset(meshes[i], meshAssetPath);
                AssetDatabase.ImportAsset(meshAssetPath);

                // Save the rebound instance as a new prefab (NOT a variant — we want a flat prefab
                // because a variant would still reference the source GLB's original meshes).
                instance.transform.position = Vector3.zero;
                if (File.Exists(prefabAssetPath)) AssetDatabase.DeleteAsset(prefabAssetPath);
                PrefabUtility.SaveAsPrefabAsset(instance, prefabAssetPath);

                Debug.Log($"MeshyMeshSimplify: {glbPath}  {totalBefore:n0} -> {totalAfter:n0} tris  " +
                          $"(quality={quality:F2}, {simplifiedFor.Count} mesh(es))  ->  {prefabAssetPath}");
            }
            finally
            {
                Object.DestroyImmediate(instance);
            }
        }

        // Look up the per-import simplify quality the bridge wrote into metadata.json.
        // No JsonUtility because the field may be missing on older imports — pull it
        // out with a tiny regex so we don't need a strict schema.
        static float ReadQualityFromMetadata(string folder, float fallback)
        {
            string metaPath = $"{folder}/metadata.json";
            if (!File.Exists(metaPath)) return fallback;
            try
            {
                string txt = File.ReadAllText(metaPath);
                var m = System.Text.RegularExpressions.Regex.Match(
                    txt, @"""simplify_quality""\s*:\s*([0-9]*\.?[0-9]+)");
                if (!m.Success) return fallback;
                float q = float.Parse(m.Groups[1].Value, System.Globalization.CultureInfo.InvariantCulture);
                return Mathf.Clamp(q, 0.01f, 1.0f);
            }
            catch { return fallback; }
        }
    }
}
