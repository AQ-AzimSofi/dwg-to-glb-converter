"""
Inner Blender script for DXF/STL/OBJ to GLB conversion.

This script runs inside Blender's Python environment (via --background --python).
It receives arguments as a JSON string after "--" on the command line.

Do not run this script directly; use convert_blender.py instead.
"""

import json
import math
import sys
from pathlib import Path

import bpy
import bmesh
from mathutils import Matrix, Vector


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for mesh in bpy.data.meshes:
        bpy.data.meshes.remove(mesh)
    for mat in bpy.data.materials:
        bpy.data.materials.remove(mat)


def get_or_create_material(r, g, b, a=255):
    name = f"mat_{r}_{g}_{b}"
    mat = bpy.data.materials.get(name)
    if mat:
        return mat
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (r / 255, g / 255, b / 255, 1.0)
    return mat


ACI_COLORS = {
    1: (255, 0, 0),
    2: (255, 255, 0),
    3: (0, 255, 0),
    4: (0, 255, 255),
    5: (0, 0, 255),
    6: (255, 0, 255),
    7: (255, 255, 255),
    8: (128, 128, 128),
    9: (192, 192, 192),
}

UNIT_TO_METERS = {
    0: 1.0, 1: 0.0254, 2: 0.3048, 3: 1609.344,
    4: 0.001, 5: 0.01, 6: 1.0, 7: 1000.0,
    8: 0.0000254, 9: 0.001, 10: 0.9144,
    11: 1.0e-10, 12: 1.0e-9, 13: 1.0e-6,
    14: 0.01, 15: 10.0, 16: 100.0,
    17: 1.0e15, 18: 1.496e11, 19: 9.461e15, 20: 3.086e16,
}


def resolve_color(entity, doc):
    color_index = entity.dxf.get("color", 256)

    if color_index == 256:
        layer_name = entity.dxf.get("layer", "0")
        try:
            layer = doc.layers.get(layer_name)
            color_index = layer.color
        except Exception:
            color_index = 7

    if color_index == 0:
        color_index = 7

    if entity.dxf.hasattr("true_color"):
        tc = entity.dxf.true_color
        return ((tc >> 16) & 0xFF, (tc >> 8) & 0xFF, tc & 0xFF)

    try:
        from ezdxf.colors import aci2rgb
        return aci2rgb(color_index)
    except (ImportError, IndexError):
        pass

    return ACI_COLORS.get(color_index, (200, 200, 200))


def create_mesh_object(name, vertices, faces, color=None):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)

    if color:
        mat = get_or_create_material(*color)
        obj.data.materials.append(mat)

    return obj


def tessellate_arc_points(center, radius, start_angle, end_angle, segments=32):
    if end_angle < start_angle:
        end_angle += 360.0

    points = []
    for i in range(segments + 1):
        t = i / segments
        angle = math.radians(start_angle + t * (end_angle - start_angle))
        x = center[0] + radius * math.cos(angle)
        y = center[1] + radius * math.sin(angle)
        z = center[2] if len(center) > 2 else 0.0
        points.append((x, y, z))
    return points


def line_to_ribbon(p1, p2, width=0.01):
    p1 = Vector(p1)
    p2 = Vector(p2)
    direction = p2 - p1
    length = direction.length
    if length < 1e-10:
        return [], []

    direction.normalize()
    up = Vector((0, 0, 1))
    if abs(direction.dot(up)) > 0.99:
        up = Vector((0, 1, 0))
    perp = direction.cross(up)
    if perp.length < 1e-10:
        return [], []
    perp.normalize()
    perp *= width * 0.5

    v0 = p1 - perp
    v1 = p1 + perp
    v2 = p2 + perp
    v3 = p2 - perp

    vertices = [tuple(v0), tuple(v1), tuple(v2), tuple(v3)]
    faces = [(0, 1, 2), (0, 2, 3)]
    return vertices, faces


def convert_dxf(filepath, output_path, analyze_only=False):
    import ezdxf
    doc = ezdxf.readfile(str(filepath))
    msp = doc.modelspace()

    entity_counts = {}
    for entity in msp:
        etype = entity.dxftype()
        entity_counts[etype] = entity_counts.get(etype, 0) + 1

    if analyze_only:
        issues = []
        solid_count = entity_counts.get("3DSOLID", 0)
        if solid_count > 0:
            convertible_types = {"3DFACE", "MESH", "POLYFACE", "POLYMESH",
                                 "LINE", "LWPOLYLINE", "POLYLINE", "ARC",
                                 "CIRCLE", "SPLINE", "INSERT"}
            has_convertible = any(t in entity_counts for t in convertible_types)
            if not has_convertible:
                issues.append(
                    f"File contains ONLY 3DSOLID entities ({solid_count} total). "
                    "These use proprietary ACIS format."
                )
            else:
                issues.append(
                    f"Found {solid_count} 3DSOLID entities (will be skipped). "
                    "Other geometry types are present and will be converted."
                )
        result = {
            "entity_counts": entity_counts,
            "issues": issues,
            "total_entities": sum(entity_counts.values()),
        }
        print(f"RESULT:{json.dumps(result)}")
        return

    try:
        units = doc.header.get("$INSUNITS", 0)
    except Exception:
        units = 0
    scale = UNIT_TO_METERS.get(units, 1.0)
    if units != 0:
        print(f"INFO: DXF units: {units} (scale to meters: {scale})")
    else:
        print("WARNING: No $INSUNITS set in DXF. Assuming meters.")

    converted = {}
    skipped = {}
    obj_counter = [0]

    def record(d, etype):
        d[etype] = d.get(etype, 0) + 1

    def next_name(prefix):
        obj_counter[0] += 1
        return f"{prefix}_{obj_counter[0]}"

    def process_entity(entity, transform=None):
        etype = entity.dxftype()
        color = resolve_color(entity, doc)

        if etype == "3DFACE":
            points = [
                (entity.dxf.vtx0[0], entity.dxf.vtx0[1], entity.dxf.vtx0[2]),
                (entity.dxf.vtx1[0], entity.dxf.vtx1[1], entity.dxf.vtx1[2]),
                (entity.dxf.vtx2[0], entity.dxf.vtx2[1], entity.dxf.vtx2[2]),
            ]
            if entity.dxf.vtx3 != entity.dxf.vtx2:
                points.append(
                    (entity.dxf.vtx3[0], entity.dxf.vtx3[1], entity.dxf.vtx3[2])
                )
            if len(points) == 3:
                faces = [(0, 1, 2)]
            else:
                faces = [(0, 1, 2), (0, 2, 3)]

            obj = create_mesh_object(next_name("3dface"), points, faces, color)
            if transform is not None:
                obj.matrix_world = transform
            record(converted, etype)

        elif etype == "MESH":
            try:
                vertices = [tuple(v) for v in entity.vertices]
                tri_faces = []
                for face in entity.faces:
                    idxs = list(face)
                    if len(idxs) == 3:
                        tri_faces.append(tuple(idxs))
                    elif len(idxs) == 4:
                        tri_faces.append((idxs[0], idxs[1], idxs[2]))
                        tri_faces.append((idxs[0], idxs[2], idxs[3]))
                    elif len(idxs) > 4:
                        for i in range(1, len(idxs) - 1):
                            tri_faces.append((idxs[0], idxs[i], idxs[i + 1]))
                if tri_faces:
                    obj = create_mesh_object(next_name("mesh"), vertices, tri_faces, color)
                    if transform is not None:
                        obj.matrix_world = transform
                record(converted, etype)
            except Exception as e:
                print(f"WARNING: Failed to convert MESH: {e}")
                record(skipped, etype)

        elif etype in ("LINE", "LWPOLYLINE", "POLYLINE", "SPLINE"):
            points = []
            try:
                if etype == "LINE":
                    start = entity.dxf.start
                    end = entity.dxf.end
                    points = [
                        (start[0], start[1], start[2] if len(start) > 2 else 0),
                        (end[0], end[1], end[2] if len(end) > 2 else 0),
                    ]
                elif etype == "LWPOLYLINE":
                    for x, y, *rest in entity.get_points(format="xyb"):
                        points.append((x, y, entity.dxf.get("elevation", 0)))
                    if entity.closed and len(points) > 1:
                        points.append(points[0])
                elif etype == "POLYLINE":
                    for v in entity.vertices:
                        loc = v.dxf.location
                        points.append((loc[0], loc[1], loc[2]))
                    if entity.is_closed:
                        points.append(points[0])
                elif etype == "SPLINE":
                    ctrl = list(entity.control_points)
                    if ctrl:
                        points = [(p[0], p[1], p[2] if len(p) > 2 else 0) for p in ctrl]
            except Exception as e:
                print(f"WARNING: Failed to extract points from {etype}: {e}")
                record(skipped, etype)
                return

            if len(points) >= 2:
                all_verts = []
                all_faces = []
                offset = 0
                for i in range(len(points) - 1):
                    verts, faces = line_to_ribbon(points[i], points[i + 1])
                    if verts:
                        all_verts.extend(verts)
                        all_faces.extend([(f[0] + offset, f[1] + offset, f[2] + offset) for f in faces])
                        offset += len(verts)
                if all_verts:
                    obj = create_mesh_object(next_name(etype.lower()), all_verts, all_faces, color)
                    if transform is not None:
                        obj.matrix_world = transform
                record(converted, etype)
            else:
                record(skipped, etype)

        elif etype in ("ARC", "CIRCLE"):
            try:
                center = entity.dxf.center
                radius = entity.dxf.radius
                ctr = (center[0], center[1], center[2] if len(center) > 2 else 0)
                if etype == "CIRCLE":
                    pts = tessellate_arc_points(ctr, radius, 0, 360, segments=64)
                else:
                    pts = tessellate_arc_points(
                        ctr, radius,
                        entity.dxf.start_angle,
                        entity.dxf.end_angle,
                        segments=32,
                    )
                all_verts = []
                all_faces = []
                offset = 0
                for i in range(len(pts) - 1):
                    verts, faces = line_to_ribbon(pts[i], pts[i + 1])
                    if verts:
                        all_verts.extend(verts)
                        all_faces.extend([(f[0] + offset, f[1] + offset, f[2] + offset) for f in faces])
                        offset += len(verts)
                if all_verts:
                    obj = create_mesh_object(next_name(etype.lower()), all_verts, all_faces, color)
                    if transform is not None:
                        obj.matrix_world = transform
                record(converted, etype)
            except Exception as e:
                print(f"WARNING: Failed to convert {etype}: {e}")
                record(skipped, etype)

        elif etype == "INSERT":
            block_name = entity.dxf.name
            try:
                block = doc.blocks.get(block_name)
            except Exception:
                print(f"WARNING: Block '{block_name}' not found")
                record(skipped, etype)
                return

            insert_point = entity.dxf.get("insert", (0, 0, 0))
            x_scale = entity.dxf.get("xscale", 1.0)
            y_scale = entity.dxf.get("yscale", 1.0)
            z_scale = entity.dxf.get("zscale", 1.0)
            rotation = math.radians(entity.dxf.get("rotation", 0.0))

            cos_r = math.cos(rotation)
            sin_r = math.sin(rotation)

            block_transform = Matrix((
                (x_scale * cos_r, -y_scale * sin_r, 0, insert_point[0]),
                (x_scale * sin_r,  y_scale * cos_r, 0, insert_point[1]),
                (0,                0,                z_scale, insert_point[2] if len(insert_point) > 2 else 0),
                (0,                0,                0, 1),
            ))

            if transform is not None:
                block_transform = transform @ block_transform

            for sub_entity in block:
                process_entity(sub_entity, block_transform)
            record(converted, etype)

        elif etype == "3DSOLID":
            try:
                import importlib.util
                tess_path = Path(__file__).parent.parent / "trimesh" / "acis_tessellator.py"
                spec = importlib.util.spec_from_file_location("acis_tessellator", tess_path)
                acis_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(acis_mod)
                sab_data = entity.sab
                if sab_data:
                    tri_mesh = acis_mod.tessellate_solid(sab_data)
                    if tri_mesh is not None and len(tri_mesh.faces) > 0:
                        verts = [tuple(v) for v in tri_mesh.vertices]
                        faces = [tuple(f) for f in tri_mesh.faces]
                        obj = create_mesh_object(next_name("3dsolid"), verts, faces, color)
                        if transform is not None:
                            obj.matrix_world = transform
                        record(converted, etype)
                        return
            except Exception as e:
                print(f"WARNING: 3DSOLID tessellation failed: {e}")
            record(skipped, etype)

        else:
            record(skipped, etype)

    for entity in msp:
        process_entity(entity)

    if not converted:
        print(f"ERROR: No convertible geometry in {filepath.name}")
        result = {
            "success": False,
            "converted": converted,
            "skipped": skipped,
        }
        print(f"RESULT:{json.dumps(result)}")
        return

    # Apply unit scaling
    if scale != 1.0:
        for obj in bpy.data.objects:
            if obj.type == "MESH":
                obj.scale = (scale, scale, scale)
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.transform_apply(scale=True)

    # Join all mesh objects into one
    mesh_objects = [obj for obj in bpy.data.objects if obj.type == "MESH"]
    if mesh_objects:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in mesh_objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = mesh_objects[0]
        if len(mesh_objects) > 1:
            bpy.ops.object.join()

        combined = bpy.context.active_object

        # Center if coordinates are large
        bbox_center = sum((Vector(b) for b in combined.bound_box), Vector()) / 8
        world_center = combined.matrix_world @ bbox_center
        if any(abs(c) > 10000 for c in world_center):
            print(f"INFO: Large coordinates detected (center: {tuple(world_center)}). Centering model.")
            combined.location -= world_center

        total_verts = len(combined.data.vertices)
        total_faces = len(combined.data.polygons)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.export_scene.gltf(
            filepath=str(output_path),
            export_format="GLB",
            use_selection=True,
            export_apply=True,
        )

        result = {
            "success": True,
            "converted": converted,
            "skipped": skipped,
            "vertex_count": total_verts,
            "face_count": total_faces,
            "output": str(output_path),
        }
        print(f"RESULT:{json.dumps(result)}")
    else:
        result = {"success": False, "converted": converted, "skipped": skipped}
        print(f"RESULT:{json.dumps(result)}")


def convert_stl(filepath, output_path):
    bpy.ops.wm.stl_import(filepath=str(filepath))

    mesh_objects = [obj for obj in bpy.data.objects if obj.type == "MESH"]
    if not mesh_objects:
        print(f"ERROR: No geometry in STL file: {filepath.name}")
        print(f'RESULT:{{"success": false, "vertex_count": 0, "face_count": 0}}')
        return

    bpy.ops.object.select_all(action="SELECT")
    bpy.context.view_layer.objects.active = mesh_objects[0]
    if len(mesh_objects) > 1:
        bpy.ops.object.join()

    combined = bpy.context.active_object
    bbox_center = sum((Vector(b) for b in combined.bound_box), Vector()) / 8
    world_center = combined.matrix_world @ bbox_center
    if any(abs(c) > 10000 for c in world_center):
        print(f"INFO: Large coordinates detected. Centering model.")
        combined.location -= world_center

    total_verts = len(combined.data.vertices)
    total_faces = len(combined.data.polygons)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.gltf(
        filepath=str(output_path),
        export_format="GLB",
        use_selection=True,
        export_apply=True,
    )

    result = {
        "success": True,
        "vertex_count": total_verts,
        "face_count": total_faces,
        "output": str(output_path),
    }
    print(f"RESULT:{json.dumps(result)}")


def convert_obj(filepath, output_path):
    bpy.ops.wm.obj_import(filepath=str(filepath))

    mesh_objects = [obj for obj in bpy.data.objects if obj.type == "MESH"]
    if not mesh_objects:
        print(f"ERROR: No geometry in OBJ file: {filepath.name}")
        print(f'RESULT:{{"success": false, "vertex_count": 0, "face_count": 0}}')
        return

    bpy.ops.object.select_all(action="SELECT")
    bpy.context.view_layer.objects.active = mesh_objects[0]
    if len(mesh_objects) > 1:
        bpy.ops.object.join()

    combined = bpy.context.active_object
    bbox_center = sum((Vector(b) for b in combined.bound_box), Vector()) / 8
    world_center = combined.matrix_world @ bbox_center
    if any(abs(c) > 10000 for c in world_center):
        print(f"INFO: Large coordinates detected. Centering model.")
        combined.location -= world_center

    total_verts = len(combined.data.vertices)
    total_faces = len(combined.data.polygons)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.gltf(
        filepath=str(output_path),
        export_format="GLB",
        use_selection=True,
        export_apply=True,
    )

    result = {
        "success": True,
        "vertex_count": total_verts,
        "face_count": total_faces,
        "output": str(output_path),
    }
    print(f"RESULT:{json.dumps(result)}")


def main():
    argv = sys.argv
    if "--" not in argv:
        print("ERROR: No arguments passed after --")
        print('RESULT:{"success": false, "error": "No arguments"}')
        return

    args_json = argv[argv.index("--") + 1]
    args = json.loads(args_json)

    input_path = Path(args["input"])
    output_path = Path(args["output"])
    analyze_only = args.get("analyze", False)

    clear_scene()

    ext = input_path.suffix.lower()
    if ext == ".dxf":
        convert_dxf(input_path, output_path, analyze_only)
    elif ext == ".stl":
        convert_stl(input_path, output_path)
    elif ext == ".obj":
        convert_obj(input_path, output_path)
    else:
        print(f"ERROR: Unsupported format: {ext}")
        print(f'RESULT:{{"success": false, "error": "Unsupported format: {ext}"}}')


if __name__ == "__main__":
    main()
