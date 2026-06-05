// MeshyBrowser.cs — Unity Editor tool to search the Meshy RAG and import models.
//
// Pairs with meshy_server.py (the local bridge). Open via  Window > Meshy > Model Browser.
// Enter a prompt + max polygon budget, hit Search to see the top 10 (thumbnail + metadata),
// then Import to download + unpack a chosen model (GLB / OBJ + Unity-named textures) into
// Assets/MeshyImports/<name>/.
//
// Requirements:
//   * Run the bridge first:  python meshy_server.py --port 8765
//   * For GLB import in Unity, install the glTFast package (com.unity.cloud.gltfast).
//     OBJ imports natively with no package.
//
// Place this file under any folder named "Editor" in your project.

using System;
using System.Collections.Generic;
using UnityEditor;
using UnityEngine;
using UnityEngine.Networking;

namespace MeshyTool
{
    public class MeshyBrowser : EditorWindow
    {
        // ---- server / request config ----
        const string DefaultServer = "http://127.0.0.1:8765";
        string server = DefaultServer;

        // ---- query inputs ----
        string prompt = "fierce armored dragon";
        int maxPoly = 100000;
        int minPoly = 0;
        string tag = "";
        bool printableOnly = false;
        int formatIndex = 2;                  // 0=glb 1=obj 2=both
        readonly string[] formats = { "glb", "obj", "both" };

        // ---- post-process inputs (applied to the imported model in the bridge) ----
        int targetTris = 15000;               // kept for telemetry only; simplify is now Unity-side
        int textureSizeIndex = 1;             // 0=512  1=1024  2=2048  3=4096
        readonly int[] textureSizes = { 512, 1024, 2048, 4096 };
        readonly string[] textureSizeLabels = { "512", "1024", "2048", "4096" };
        float simplifyQuality = 0.50f;        // UnityMeshSimplifier quality (fraction of tris to keep)

        // ---- state ----
        List<ModelResult> results = new List<ModelResult>();
        readonly Dictionary<string, Texture2D> thumbs = new Dictionary<string, Texture2D>();
        Vector2 scroll;
        string status = "Idle. Start the Python bridge, then Search.";
        bool busy = false;

        // ---- test hooks (used by MeshyE2ETest.cs to drive the window headlessly) ----
        public bool IsBusy => busy;
        public string Status => status;
        public int ResultCount => results.Count;
        public void DriveSearch(string promptIn, int maxPolyIn, int minPolyIn, string tagIn, int formatIdxIn)
        {
            prompt = promptIn; maxPoly = maxPolyIn; minPoly = minPolyIn; tag = tagIn ?? "";
            formatIndex = Mathf.Clamp(formatIdxIn, 0, formats.Length - 1);
            DoSearch();
        }
        public void DriveImport(int index)
        {
            if (index < 0 || index >= results.Count) { status = "DriveImport: index out of range"; return; }
            DoImport(results[index]);
        }
        public ModelResult GetResult(int i) => (i >= 0 && i < results.Count) ? results[i] : null;

        // in-flight async web requests, polled from EditorApplication.update
        class Job { public UnityWebRequest req; public Action<UnityWebRequest> onDone; }
        readonly List<Job> jobs = new List<Job>();

        [MenuItem("Window/Meshy/Model Browser")]
        public static void Open() => GetWindow<MeshyBrowser>("Meshy Browser");

        void OnEnable() { EditorApplication.update += Poll; }
        void OnDisable() { EditorApplication.update -= Poll; }

        void Poll()
        {
            for (int i = jobs.Count - 1; i >= 0; i--)
            {
                var op = jobs[i].req;
                if (op.isDone)
                {
                    try { jobs[i].onDone(op); } finally { op.Dispose(); jobs.RemoveAt(i); }
                    Repaint();
                }
            }
        }

        void Send(UnityWebRequest req, Action<UnityWebRequest> onDone, int timeoutSec = 30)
        {
            req.timeout = timeoutSec;
            req.SendWebRequest();
            jobs.Add(new Job { req = req, onDone = onDone });
        }

        // ----------------------------------------------------------------- GUI

        void OnGUI()
        {
            EditorGUILayout.Space();
            EditorGUILayout.LabelField("Meshy Model Browser", EditorStyles.boldLabel);
            server = EditorGUILayout.TextField("Bridge URL", server);

            EditorGUILayout.Space();
            prompt = EditorGUILayout.TextField("Prompt", prompt);
            maxPoly = EditorGUILayout.IntField("Max polygons (tris)", maxPoly);
            using (new EditorGUILayout.HorizontalScope())
            {
                minPoly = EditorGUILayout.IntField("Min polygons", minPoly);
                tag = EditorGUILayout.TextField("Tag filter", tag);
            }
            using (new EditorGUILayout.HorizontalScope())
            {
                printableOnly = EditorGUILayout.ToggleLeft("3D-printable only", printableOnly, GUILayout.Width(150));
                formatIndex = EditorGUILayout.Popup("Import as", formatIndex, formats);
            }

            EditorGUILayout.Space();
            EditorGUILayout.LabelField("Post-process (applied on import)", EditorStyles.miniBoldLabel);
            simplifyQuality = EditorGUILayout.Slider(
                new GUIContent("Simplify quality", "Fraction of triangles to keep. 0.10 = aggressive, 0.50 = balanced, 1.0 = no simplify."),
                simplifyQuality, 0.05f, 1.0f);
            using (new EditorGUILayout.HorizontalScope())
            {
                textureSizeIndex = EditorGUILayout.Popup("Texture size", textureSizeIndex, textureSizeLabels);
                // Spacer so the slider doesn't squish — keep poly hint visible:
                EditorGUILayout.LabelField($"≈ {(int)(simplifyQuality * 100)}% poly", EditorStyles.miniLabel,
                                           GUILayout.Width(90));
            }

            EditorGUILayout.Space();
            using (new EditorGUI.DisabledScope(busy))
            {
                if (GUILayout.Button("Search", GUILayout.Height(28))) DoSearch();
            }

            EditorGUILayout.HelpBox(status, busy ? MessageType.Info : MessageType.None);

            // ---- results grid ----
            scroll = EditorGUILayout.BeginScrollView(scroll);
            foreach (var r in results) DrawResult(r);
            EditorGUILayout.EndScrollView();
        }

        void DrawResult(ModelResult r)
        {
            using (new EditorGUILayout.HorizontalScope(EditorStyles.helpBox))
            {
                // thumbnail
                Texture2D t = thumbs.TryGetValue(r.result_id, out var tex) ? tex : null;
                Rect box = GUILayoutUtility.GetRect(96, 96, GUILayout.Width(96), GUILayout.Height(96));
                if (t != null) GUI.DrawTexture(box, t, ScaleMode.ScaleToFit);
                else EditorGUI.DrawRect(box, new Color(0.2f, 0.2f, 0.2f));

                // metadata
                using (new EditorGUILayout.VerticalScope())
                {
                    EditorGUILayout.LabelField(string.IsNullOrEmpty(r.prompt) ? "(untitled)" : r.prompt,
                                               EditorStyles.boldLabel);
                    EditorGUILayout.LabelField($"{r.triangle_count:n0} tris   •   {r.downloads} downloads   •   {r.license}");
                    EditorGUILayout.LabelField($"tags: {(r.tags != null ? string.Join(", ", r.tags) : "")}",
                                               EditorStyles.miniLabel);
                    EditorGUILayout.LabelField($"match distance: {r.distance:0.000}", EditorStyles.miniLabel);
                    using (new EditorGUILayout.HorizontalScope())
                    {
                        using (new EditorGUI.DisabledScope(busy))
                            if (GUILayout.Button("Import", GUILayout.Width(90))) DoImport(r);
                        if (GUILayout.Button("Open page", GUILayout.Width(90)))
                            Application.OpenURL(r.public_url);
                    }
                }
            }
        }

        // ----------------------------------------------------------------- actions

        void DoSearch()
        {
            results.Clear(); thumbs.Clear();
            busy = true; status = "Searching…";
            string url = $"{server}/search?q={UnityWebRequest.EscapeURL(prompt)}" +
                         $"&max_tris={maxPoly}&min_tris={minPoly}&limit=10" +
                         (string.IsNullOrEmpty(tag) ? "" : $"&tag={UnityWebRequest.EscapeURL(tag)}") +
                         (printableOnly ? "&printable=1" : "");
            Send(UnityWebRequest.Get(url), op =>
            {
                busy = false;
                if (op.result != UnityWebRequest.Result.Success)
                { status = $"Search failed: {op.error}. Is the bridge running at {server}?"; return; }
                var resp = JsonUtility.FromJson<SearchResponse>(op.downloadHandler.text);
                if (resp == null || !resp.ok) { status = "Search returned no results."; return; }
                results = new List<ModelResult>(resp.results ?? Array.Empty<ModelResult>());
                status = $"{results.Count} results.";
                foreach (var r in results) FetchThumb(r);
            });
        }

        void FetchThumb(ModelResult r)
        {
            if (string.IsNullOrEmpty(r.thumbnail_url)) return;
            Send(UnityWebRequestTexture.GetTexture(r.thumbnail_url), op =>
            {
                if (op.result == UnityWebRequest.Result.Success)
                    thumbs[r.result_id] = DownloadHandlerTexture.GetContent(op);
            });
        }

        void DoImport(ModelResult r)
        {
            busy = true;
            status = $"Importing “{r.prompt}” … (browser capture can take ~60–120s)";
            string name = Sanitize(string.IsNullOrEmpty(r.prompt) ? r.result_id : r.prompt);
            string dest = System.IO.Path.Combine(Application.dataPath, "MeshyImports");
            string url = $"{server}/import?result_id={r.result_id}" +
                         $"&name={UnityWebRequest.EscapeURL(name)}" +
                         $"&dest={UnityWebRequest.EscapeURL(dest)}" +
                         $"&format={formats[formatIndex]}&timeout=150" +
                         $"&target_tris={Mathf.Max(100, targetTris)}" +
                         $"&texture_max_size={textureSizes[textureSizeIndex]}" +
                         $"&simplify_quality={simplifyQuality.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture)}";
            Send(UnityWebRequest.Get(url), op =>
            {
                busy = false;
                if (op.result != UnityWebRequest.Result.Success)
                { status = $"Import failed: {op.error}"; return; }
                var resp = JsonUtility.FromJson<ImportResponse>(op.downloadHandler.text);
                if (resp == null || !resp.ok)
                { status = $"Import failed: {(resp != null ? resp.error : "unknown")}"; return; }
                AssetDatabase.Refresh();
                status = $"Imported to Assets/MeshyImports/{name}/ ({resp.files.Length} files).";
                var folder = AssetDatabase.LoadAssetAtPath<UnityEngine.Object>($"Assets/MeshyImports/{name}");
                if (folder != null) EditorGUIUtility.PingObject(folder);
            }, timeoutSec: 180);
        }

        static string Sanitize(string s)
        {
            var sb = new System.Text.StringBuilder();
            foreach (char c in s) sb.Append(char.IsLetterOrDigit(c) ? c : '_');
            string outp = sb.ToString().Trim('_');
            return string.IsNullOrEmpty(outp) ? "MeshyModel" : (outp.Length > 48 ? outp.Substring(0, 48) : outp);
        }

        // ----------------------------------------------------------------- JSON DTOs
        [Serializable] public class SearchResponse { public bool ok; public ModelResult[] results; }
        [Serializable] public class ModelResult
        {
            public string result_id, public_url, prompt, license, thumbnail_url;
            public int triangle_count, views, downloads;
            public bool is_3d_printable;
            public float distance;
            public string[] tags;
        }
        [Serializable] public class ImportResponse { public bool ok; public string folder, error; public string[] files; }
    }
}
