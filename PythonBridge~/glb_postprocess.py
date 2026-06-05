#!/usr/bin/env python3
"""Turn a captured Meshy GLB into a game-ready asset.

Two jobs:
  1. **Decimate the mesh** to a target triangle count via `npx @gltf-transform/cli`
     (`weld` to merge duplicate vertices, then `simplify` using meshoptimizer). Way better
     quality than trimesh's quadric decimation and writes a clean GLB out.
  2. **Extract the GLB's embedded PBR textures** (not the corrupted ~200-byte placeholder
     PNGs Meshy serves on /output/<map>). The captured GLB stores the real textures inside
     its BIN chunk, with `materials[*].pbrMetallicRoughness.*Texture` pointers we walk to
     label each image by PBR slot. WebP textures (Meshy uses EXT_texture_webp on some
     models) are decoded via Pillow.

Outputs into <out_dir>:
    <basename>.glb                              -- decimated GLB
    <basename>_BaseMap.png                      -- sRGB color
    <basename>_Normal.png                       -- normal map (tangent space)
    <basename>_MetallicRoughness.png            -- glTF packing: G=roughness, B=metallic
    <basename>_Occlusion.png                    -- if present
    <basename>_Emission.png                     -- if present

The Unity-side `MeshyAssetPostprocessor.cs` then sets each TextureImporter type
(sRGB on/off, NormalMap, etc.) based on the suffix.

CLI:
    python glb_postprocess.py input.glb --out-dir DIR --basename NAME
                              [--target-tris 15000] [--texture-max-size 1024]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

# glTF accessor mapping: index in `materials[m].extensions.EXT_texture_webp.source`
# is also handled if EXT_texture_webp is present.
PBR_SLOTS = [
    # (json path tuple, output suffix)
    (("pbrMetallicRoughness", "baseColorTexture"),         "_BaseMap"),
    (("normalTexture",),                                    "_Normal"),
    (("pbrMetallicRoughness", "metallicRoughnessTexture"), "_MetallicRoughness"),
    (("occlusionTexture",),                                 "_Occlusion"),
    (("emissiveTexture",),                                  "_Emission"),
]


def parse_glb(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"glTF":
        raise ValueError(f"{path} is not a binary glTF")
    off = 12
    gltf, bin_chunk = None, b""
    while off < len(data):
        clen, ctype = struct.unpack("<II", data[off:off + 8])
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == 0x4E4F534A:        # JSON
            gltf = json.loads(chunk)
        elif ctype == 0x004E4942:      # BIN\0
            bin_chunk = chunk
    return gltf, bin_chunk


def _texture_image_index(gltf, tex_idx):
    """Resolve a texture index to its image index, handling EXT_texture_webp."""
    tex = gltf["textures"][tex_idx]
    src = tex.get("source")
    if src is None:
        # extension-based source (e.g. EXT_texture_webp.source)
        for ext in (tex.get("extensions") or {}).values():
            if isinstance(ext, dict) and "source" in ext:
                src = ext["source"]
                break
    return src


def _walk_slot(material, path):
    cur = material
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    if isinstance(cur, dict):
        return cur.get("index")
    return None


def extract_image_bytes(gltf, bin_chunk, image_idx):
    """Return (bytes, mime) for a glTF image by index."""
    img = gltf["images"][image_idx]
    mime = img.get("mimeType", "image/png")
    if "bufferView" in img:
        bv = gltf["bufferViews"][img["bufferView"]]
        start = bv.get("byteOffset", 0)
        return bin_chunk[start:start + bv["byteLength"]], mime
    if "uri" in img and img["uri"].startswith("data:"):
        import base64
        head, b64 = img["uri"].split(",", 1)
        return base64.b64decode(b64), mime
    raise ValueError(f"image[{image_idx}] has no embedded data")


def decode_to_pil(img_bytes, mime):
    from PIL import Image
    import io
    return Image.open(io.BytesIO(img_bytes))


def save_as_png(pil_img, path, max_size):
    """Save a PIL image as PNG, resized so its longest side is <= max_size."""
    from PIL import Image
    if max_size and max(pil_img.size) > max_size:
        scale = max_size / max(pil_img.size)
        new_size = (max(1, int(pil_img.size[0] * scale)),
                    max(1, int(pil_img.size[1] * scale)))
        pil_img = pil_img.resize(new_size, Image.LANCZOS)
    if pil_img.mode not in ("RGB", "RGBA", "L"):
        pil_img = pil_img.convert("RGBA")
    pil_img.save(path, "PNG", optimize=True)


def extract_textures(glb_path, out_dir, basename, max_size):
    """Pull textures out of the GLB, label by PBR slot, save Unity-named PNGs."""
    from PIL import Image
    gltf, bin_chunk = parse_glb(glb_path)
    saved = {}
    if not gltf.get("materials"):
        return saved
    # We currently only handle the first material — Meshy ships one material per model.
    material = gltf["materials"][0]
    for path, suffix in PBR_SLOTS:
        tex_idx = _walk_slot(material, path)
        if tex_idx is None:
            continue
        try:
            img_idx = _texture_image_index(gltf, tex_idx)
            if img_idx is None:
                continue
            img_bytes, mime = extract_image_bytes(gltf, bin_chunk, img_idx)
            pil_img = decode_to_pil(img_bytes, mime)
            out_path = os.path.join(out_dir, basename + suffix + ".png")
            save_as_png(pil_img, out_path, max_size)
            saved[suffix] = out_path
        except Exception as e:
            print(f"  warn: failed to extract {suffix}: {e}", file=sys.stderr)
    return saved


def count_tris(glb_path):
    gltf, _ = parse_glb(glb_path)
    n = 0
    for mesh in gltf.get("meshes", []):
        for prim in mesh.get("primitives", []):
            if "indices" in prim:
                n += gltf["accessors"][prim["indices"]]["count"] // 3
    return n


def _npx():
    """Resolve `npx` to its real path — on Windows it's an .cmd shim subprocess can't find on PATH alone."""
    return shutil.which("npx") or shutil.which("npx.cmd") or "npx"


def _run_gltf_transform(args):
    """Invoke gltf-transform via npx, returning (stdout, stderr) and raising on failure."""
    cmd = [_npx(), "--yes", "@gltf-transform/cli@latest", *args]
    r = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gltf-transform {args[0]} failed: {r.stderr.strip()[:400]}")
    return r.stdout, r.stderr


def make_game_ready(input_glb, output_glb, target_tris, texture_size=1024,
                    error=0.01, lock_border=True, use_draco=True):
    """Texture/file-size cleanup pipeline. NO geometry simplification — that now lives on
    the Unity side (UnityMeshSimplifier in the AssetPostprocessor) where it has access to
    real Mesh data and proper UV-seam preservation.

        weld   -> merge duplicate vertices
        resize -> bake textures down to texture_size  (the big file-size win)
        draco  -> compress geometry; matches the project's already-installed Draco pkg

    target_tris/error/lock_border are accepted for back-compat but only used to report
    intent in the metadata; the Unity-side postprocessor reads its own target.
    """
    current = count_tris(input_glb)

    with tempfile.TemporaryDirectory() as td:
        a = os.path.join(td, "a.glb")
        b = os.path.join(td, "b.glb")
        _run_gltf_transform(["weld", input_glb, a])
        if texture_size and texture_size > 0:
            _run_gltf_transform(["resize", a, b, "--width", str(texture_size),
                                 "--height", str(texture_size)])
        else:
            shutil.copy2(a, b)
        if use_draco:
            _run_gltf_transform(["draco", b, output_glb])
        else:
            shutil.copy2(b, output_glb)

    new_tris = count_tris(output_glb)
    return {"current_tris": current, "target_tris": target_tris,
            "result_tris": new_tris,
            "simplify_done_in": "Unity (UnityMeshSimplifier)" if current > target_tris else "skipped (input already low-poly)",
            "draco": use_draco, "texture_size_baked": texture_size}


# Back-compat alias used elsewhere in this codebase.
decimate = make_game_ready


def main(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="path to captured GLB")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--basename", required=True, help="filename stem (e.g. low_poly_sword)")
    p.add_argument("--target-tris", type=int, default=15000,
                   help="decimate down to this many triangles (default 15000)")
    p.add_argument("--texture-max-size", type=int, default=1024,
                   help="resize textures so longest side is <= this (default 1024; 0 = no resize)")
    p.add_argument("--error", type=float, default=0.01,
                   help="simplify error budget — fraction of mesh radius (default 0.01)")
    p.add_argument("--no-lock-border", action="store_true",
                   help="don't preserve topological borders during simplify")
    p.add_argument("--no-draco", action="store_true",
                   help="don't apply KHR_draco_mesh_compression to the output GLB")
    args = p.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    out_glb = os.path.join(args.out_dir, args.basename + ".glb")

    decim = make_game_ready(args.input, out_glb, args.target_tris,
                            texture_size=args.texture_max_size,
                            error=args.error,
                            lock_border=not args.no_lock_border,
                            use_draco=not args.no_draco)
    # Extract textures FROM THE OUTPUT GLB so the sidecar PNGs match the in-GLB resized ones.
    tex = extract_textures(out_glb, args.out_dir, args.basename, args.texture_max_size or None)

    print(json.dumps({"ok": True, "glb": out_glb, "decimate": decim, "textures": tex}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
