// MeshyAssetPostprocessor.cs — fix TextureImporter settings for files Meshy-imported by the
// bridge. Without this, Unity defaults sRGB ON for everything (which wrecks Normal/Metallic/
// Roughness/Occlusion sampling) and never marks the normal map as a NormalMap type.
//
// Only kicks in for paths under Assets/MeshyImports/ and only on files whose name ends in
// one of the suffixes the bridge writes (_BaseMap, _Normal, _MetallicRoughness, _Occlusion,
// _Emission). All other texture imports in the project are untouched.

using System.IO;
using UnityEditor;
using UnityEngine;

namespace MeshyTool
{
    public class MeshyAssetPostprocessor : AssetPostprocessor
    {
        const string PrefixDir = "Assets/MeshyImports/";

        void OnPreprocessTexture()
        {
            string path = assetPath;
            if (!path.StartsWith(PrefixDir)) return;

            var importer = (TextureImporter)assetImporter;
            string name = Path.GetFileNameWithoutExtension(path);
            string suffix = ExtractSuffix(name);
            if (suffix == null) return;

            // Common defaults for all Meshy textures
            importer.maxTextureSize = 1024;
            importer.mipmapEnabled = true;
            importer.streamingMipmaps = true;
            importer.wrapMode = TextureWrapMode.Repeat;
            importer.filterMode = FilterMode.Bilinear;
            importer.anisoLevel = 4;

            switch (suffix)
            {
                case "_BaseMap":
                    importer.textureType = TextureImporterType.Default;
                    importer.sRGBTexture = true;          // albedo is sRGB
                    importer.alphaSource = TextureImporterAlphaSource.FromInput;
                    break;

                case "_Normal":
                    importer.textureType = TextureImporterType.NormalMap;
                    importer.sRGBTexture = false;
                    importer.alphaSource = TextureImporterAlphaSource.None;
                    importer.convertToNormalmap = false;  // it already IS a normal map
                    break;

                case "_MetallicRoughness":
                case "_Metallic":
                case "_Roughness":
                case "_Occlusion":
                    importer.textureType = TextureImporterType.Default;
                    importer.sRGBTexture = false;         // data textures must be linear
                    importer.alphaSource = TextureImporterAlphaSource.None;
                    break;

                case "_Emission":
                    importer.textureType = TextureImporterType.Default;
                    importer.sRGBTexture = true;          // emission colour is sRGB
                    importer.alphaSource = TextureImporterAlphaSource.None;
                    break;
            }

            // Per-platform compression: BC7 on desktop, ASTC on mobile.
            ApplyPlatformOverride(importer, "Standalone", TextureImporterFormat.BC7);
            ApplyPlatformOverride(importer, "Android", TextureImporterFormat.ASTC_6x6);
            ApplyPlatformOverride(importer, "iPhone", TextureImporterFormat.ASTC_6x6);
            ApplyPlatformOverride(importer, "WebGL", TextureImporterFormat.DXT5);
        }

        static void ApplyPlatformOverride(TextureImporter importer, string platform, TextureImporterFormat fmt)
        {
            var settings = importer.GetPlatformTextureSettings(platform);
            settings.overridden = true;
            settings.maxTextureSize = importer.maxTextureSize;
            settings.format = fmt;
            settings.textureCompression = TextureImporterCompression.Compressed;
            importer.SetPlatformTextureSettings(settings);
        }

        static string ExtractSuffix(string nameWithoutExt)
        {
            // basename + "_" + suffix-without-leading-underscore  e.g. "low_poly_sword_BaseMap"
            int idx = nameWithoutExt.LastIndexOf('_');
            return idx > 0 ? nameWithoutExt.Substring(idx) : null;
        }
    }
}
