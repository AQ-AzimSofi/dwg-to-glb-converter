# DWG to GLB Conversion Report

**Date:** 2026-03-12
**Customer data:** (redacted)
**Method:** Autodesk Platform Services (APS) API

## Input Files

| File | Size | 3DSOLID | Other entities |
|------|------|---------|----------------|
| sample_manhole.dwg | 462 KB | 7 | None |
| sample_launch_shaft.dwg | 17.8 MB | 1,621 | POLYLINE: 1,765 / LWPOLYLINE: 185 / INSERT: 52 |

## Conversion Results

### sample_manhole.dwg -- SUCCESS

- Meshes: 11
- Vertices: 540
- Faces: 464
- Output: `output_glb_aps/manhole/output.glb` (31 KB)

### sample_launch_shaft.dwg -- SUCCESS

- Meshpacks: 14
- Output: `output_glb_aps/launch_shaft/output.glb` (16 MB)
- Note: Required `gltf-transform copy` to fix ACCESSOR_TOO_LONG validation error from forge-convert-utils

## Summary

| File | 3DSOLID entities | GLB produced | GLB size |
|------|------------------|--------------|----------|
| Manhole | 7 | Yes | 31 KB |
| Launch shaft | 1,621 | Yes | 16 MB |

Both files converted successfully with all 3DSOLID geometry intact. The APS API tessellated all ACIS solid data because Autodesk's server-side engine has native ACIS support.

## Open-Source Attempts (All Failed)

Three open-source methods were tested before the APS approach. All failed because they lack the proprietary ACIS geometry kernel:

| Method | Result | Reason |
|--------|--------|--------|
| ezdxf + trimesh | No GLB | Cannot tessellate ACIS B-rep data |
| Blender 4.2 | No GLB | DXF import addon removed |
| FreeCAD + ODA | No GLB | OpenCASCADE does not support ACIS format |

See `report_dwg_to_glb.md` for full details on each method.
