#!/usr/bin/env python3
"""Convert a binary glTF (.glb) to Wavefront .obj + .mtl, stdlib only.

Handles the common case Meshy produces: one or more mesh primitives with POSITION,
NORMAL and TEXCOORD_0 attributes and triangle indices, stored in the GLB's BIN chunk.
Writes vertices/normals/uvs/faces and an .mtl that points at a base-color texture if one
is provided. No Blender, no external deps.

Usage:
  python glb_to_obj.py model.glb --out model.obj [--basecolor texture_0.png]
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

# glTF componentType -> (struct char, byte size)
COMPONENT = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2),
             5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
# glTF type -> number of components
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def parse_glb(path):
    with open(path, "rb") as f:
        data = f.read()
    magic, ver, _ = struct.unpack("<4sII", data[:12])
    if magic != b"glTF":
        sys.exit(f"{path} is not a binary glTF (magic={magic!r})")
    off, gltf, bin_chunk = 12, None, b""
    import json
    while off < len(data):
        clen, ctype = struct.unpack("<II", data[off:off + 8])
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == 0x4E4F534A:       # 'JSON'
            gltf = json.loads(chunk)
        elif ctype == 0x004E4942:     # 'BIN\0'
            bin_chunk = chunk
    return gltf, bin_chunk


def read_accessor(gltf, bin_chunk, idx):
    """Return a list of tuples (one per element) for accessor `idx`."""
    acc = gltf["accessors"][idx]
    bv = gltf["bufferViews"][acc["bufferView"]]
    comp_char, comp_size = COMPONENT[acc["componentType"]]
    n = NCOMP[acc["type"]]
    count = acc["count"]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or (comp_size * n)
    out = []
    for i in range(count):
        start = base + i * stride
        vals = struct.unpack_from("<" + comp_char * n, bin_chunk, start)
        out.append(vals)
    return out


def _read_draco(gltf, bin_chunk, prim, draco):
    """Decode a KHR_draco_mesh_compression primitive -> (positions, normals, uvs, indices)."""
    try:
        import DracoPy
    except ImportError:
        sys.exit("This model is Draco-compressed. Install the decoder: pip install DracoPy")
    bv = gltf["bufferViews"][draco["bufferView"]]
    start = bv.get("byteOffset", 0)
    buf = bin_chunk[start:start + bv["byteLength"]]
    m = DracoPy.decode(buf)
    positions = [tuple(p) for p in m.points]
    normals = [tuple(n) for n in m.normals] if getattr(m, "normals", None) is not None and len(m.normals) else None
    uvs = [tuple(t) for t in m.tex_coord] if getattr(m, "tex_coord", None) is not None and len(m.tex_coord) else None
    indices = [int(i) for face in m.faces for i in face]
    return positions, normals, uvs, indices


def _node_matrix(node):
    """Local 4x4 transform of a glTF node, from `matrix` or TRS (numpy)."""
    import numpy as np
    if "matrix" in node:  # column-major in glTF
        return np.array(node["matrix"], dtype=float).reshape(4, 4).T
    T = np.eye(4)
    if "translation" in node:
        T[:3, 3] = node["translation"]
    R = np.eye(4)
    if "rotation" in node:  # quaternion [x,y,z,w]
        x, y, z, w = node["rotation"]
        R[:3, :3] = [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    S = np.eye(4)
    if "scale" in node:
        S[0, 0], S[1, 1], S[2, 2] = node["scale"]
    return T @ R @ S


def mesh_world_matrices(gltf):
    """Map mesh index -> world 4x4 matrix by walking the scene graph (first instance)."""
    import numpy as np
    nodes = gltf.get("nodes", [])
    roots = (gltf.get("scenes", [{}])[gltf.get("scene", 0)].get("nodes")
             if gltf.get("scenes") else range(len(nodes)))
    out = {}

    def walk(ni, parent):
        node = nodes[ni]
        world = parent @ _node_matrix(node)
        if "mesh" in node and node["mesh"] not in out:
            out[node["mesh"]] = world
        for ch in node.get("children", []):
            walk(ch, world)

    for r in (roots or []):
        walk(r, np.eye(4))
    return out


def convert(glb_path, obj_path, basecolor=None):
    import numpy as np
    gltf, bin_chunk = parse_glb(glb_path)
    if not gltf.get("meshes"):
        sys.exit("no meshes in glTF")

    mtl_path = os.path.splitext(obj_path)[0] + ".mtl"
    name = os.path.splitext(os.path.basename(obj_path))[0]
    worlds = mesh_world_matrices(gltf)  # bake KHR_mesh_quantization scale + node TRS

    v_lines, vt_lines, vn_lines, f_lines = [], [], [], []
    v_off = vt_off = vn_off = 0  # OBJ indices are 1-based and global across the file

    for mi, mesh in enumerate(gltf["meshes"]):
        world = worlds.get(mi, np.eye(4))
        normal_mat = np.linalg.inv(world[:3, :3]).T  # for transforming normals
        for pi, prim in enumerate(mesh.get("primitives", [])):
            attrs = prim.get("attributes", {})
            draco = (prim.get("extensions") or {}).get("KHR_draco_mesh_compression")
            if draco:
                positions, normals, uvs, indices = _read_draco(gltf, bin_chunk, prim, draco)
            else:
                if "POSITION" not in attrs:
                    continue
                positions = read_accessor(gltf, bin_chunk, attrs["POSITION"])
                normals = read_accessor(gltf, bin_chunk, attrs["NORMAL"]) if "NORMAL" in attrs else None
                uvs = read_accessor(gltf, bin_chunk, attrs["TEXCOORD_0"]) if "TEXCOORD_0" in attrs else None
                indices = ([x[0] for x in read_accessor(gltf, bin_chunk, prim["indices"])]
                           if "indices" in prim else list(range(len(positions))))

            pos = np.array(positions, dtype=float)[:, :3]
            pos = (world @ np.c_[pos, np.ones(len(pos))].T).T[:, :3]
            for x, y, z in pos:
                v_lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
            if normals:
                nrm = np.array(normals, dtype=float)[:, :3] @ normal_mat.T
                norms = np.linalg.norm(nrm, axis=1, keepdims=True)
                nrm = nrm / np.where(norms == 0, 1, norms)
                for x, y, z in nrm:
                    vn_lines.append(f"vn {x:.6f} {y:.6f} {z:.6f}")
            if uvs:
                for u, w in uvs:
                    # glTF UV origin is top-left; OBJ is bottom-left -> flip V
                    vt_lines.append(f"vt {u:.6f} {1.0 - w:.6f}")

            f_lines.append(f"g {name}_{pi}")
            if basecolor:
                f_lines.append("usemtl material_0")
            for t in range(0, len(indices) - 2, 3):
                a, b, c = indices[t] + 1, indices[t + 1] + 1, indices[t + 2] + 1
                def vert(k):
                    vi = k + v_off
                    ti = (k + vt_off) if uvs else ""
                    ni = (k + vn_off) if normals else ""
                    if uvs and normals:
                        return f"{vi}/{ti}/{ni}"
                    if normals:
                        return f"{vi}//{ni}"
                    if uvs:
                        return f"{vi}/{ti}"
                    return f"{vi}"
                f_lines.append(f"f {vert(indices[t])} {vert(indices[t+1])} {vert(indices[t+2])}")
            v_off += len(positions)
            if uvs:
                vt_off += len(uvs)
            if normals:
                vn_off += len(normals)

    with open(obj_path, "w", encoding="utf-8") as f:
        f.write(f"# converted from {os.path.basename(glb_path)} by glb_to_obj.py\n")
        if basecolor:
            f.write(f"mtllib {os.path.basename(mtl_path)}\n")
        f.write("\n".join(v_lines) + "\n")
        if vt_lines:
            f.write("\n".join(vt_lines) + "\n")
        if vn_lines:
            f.write("\n".join(vn_lines) + "\n")
        f.write("\n".join(f_lines) + "\n")

    if basecolor:
        with open(mtl_path, "w", encoding="utf-8") as f:
            f.write("newmtl material_0\n")
            f.write("Ka 1.000 1.000 1.000\nKd 1.000 1.000 1.000\nillum 2\n")
            f.write(f"map_Kd {os.path.basename(basecolor)}\n")

    tri_count = len(f_lines) - sum(1 for ln in f_lines if ln.startswith(("g ", "usemtl")))
    return {"obj": obj_path, "mtl": mtl_path if basecolor else None,
            "vertices": v_off, "triangles": tri_count}


def main(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("glb")
    p.add_argument("--out", required=True, help="output .obj path")
    p.add_argument("--basecolor", help="base-color texture filename to reference in the .mtl")
    args = p.parse_args(argv)
    import json
    print(json.dumps(convert(args.glb, args.out, args.basecolor), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
