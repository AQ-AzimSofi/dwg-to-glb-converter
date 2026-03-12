"""
Pure Blender DXF-to-GLB converter.

Uses only Blender's built-in DXF import addon (io_import_dxf) and GLB export.
No ezdxf, no custom geometry building. Run via:
    ~/blender/blender --background --python convert_pure.py -- input.dxf output.glb
"""

import sys
import bpy
from pathlib import Path


def main():
    argv = sys.argv
    if "--" not in argv:
        print("ERROR: usage: blender --background --python convert_pure.py -- input.dxf output.glb")
        return

    args = argv[argv.index("--") + 1:]
    if len(args) < 2:
        print("ERROR: need input and output paths")
        return

    input_path = Path(args[0])
    output_path = Path(args[1])

    # Clear default scene
    bpy.ops.wm.read_factory_settings(use_empty=True)

    ext = input_path.suffix.lower()

    if ext == ".dxf":
        # Try Blender's built-in DXF import addon
        try:
            bpy.ops.preferences.addon_enable(module="io_import_dxf")
            bpy.ops.import_scene.dxf(filepath=str(input_path))
        except Exception as e:
            print(f"ERROR: Blender has no DXF import capability: {e}")
            print("Blender 4.2 removed the io_import_dxf addon.")
            print("Blender cannot natively import DXF files.")
            return

    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=str(input_path))

    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=str(input_path))

    else:
        print(f"ERROR: unsupported format: {ext}")
        return

    mesh_objects = [obj for obj in bpy.data.objects if obj.type == "MESH"]
    print(f"Imported {len(mesh_objects)} mesh objects")

    if not mesh_objects:
        print("ERROR: No geometry imported")
        return

    total_verts = sum(len(obj.data.vertices) for obj in mesh_objects)
    total_faces = sum(len(obj.data.polygons) for obj in mesh_objects)
    print(f"Total vertices: {total_verts}, faces: {total_faces}")

    # Select all and export
    bpy.ops.object.select_all(action="SELECT")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.gltf(
        filepath=str(output_path),
        export_format="GLB",
        use_selection=True,
        export_apply=True,
    )

    print(f"Output: {output_path}")
    print(f"Vertices: {total_verts}, Faces: {total_faces}")


if __name__ == "__main__":
    main()
