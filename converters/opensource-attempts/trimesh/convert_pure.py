"""
Pure ezdxf + trimesh DXF-to-GLB converter.

Uses only ezdxf's native API to read DXF entities and trimesh to export GLB.
No custom ACIS/SAT parsing. 3DSOLID entities are skipped.
"""

import argparse
import sys
from pathlib import Path

import ezdxf
import numpy as np
import trimesh

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent


def convert(dxf_path: str, output_dir: str):
    filepath = Path(dxf_path)
    doc = ezdxf.readfile(str(filepath))
    msp = doc.modelspace()

    units = doc.header.get("$INSUNITS", 0)
    scale = {
        0: 1.0, 1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0,
    }.get(units, 1.0)
    print(f"Units: {units} (scale: {scale})")

    entity_counts = {}
    for e in msp:
        t = e.dxftype()
        entity_counts[t] = entity_counts.get(t, 0) + 1
    print(f"Entity counts: {entity_counts}")

    meshes = []
    converted = {}
    skipped = {}

    for entity in msp:
        etype = entity.dxftype()

        if etype == "3DFACE":
            pts = [
                list(entity.dxf.vtx0),
                list(entity.dxf.vtx1),
                list(entity.dxf.vtx2),
            ]
            if entity.dxf.vtx3 != entity.dxf.vtx2:
                pts.append(list(entity.dxf.vtx3))
            verts = np.array(pts)
            faces = np.array([[0, 1, 2]] if len(pts) == 3 else [[0, 1, 2], [0, 2, 3]])
            meshes.append(trimesh.Trimesh(vertices=verts, faces=faces))
            converted[etype] = converted.get(etype, 0) + 1

        elif etype == "MESH":
            try:
                verts = np.array([list(v) for v in entity.vertices])
                tri_faces = []
                for face in entity.faces:
                    idxs = list(face)
                    if len(idxs) == 3:
                        tri_faces.append(idxs)
                    elif len(idxs) >= 4:
                        for i in range(1, len(idxs) - 1):
                            tri_faces.append([idxs[0], idxs[i], idxs[i + 1]])
                if tri_faces:
                    meshes.append(trimesh.Trimesh(vertices=verts, faces=np.array(tri_faces)))
                    converted[etype] = converted.get(etype, 0) + 1
            except Exception as e:
                print(f"MESH failed: {e}")
                skipped[etype] = skipped.get(etype, 0) + 1

        elif etype == "3DSOLID":
            skipped[etype] = skipped.get(etype, 0) + 1

        else:
            skipped[etype] = skipped.get(etype, 0) + 1

    print(f"Converted: {converted}")
    print(f"Skipped: {skipped}")

    if not meshes:
        print("ERROR: No geometry produced")
        return

    combined = trimesh.util.concatenate(meshes)
    if scale != 1.0:
        combined.apply_scale(scale)
    combined.apply_translation(-combined.centroid)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    glb_path = out / (filepath.stem + ".glb")
    trimesh.Scene(geometry={"model": combined}).export(str(glb_path), file_type="glb")

    print(f"Output: {glb_path}")
    print(f"Vertices: {len(combined.vertices)}, Faces: {len(combined.faces)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("-o", "--output-dir", default=str(PROJECT_ROOT / "output_glb"))
    args = parser.parse_args()
    convert(args.input, args.output_dir)
