"""
Pure FreeCAD DXF-to-GLB converter.

Uses ODA FileConverter (DWG->DXF) + FreeCAD's importDXF module + Mesh export.
No custom SAT/ACIS parsing. Run via:
    ~/freecad/squashfs-root/usr/bin/freecadcmd convert_pure.py input.dwg output_dir
"""

import sys
import os

# FreeCAD must be invoked via freecadcmd which sets up its Python environment
try:
    import FreeCAD
    import importDXF
    import Mesh
    import MeshPart
except ImportError:
    print("ERROR: Must be run via freecadcmd, not regular Python")
    sys.exit(1)

import subprocess
import tempfile
from pathlib import Path


def dwg_to_dxf(dwg_path, output_dir):
    """Convert DWG to DXF using ODA FileConverter."""
    cmd = [
        "xvfb-run", "-a", "ODAFileConverter",
        str(Path(dwg_path).parent), str(output_dir),
        "ACAD2010", "DXF", "0", "1",
    ]
    subprocess.run(cmd, capture_output=True, timeout=120)
    dxf = Path(output_dir) / (Path(dwg_path).stem + ".dxf")
    if not dxf.exists():
        raise RuntimeError("ODA conversion failed")
    return str(dxf)


def convert(input_path, output_dir):
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ext = input_path.suffix.lower()

    if ext == ".dwg":
        print(f"Converting DWG to DXF via ODA...")
        tmp_dir = tempfile.mkdtemp(prefix="oda_")
        dxf_path = dwg_to_dxf(str(input_path), tmp_dir)
        print(f"DXF: {dxf_path}")
    elif ext == ".dxf":
        dxf_path = str(input_path)
    else:
        print(f"ERROR: unsupported format: {ext}")
        return

    # Use FreeCAD's native DXF import
    print("Importing DXF via FreeCAD importDXF...")
    doc = FreeCAD.newDocument("conversion")
    try:
        importDXF.insert(dxf_path, doc.Name)
    except Exception as e:
        print(f"importDXF error: {e}")

    objects = doc.Objects
    print(f"FreeCAD imported {len(objects)} objects")

    for obj in objects:
        print(f"  {obj.Name}: {obj.TypeId}")

    # Try to export shapes as mesh
    shape_objects = [obj for obj in objects if hasattr(obj, "Shape") and obj.Shape.Faces]
    print(f"Objects with faces: {len(shape_objects)}")

    if not shape_objects:
        print("ERROR: No tessellatable geometry found")
        # Still export what we have as STL (may be empty)
        stl_path = output_dir / (input_path.stem + ".stl")
        try:
            meshes = []
            for obj in objects:
                if hasattr(obj, "Shape"):
                    mesh = MeshPart.meshFromShape(obj.Shape, LinearDeflection=0.1, AngularDeflection=0.5)
                    meshes.append(mesh)
            if meshes:
                combined = meshes[0]
                for m in meshes[1:]:
                    combined.addMesh(m)
                combined.write(str(stl_path))
                print(f"Exported STL: {stl_path} ({combined.CountPoints} points, {combined.CountFacets} facets)")
            else:
                print("No mesh data to export")
        except Exception as e:
            print(f"Mesh export failed: {e}")
        return

    # Tessellate and export
    stl_path = output_dir / (input_path.stem + ".stl")
    try:
        meshes = []
        for obj in shape_objects:
            mesh = MeshPart.meshFromShape(obj.Shape, LinearDeflection=0.1, AngularDeflection=0.5)
            meshes.append(mesh)
        combined = meshes[0]
        for m in meshes[1:]:
            combined.addMesh(m)
        combined.write(str(stl_path))
        print(f"Exported STL: {stl_path} ({combined.CountPoints} points, {combined.CountFacets} facets)")
    except Exception as e:
        print(f"Export failed: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: freecadcmd convert_pure.py input.dwg output_dir")
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2])
