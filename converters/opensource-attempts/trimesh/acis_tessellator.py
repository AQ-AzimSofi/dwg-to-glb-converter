"""
ACIS SAB tessellator for 3DSOLID entities.

Parses ezdxf's SAB text dump and tessellates the B-rep geometry into
triangle meshes. Handles:
  - cone-surface (cylinders, truncated cones)
  - plane-surface with straight-curve boundaries (boxes, extrusions, polygonal solids)
  - plane-surface with ellipse-curve boundaries (circular caps)

This module is used by convert.py when 3DSOLID entities are encountered.
"""

import logging

import numpy as np
import trimesh
from ezdxf.acis import api as acis_api

logger = logging.getLogger("dxf2glb")


def parse_records(text_lines: list[str]) -> list[dict]:
    """Parse SAB text dump into a list of record dicts."""
    records = []
    current = None

    for line in text_lines:
        line = line.rstrip()
        if "record:" in line and line.lstrip().startswith("-"):
            if current is not None:
                records.append(current)
            current = {"fields": []}
            continue

        if current is None:
            continue

        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if key == "ENTITY_TYPE":
            current["type"] = value
        elif key == "POINTER":
            current["fields"].append(("ptr", int(value)))
        elif key == "INT":
            current["fields"].append(("int", int(value)))
        elif key == "DOUBLE":
            current["fields"].append(("double", float(value)))
        elif key == "LOCATION_VEC":
            current["fields"].append(("location", _parse_vec(value)))
        elif key == "DIRECTION_VEC":
            current["fields"].append(("direction", _parse_vec(value)))
        elif key == "STR":
            current["fields"].append(("str", value))
        elif key in ("BOOL_TRUE", "BOOL_FALSE"):
            current["fields"].append(("bool", value == "True"))

    if current is not None:
        records.append(current)

    return records


def _parse_vec(s: str) -> tuple[float, float, float]:
    s = s.strip("()")
    parts = [float(x.strip()) for x in s.split(",")]
    return (parts[0], parts[1], parts[2])


def _get_fields(rec: dict, field_type: str) -> list:
    return [f[1] for f in rec["fields"] if f[0] == field_type]


def _get_ptrs(rec: dict) -> list[int]:
    return [f[1] for f in rec["fields"] if f[0] == "ptr"]


def get_transform_matrix(records: list[dict]) -> np.ndarray:
    for rec in records:
        if rec.get("type") == "transform":
            dirs = _get_fields(rec, "direction")
            doubles = _get_fields(rec, "double")

            if len(dirs) >= 4:
                rot = np.array([dirs[0], dirs[1], dirs[2]], dtype=np.float64)
                trans = np.array(dirs[3], dtype=np.float64)
                scale = doubles[0] if doubles else 1.0

                mat = np.eye(4, dtype=np.float64)
                mat[:3, :3] = rot.T * scale
                mat[:3, 3] = trans
                return mat

    return np.eye(4, dtype=np.float64)


def _triangulate_polygon_3d(vertices_3d: list[np.ndarray]) -> list[list[int]]:
    """Triangulate a 3D polygon by projecting to 2D and using ear-clipping."""
    n = len(vertices_3d)
    if n < 3:
        return []
    if n == 3:
        return [[0, 1, 2]]
    if n == 4:
        return [[0, 1, 2], [0, 2, 3]]

    pts = np.array(vertices_3d, dtype=np.float64)

    normal = np.zeros(3)
    for i in range(n):
        j = (i + 1) % n
        normal[0] += (pts[i][1] - pts[j][1]) * (pts[i][2] + pts[j][2])
        normal[1] += (pts[i][2] - pts[j][2]) * (pts[i][0] + pts[j][0])
        normal[2] += (pts[i][0] - pts[j][0]) * (pts[i][1] + pts[j][1])

    norm_len = np.linalg.norm(normal)
    if norm_len < 1e-12:
        return [[0, i, i + 1] for i in range(1, n - 1)]
    normal /= norm_len

    if abs(normal[2]) > 0.9:
        u_axis = np.array([1, 0, 0], dtype=np.float64)
    else:
        u_axis = np.cross(normal, [0, 0, 1])
        u_axis /= np.linalg.norm(u_axis)
    v_axis = np.cross(normal, u_axis)
    v_axis /= np.linalg.norm(v_axis)

    pts_2d = np.column_stack([pts @ u_axis, pts @ v_axis])

    return _ear_clip(pts_2d)


def _cross_2d(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _point_in_triangle_2d(p, a, b, c):
    d1 = _cross_2d(p, a, b)
    d2 = _cross_2d(p, b, c)
    d3 = _cross_2d(p, c, a)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def _ear_clip(pts_2d: np.ndarray) -> list[list[int]]:
    """Ear-clipping triangulation for a simple polygon in 2D."""
    n = len(pts_2d)
    if n < 3:
        return []

    indices = list(range(n))
    triangles = []

    # Ensure CCW winding
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += pts_2d[i][0] * pts_2d[j][1]
        area -= pts_2d[j][0] * pts_2d[i][1]
    if area < 0:
        indices.reverse()

    max_iter = n * n
    iteration = 0

    while len(indices) > 2 and iteration < max_iter:
        iteration += 1
        ear_found = False

        for i in range(len(indices)):
            prev_i = (i - 1) % len(indices)
            next_i = (i + 1) % len(indices)

            a = pts_2d[indices[prev_i]]
            b = pts_2d[indices[i]]
            c = pts_2d[indices[next_i]]

            # Check if this is a convex vertex
            cross = _cross_2d(a, b, c)
            if cross <= 1e-12:
                continue

            # Check if any other vertex is inside this triangle
            is_ear = True
            for j in range(len(indices)):
                if j == prev_i or j == i or j == next_i:
                    continue
                if _point_in_triangle_2d(pts_2d[indices[j]], a, b, c):
                    is_ear = False
                    break

            if is_ear:
                triangles.append([indices[prev_i], indices[i], indices[next_i]])
                indices.pop(i)
                ear_found = True
                break

        if not ear_found:
            # Fallback: fan triangulation from first vertex
            for i in range(1, len(indices) - 1):
                triangles.append([indices[0], indices[i], indices[i + 1]])
            break

    return triangles


def _triangulate_with_holes(outer_pts, hole_rings):
    """Triangulate a 3D polygon with holes using mapbox_earcut."""
    from mapbox_earcut import triangulate_float64

    all_pts_3d = list(outer_pts)
    n = len(outer_pts)

    pts = np.array(all_pts_3d, dtype=np.float64)
    normal = np.zeros(3)
    for i in range(n):
        j = (i + 1) % n
        normal[0] += (pts[i][1] - pts[j][1]) * (pts[i][2] + pts[j][2])
        normal[1] += (pts[i][2] - pts[j][2]) * (pts[i][0] + pts[j][0])
        normal[2] += (pts[i][0] - pts[j][0]) * (pts[i][1] + pts[j][1])
    norm_len = np.linalg.norm(normal)
    if norm_len < 1e-12:
        return None
    normal /= norm_len

    if abs(normal[2]) > 0.9:
        u_axis = np.array([1, 0, 0], dtype=np.float64)
    else:
        u_axis = np.cross(normal, [0, 0, 1])
        u_axis /= np.linalg.norm(u_axis)
    v_axis = np.cross(normal, u_axis)
    v_axis /= np.linalg.norm(v_axis)

    ring_ends = [len(outer_pts)]
    for hole in hole_rings:
        all_pts_3d.extend(hole)
        ring_ends.append(len(all_pts_3d))

    all_pts = np.array(all_pts_3d, dtype=np.float64)
    coords_2d = np.column_stack([all_pts @ u_axis, all_pts @ v_axis])

    coords_input = np.ascontiguousarray(coords_2d, dtype=np.float64)
    rings = np.array(ring_ends, dtype=np.uint32)

    tri_indices = triangulate_float64(coords_input, rings)
    if len(tri_indices) == 0:
        return None

    faces = tri_indices.reshape(-1, 3)
    valid = []
    for f in faces:
        if f[0] != f[1] and f[1] != f[2] and f[0] != f[2]:
            if all(idx < len(all_pts) for idx in f):
                valid.append(f)
    if not valid:
        return None

    mesh = trimesh.Trimesh(
        vertices=all_pts,
        faces=np.array(valid, dtype=np.int64),
    )
    return mesh if len(mesh.faces) > 0 else None


def tessellate_cylinder(center, axis, radius, z_min, z_max, segments=32):
    center = np.array(center, dtype=np.float64)
    axis = np.array(axis, dtype=np.float64)
    axis_len = np.linalg.norm(axis)
    if axis_len < 1e-12:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    axis = axis / axis_len

    if abs(np.dot(axis, [0, 0, 1])) > 0.99:
        perp1 = np.cross(axis, [0, 1, 0])
    else:
        perp1 = np.cross(axis, [0, 0, 1])
    perp1 = perp1 / np.linalg.norm(perp1)
    perp2 = np.cross(axis, perp1)
    perp2 = perp2 / np.linalg.norm(perp2)

    angles = np.linspace(0, 2 * np.pi, segments + 1)[:-1]
    vertices = []
    faces = []

    for a in angles:
        vertices.append(center + axis * z_min + radius * (np.cos(a) * perp1 + np.sin(a) * perp2))
    for a in angles:
        vertices.append(center + axis * z_max + radius * (np.cos(a) * perp1 + np.sin(a) * perp2))

    n = segments
    for i in range(n):
        i_next = (i + 1) % n
        faces.append([i, i_next, n + i_next])
        faces.append([i, n + i_next, n + i])

    bc = len(vertices)
    vertices.append(center + axis * z_min)
    for i in range(n):
        faces.append([bc, (i + 1) % n, i])

    tc = len(vertices)
    vertices.append(center + axis * z_max)
    for i in range(n):
        faces.append([tc, n + i, n + (i + 1) % n])

    return np.array(vertices, dtype=np.float64), np.array(faces, dtype=np.int64)


def tessellate_cone_frustum(center, axis, radius_bottom, radius_top, z_min, z_max, segments=32):
    center = np.array(center, dtype=np.float64)
    axis = np.array(axis, dtype=np.float64)
    axis_len = np.linalg.norm(axis)
    if axis_len < 1e-12:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    axis = axis / axis_len

    if abs(np.dot(axis, [0, 0, 1])) > 0.99:
        perp1 = np.cross(axis, [0, 1, 0])
    else:
        perp1 = np.cross(axis, [0, 0, 1])
    perp1 = perp1 / np.linalg.norm(perp1)
    perp2 = np.cross(axis, perp1)
    perp2 = perp2 / np.linalg.norm(perp2)

    angles = np.linspace(0, 2 * np.pi, segments + 1)[:-1]
    vertices = []
    faces = []

    for a in angles:
        vertices.append(center + axis * z_min + radius_bottom * (np.cos(a) * perp1 + np.sin(a) * perp2))
    for a in angles:
        vertices.append(center + axis * z_max + radius_top * (np.cos(a) * perp1 + np.sin(a) * perp2))

    n = segments
    for i in range(n):
        i_next = (i + 1) % n
        faces.append([i, i_next, n + i_next])
        faces.append([i, n + i_next, n + i])

    if radius_bottom > 1e-10:
        bc = len(vertices)
        vertices.append(center + axis * z_min)
        for i in range(n):
            faces.append([bc, (i + 1) % n, i])

    if radius_top > 1e-10:
        tc = len(vertices)
        vertices.append(center + axis * z_max)
        for i in range(n):
            faces.append([tc, n + i, n + (i + 1) % n])

    return np.array(vertices, dtype=np.float64), np.array(faces, dtype=np.int64)


def _tessellate_ellipse_loop(records, rec_map, loop_id, loop_coedge, coedge_info, segments=32):
    """Tessellate ellipse-curve edges in a loop into discrete points."""
    if loop_id not in loop_coedge:
        return []

    first_ce = loop_coedge[loop_id]
    visited = set()
    cur = first_ce
    ellipse_curves = []

    while cur and cur not in visited:
        visited.add(cur)
        if cur not in coedge_info:
            break
        ci = coedge_info[cur]
        edge_id = ci["edge"]
        edge_rec = rec_map.get(edge_id)
        if edge_rec:
            for p in _get_ptrs(edge_rec):
                if p >= 0 and p in rec_map and rec_map[p].get("type") == "ellipse-curve":
                    curve = rec_map[p]
                    locs = _get_fields(curve, "location")
                    dirs = _get_fields(curve, "direction")
                    doubles = _get_fields(curve, "double")
                    if locs and len(dirs) >= 2:
                        ellipse_curves.append({
                            "center": np.array(locs[0]),
                            "axis": np.array(dirs[0]),
                            "major": np.array(dirs[1]),
                            "ratio": doubles[0] if doubles else 1.0,
                        })
        cur = ci["next"]
        if cur == first_ce:
            break

    if not ellipse_curves:
        return []

    ec = ellipse_curves[0]
    center = ec["center"]
    axis = ec["axis"]
    major = ec["major"]
    radius = np.linalg.norm(major)
    ratio = ec["ratio"]

    axis_len = np.linalg.norm(axis)
    if axis_len < 1e-12:
        return []
    axis_n = axis / axis_len

    major_n = major / radius if radius > 1e-12 else np.array([1, 0, 0])
    minor_n = np.cross(axis_n, major_n)
    minor_len = np.linalg.norm(minor_n)
    if minor_len < 1e-12:
        return []
    minor_n /= minor_len

    angles = np.linspace(0, 2 * np.pi, segments + 1)[:-1]
    points = []
    for a in angles:
        pt = center + radius * np.cos(a) * major_n + radius * ratio * np.sin(a) * minor_n
        points.append(pt)
    return points


def _build_brep_faces(records: list[dict]) -> list[dict]:
    """
    Trace the B-rep topology: face -> loop -> coedge -> edge -> vertex -> point.
    Returns a list of face dicts with 'outer' polygon and optional 'holes' list.
    Each polygon is a list of 3D vertex positions.
    """
    rec_map = {}
    for i, rec in enumerate(records):
        rec_map[i] = rec

    point_coords = {}
    for i, rec in enumerate(records):
        if rec.get("type") == "point":
            locs = _get_fields(rec, "location")
            if locs:
                point_coords[i] = np.array(locs[0], dtype=np.float64)

    vertex_point = {}
    for i, rec in enumerate(records):
        if rec.get("type") in ("vertex", "tvertex-vertex"):
            ptrs = _get_ptrs(rec)
            for p in reversed(ptrs):
                if p >= 0 and p in rec_map and rec_map[p].get("type") == "point":
                    vertex_point[i] = p
                    break

    edge_vertices = {}
    for i, rec in enumerate(records):
        if rec.get("type") == "edge":
            ptrs = _get_ptrs(rec)
            v_start = None
            v_end = None
            for p in ptrs:
                if p >= 0 and p in rec_map and rec_map[p].get("type") in ("vertex", "tvertex-vertex"):
                    if v_start is None:
                        v_start = p
                    else:
                        v_end = p
                        break
            if v_start is not None and v_end is not None:
                edge_vertices[i] = (v_start, v_end)
            elif v_start is not None:
                edge_vertices[i] = (v_start, v_start)

    coedge_info = {}
    for i, rec in enumerate(records):
        if rec.get("type") == "coedge":
            ptrs = _get_ptrs(rec)
            bools = _get_fields(rec, "bool")
            edge_ptr = None
            next_ptr = None
            sense = False

            for p in ptrs:
                if p >= 0 and p in rec_map and rec_map[p].get("type") == "edge":
                    edge_ptr = p
                    break

            coedge_ptrs = []
            for p in ptrs:
                if p >= 0 and p in rec_map and rec_map[p].get("type") == "coedge":
                    coedge_ptrs.append(p)
            next_ptr = coedge_ptrs[0] if coedge_ptrs else None
            sense = bools[0] if bools else False

            if edge_ptr is not None:
                coedge_info[i] = {
                    "edge": edge_ptr,
                    "next": next_ptr,
                    "sense": sense,
                }

    loop_coedge = {}
    for i, rec in enumerate(records):
        if rec.get("type") == "loop":
            ptrs = _get_ptrs(rec)
            for p in ptrs:
                if p >= 0 and p in rec_map and rec_map[p].get("type") == "coedge":
                    loop_coedge[i] = p
                    break

    def _get_loop_curve_types(loop_id):
        if loop_id not in loop_coedge:
            return set()
        first_ce = loop_coedge[loop_id]
        visited = set()
        cur = first_ce
        curve_types = set()
        while cur and cur not in visited:
            visited.add(cur)
            if cur not in coedge_info:
                break
            ci = coedge_info[cur]
            edge_rec = rec_map.get(ci["edge"])
            if edge_rec:
                for p in _get_ptrs(edge_rec):
                    if p >= 0 and p in rec_map and "curve" in rec_map[p].get("type", ""):
                        curve_types.add(rec_map[p]["type"])
            cur = ci["next"]
            if cur == first_ce:
                break
        return curve_types

    def _trace_loop_vertices(loop_id):
        if loop_id not in loop_coedge:
            return []
        first_coedge_id = loop_coedge[loop_id]
        polygon_pts = []
        visited = set()
        current_coedge_id = first_coedge_id

        while current_coedge_id is not None and current_coedge_id not in visited:
            visited.add(current_coedge_id)
            if current_coedge_id not in coedge_info:
                break
            ci = coedge_info[current_coedge_id]
            edge_id = ci["edge"]
            sense = ci["sense"]

            if edge_id in edge_vertices:
                v_start, v_end = edge_vertices[edge_id]
                v_use = v_start if not sense else v_end
                if v_use in vertex_point and vertex_point[v_use] in point_coords:
                    pt = point_coords[vertex_point[v_use]]
                    if len(polygon_pts) == 0 or np.linalg.norm(polygon_pts[-1] - pt) > 1e-6:
                        polygon_pts.append(pt)

            current_coedge_id = ci["next"]
            if current_coedge_id == first_coedge_id:
                break

        if len(polygon_pts) >= 3:
            if np.linalg.norm(polygon_pts[-1] - polygon_pts[0]) < 1e-6:
                polygon_pts = polygon_pts[:-1]
        return polygon_pts if len(polygon_pts) >= 3 else []

    # Collect all loops for each face (faces can have multiple loops via chaining)
    face_info = {}
    for i, rec in enumerate(records):
        if rec.get("type") == "face":
            ptrs = _get_ptrs(rec)
            loops = []
            surface_ptr = None
            for p in ptrs:
                if p >= 0 and p in rec_map:
                    rtype = rec_map[p].get("type", "")
                    if rtype == "loop":
                        loops.append(p)
                        # Check for chained loops
                        loop_ptrs = _get_ptrs(rec_map[p])
                        for lp in loop_ptrs:
                            if lp >= 0 and lp in rec_map and rec_map[lp].get("type") == "loop" and lp not in loops:
                                loops.append(lp)
                    elif "surface" in rtype:
                        surface_ptr = p
            if loops:
                face_info[i] = {"loops": loops, "surface": surface_ptr}

    polygon_faces = []
    for face_id, finfo in face_info.items():
        surface_type = rec_map[finfo["surface"]].get("type", "") if finfo["surface"] else ""
        if "plane" not in surface_type:
            continue

        loops = finfo["loops"]
        outer_pts = None
        hole_rings = []

        ellipse_rings = []

        for loop_id in loops:
            curve_types = _get_loop_curve_types(loop_id)

            if "ellipse-curve" in curve_types and "straight-curve" not in curve_types:
                pts = _tessellate_ellipse_loop(records, rec_map, loop_id, loop_coedge, coedge_info)
                if pts:
                    ellipse_rings.append(pts)
            else:
                pts = _trace_loop_vertices(loop_id)
                if pts:
                    if outer_pts is None:
                        outer_pts = pts
                    else:
                        hole_rings.append(pts)

        if outer_pts is None and ellipse_rings:
            # All loops are ellipse-curve: largest is outer, rest are holes
            def _ring_area(pts):
                n = len(pts)
                a = 0.0
                for i in range(n):
                    j = (i + 1) % n
                    a += pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1]
                return abs(a) / 2.0

            ellipse_rings.sort(key=_ring_area, reverse=True)
            outer_pts = ellipse_rings[0]
            hole_rings.extend(ellipse_rings[1:])
        else:
            hole_rings.extend(ellipse_rings)

        if outer_pts:
            polygon_faces.append({"outer": outer_pts, "holes": hole_rings})

    return polygon_faces


def tessellate_solid(sab_data: bytes, segments: int = 32) -> trimesh.Trimesh | None:
    """
    Tessellate a single 3DSOLID's SAB data into a triangle mesh.

    Strategy:
    1. If cone-surfaces exist, tessellate them as cylinders/cones with caps.
    2. For plane-surface + straight-curve solids, trace the B-rep topology
       to extract polygon faces and triangulate them.
    3. Apply the ACIS transform matrix.
    """
    try:
        text_lines = list(acis_api.dump_sab_as_text(sab_data))
    except Exception:
        return None

    records = parse_records(text_lines)
    if not records:
        return None

    transform = get_transform_matrix(records)
    all_meshes = []

    # Check what surface types exist
    has_cone = any(r.get("type") == "cone-surface" for r in records)
    has_plane = any(r.get("type") == "plane-surface" for r in records)
    has_ellipse = any(r.get("type") == "ellipse-curve" for r in records)

    if has_cone:
        # Collect cone-surface and ellipse-curve data
        cone_surfaces = {}
        ellipse_curves = {}
        for i, rec in enumerate(records):
            if rec.get("type") == "cone-surface":
                locs = _get_fields(rec, "location")
                dirs = _get_fields(rec, "direction")
                doubles = _get_fields(rec, "double")
                if locs and len(dirs) >= 2:
                    cone_surfaces[i] = {
                        "center": np.array(locs[0]),
                        "axis": np.array(dirs[0]),
                        "ref_radius": np.linalg.norm(np.array(dirs[1])),
                        "ratio": doubles[0] if doubles else 1.0,
                    }
            elif rec.get("type") == "ellipse-curve":
                locs = _get_fields(rec, "location")
                dirs = _get_fields(rec, "direction")
                if locs and len(dirs) >= 2:
                    ellipse_curves[i] = {
                        "center": locs[0],
                        "axis": dirs[0],
                        "radius": np.linalg.norm(np.array(dirs[1])),
                    }

        for cone_id, cone in cone_surfaces.items():
            matching_curves = []
            for ec_id, ec in ellipse_curves.items():
                ec_center = np.array(ec["center"])
                offset = ec_center - cone["center"]
                axis_norm = cone["axis"] / np.linalg.norm(cone["axis"])
                z_val = np.dot(offset, axis_norm)
                matching_curves.append({"z": z_val, "radius": ec["radius"]})

            if len(matching_curves) < 2:
                if matching_curves:
                    mc = matching_curves[0]
                    verts, fcs = tessellate_cylinder(
                        cone["center"].tolist(), cone["axis"].tolist(),
                        mc["radius"], 0, mc["z"], segments,
                    )
                    if len(verts) > 0:
                        all_meshes.append(trimesh.Trimesh(vertices=verts, faces=fcs))
                continue

            matching_curves.sort(key=lambda c: c["z"])
            bottom = matching_curves[0]
            top = matching_curves[-1]

            if abs(bottom["radius"] - top["radius"]) < 1e-6:
                verts, fcs = tessellate_cylinder(
                    cone["center"].tolist(), cone["axis"].tolist(),
                    bottom["radius"], bottom["z"], top["z"], segments,
                )
            else:
                verts, fcs = tessellate_cone_frustum(
                    cone["center"].tolist(), cone["axis"].tolist(),
                    bottom["radius"], top["radius"],
                    bottom["z"], top["z"], segments,
                )
            if len(verts) > 0:
                all_meshes.append(trimesh.Trimesh(vertices=verts, faces=fcs))

    if has_plane:
        polygon_faces = _build_brep_faces(records)

        for face_data in polygon_faces:
            outer = face_data["outer"]
            holes = face_data.get("holes", [])

            if holes:
                mesh = _triangulate_with_holes(outer, holes)
                if mesh is not None:
                    all_meshes.append(mesh)
            else:
                tri_indices = _triangulate_polygon_3d(outer)
                if not tri_indices:
                    continue
                verts = np.array(outer, dtype=np.float64)
                faces = np.array(tri_indices, dtype=np.int64)
                valid = []
                for f in faces:
                    if f[0] != f[1] and f[1] != f[2] and f[0] != f[2]:
                        if all(idx < len(verts) for idx in f):
                            valid.append(f)
                if valid:
                    mesh = trimesh.Trimesh(vertices=verts, faces=np.array(valid, dtype=np.int64))
                    if len(mesh.faces) > 0:
                        all_meshes.append(mesh)

    if not all_meshes:
        return None

    combined = trimesh.util.concatenate(all_meshes)

    if not np.allclose(transform, np.eye(4)):
        combined.apply_transform(transform)

    return combined
