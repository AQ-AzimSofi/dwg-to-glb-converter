"""
Convert FBX + DWG to colored GLB.

Pipeline:
1. Read layer colors from DWG via ezdxf (layer name -> RGBA)
2. Load FBX geometry via pyassimp (grey, no materials)
3. Map FBX node names (Layer:XXX) to DWG layer colors
4. Filter 2D line meshes, apply world transforms
5. Export GLB with PBR materials

Inputs:
  - FBX file (exported from AutoCAD by the customer)
  - DWG file (original, for color data)

No 3ds Max or Design Automation needed.
"""

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import ezdxf
from ezdxf.colors import aci2rgb
import numpy as np
import pyassimp
import pyassimp.postprocess
import trimesh
from trimesh.visual.material import PBRMaterial


def dwg_to_dxf(dwg_path):
    """Convert DWG to DXF using ODA File Converter. Returns path to DXF."""
    if not shutil.which("ODAFileConverter"):
        print("[error] ODAFileConverter not found. Install it or provide a DXF file.")
        sys.exit(1)

    dwg_path = Path(dwg_path)
    tmp_dir = tempfile.mkdtemp(prefix="dwg2dxf_")
    input_dir = str(dwg_path.parent)
    subprocess.run(
        ["ODAFileConverter", input_dir, tmp_dir, "ACAD2018", "DXF", "0", "1", dwg_path.name],
        check=True,
        capture_output=True,
    )
    dxf_path = Path(tmp_dir) / dwg_path.with_suffix(".dxf").name
    if not dxf_path.exists():
        print(f"[error] ODA conversion failed, no output at {dxf_path}")
        sys.exit(1)
    print(f"[convert] DWG -> DXF: {dxf_path}")
    return str(dxf_path)


def build_layer_colors(path):
    """Read layer colors from DWG or DXF via ezdxf. Returns {layer_name: [R,G,B,A]}."""
    p = Path(path)
    if p.suffix.lower() == ".dwg":
        path = dwg_to_dxf(path)
    doc = ezdxf.readfile(path)
    layer_colors = {}
    for layer in doc.layers:
        aci = layer.color
        try:
            rgb = aci2rgb(aci)
            layer_colors[layer.dxf.name] = [rgb[0], rgb[1], rgb[2], 255]
        except Exception:
            layer_colors[layer.dxf.name] = [200, 200, 200, 255]

    block_to_layer = {}
    for e in doc.modelspace():
        if e.dxftype() == "INSERT":
            block_to_layer[e.dxf.name] = e.dxf.layer

    return layer_colors, block_to_layer


def fbx_to_glb(fbx_path, glb_path, layer_colors, block_to_layer):
    """Convert FBX to GLB, coloring meshes based on DWG layer colors."""
    default_color = [200, 200, 200, 255]

    with pyassimp.load(
        fbx_path,
        processing=pyassimp.postprocess.aiProcess_Triangulate,
    ) as assimp_scene:
        print(f"[convert] loaded FBX: {len(assimp_scene.meshes)} meshes")

        ptr_to_idx = {}
        for i, mesh in enumerate(assimp_scene.meshes):
            ptr = ctypes.cast(mesh, ctypes.c_void_p).value
            ptr_to_idx[ptr] = i

        mesh_entries = []

        def walk(node, parent_transform, inherited_layer=None):
            local = np.array(node.transformation, dtype=np.float64).reshape(4, 4)
            world = parent_transform @ local

            name = node.name or ""
            current_layer = inherited_layer
            clean = name.split("_$AssimpFbx$_")[0]

            if clean.startswith("Layer:"):
                layer_name = clean[6:]
                if layer_name in layer_colors:
                    current_layer = layer_name
            elif clean.startswith("Block:"):
                block_name = clean[6:]
                if block_name in block_to_layer:
                    bl = block_to_layer[block_name]
                    if bl in layer_colors:
                        current_layer = bl

            for mesh_ref in node.meshes:
                ptr = ctypes.cast(mesh_ref, ctypes.c_void_p).value
                if ptr in ptr_to_idx:
                    idx = ptr_to_idx[ptr]
                    color = layer_colors.get(current_layer, default_color) if current_layer else default_color
                    mesh_entries.append((idx, world.copy(), color))

            for child in node.children:
                walk(child, world, current_layer)

        walk(assimp_scene.rootnode, np.eye(4))

        pbr_cache = {}
        scene = trimesh.Scene()
        matched = 0
        skipped = 0

        for entry_i, (idx, world_tf, color) in enumerate(mesh_entries):
            mesh = assimp_scene.meshes[idx]
            verts = np.array(mesh.vertices, dtype=np.float64)
            faces = np.array(mesh.faces, dtype=np.int64)
            if len(verts) == 0 or len(faces) == 0:
                continue

            ones = np.ones((len(verts), 1), dtype=np.float64)
            verts_h = np.hstack([verts, ones])
            verts_world = (world_tf @ verts_h.T).T[:, :3]

            bbox_min = verts_world.min(axis=0)
            bbox_max = verts_world.max(axis=0)
            dims = sorted(bbox_max - bbox_min)
            if dims[0] < 0.1 and dims[1] < 0.1 and len(faces) <= 8:
                skipped += 1
                continue

            if color != default_color:
                matched += 1

            color_key = tuple(color)
            if color_key not in pbr_cache:
                pbr_cache[color_key] = PBRMaterial(
                    baseColorFactor=[c / 255.0 for c in color],
                    metallicFactor=0.0,
                    roughnessFactor=0.5,
                )
            material = pbr_cache[color_key]

            tmesh = trimesh.Trimesh(
                vertices=verts_world, faces=faces, process=False,
            )
            tmesh.visual = trimesh.visual.TextureVisuals(material=material)
            scene.add_geometry(tmesh, node_name=f"mesh_{entry_i}")

    bounds = scene.bounds
    if bounds is not None:
        center = (bounds[0] + bounds[1]) / 2.0
        for geom in scene.geometry.values():
            geom.vertices -= center

    total_verts = sum(len(g.vertices) for g in scene.geometry.values())
    total_meshes = len(scene.geometry)
    print(f"[filter] skipped {skipped} line-like meshes")
    print(f"[color] {matched}/{total_meshes} meshes colored")
    print(f"[convert] {total_meshes} meshes, {total_verts} vertices")
    scene.export(glb_path, file_type="glb")
    size_kb = os.path.getsize(glb_path) / 1024
    print(f"[convert] saved {glb_path} ({size_kb:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Convert FBX + DWG to colored GLB (no 3ds Max needed)"
    )
    parser.add_argument("fbx", help="Path to FBX file (geometry)")
    parser.add_argument("dwg", help="Path to DWG or DXF file (color data)")
    parser.add_argument("-o", "--output", help="Output directory")
    args = parser.parse_args()

    fbx_path = Path(args.fbx).resolve()
    dwg_path = Path(args.dwg).resolve()

    if not fbx_path.exists():
        print(f"FBX not found: {fbx_path}")
        sys.exit(1)
    if not dwg_path.exists():
        print(f"DWG not found: {dwg_path}")
        sys.exit(1)

    output_dir = Path(args.output) if args.output else Path("output_glb_fbx_dwg")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    glb_path = output_dir / f"{fbx_path.stem}.glb"

    print(f"[input] FBX: {fbx_path}")
    print(f"[input] DWG: {dwg_path}")

    layer_colors, block_to_layer = build_layer_colors(str(dwg_path))
    print(f"[color] {len(layer_colors)} layers, {len(block_to_layer)} block mappings")
    for name, rgba in layer_colors.items():
        print(f"  {name}: RGB({rgba[0]},{rgba[1]},{rgba[2]})")

    fbx_to_glb(str(fbx_path), str(glb_path), layer_colors, block_to_layer)

    print(f"\n[done] {fbx_path.name} + {dwg_path.name} -> {glb_path}")


if __name__ == "__main__":
    main()
