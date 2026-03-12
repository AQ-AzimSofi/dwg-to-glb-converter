# DWG to GLB Conversion: Method Comparison Report

**Date:** 2026-03-12
**Test files:**
- `sample_manhole.dwg` (462 KB, 7 3DSOLID entities)
- `sample_launch_shaft.dwg` (17.8 MB, 1621 3DSOLID + 1765 POLYLINE + 185 LWPOLYLINE + 52 INSERT)

**Core problem:** These DWG files store 3D geometry as `3DSOLID` entities using the proprietary ACIS format (Spatial Corp / Dassault Systemes). No open-source library can decode ACIS geometry.

---

## Method 1: ezdxf + trimesh (Python)

**Script:** `converters/trimesh/convert_pure.py`
**Libraries:** ezdxf (DXF parser), trimesh (mesh export)
**How it works:** ezdxf reads the DXF file and exposes entity data. trimesh packages geometry into GLB.

### Result: FAILED

| File | 3DSOLID | Other entities | GLB produced |
|------|---------|----------------|--------------|
| Manhole | 7 (all skipped) | 0 | No |
| Launch shaft | 1621 (all skipped) | 2002 (all skipped*) | No |

*POLYLINE, LWPOLYLINE, and INSERT entities are 2D annotation/layout data, not 3D mesh geometry.

**Why it fails:** ezdxf can read DXF entity metadata and extract raw ACIS binary data from 3DSOLID, but it has no geometry kernel to tessellate (convert to triangles) the ACIS B-rep data. It explicitly cannot produce mesh output from 3DSOLID entities.

---

## Method 2: Blender (headless, Python/bpy)

**Script:** `converters/blender/convert_pure.py`
**Tool:** Blender 4.2.0 in background mode (`--background --python`)
**How it works:** Blender imports DXF via its built-in addon and exports to GLB.

### Result: FAILED

Blender 4.2 does not include a DXF import addon. The `io_import_dxf` addon was removed.

```
ERROR: Blender has no DXF import capability:
Add-on not loaded: "io_import_dxf", cause: No module named 'io_import_dxf'
```

Even in older Blender versions that included `io_import_dxf` (which used the `dxfgrabber` library), the addon could not handle 3DSOLID entities -- it only supported basic 2D/line entities.

| File | GLB produced |
|------|--------------|
| Manhole | No |
| Launch shaft | No |

---

## Method 3: FreeCAD + ODA FileConverter

**Script:** `converters/freecad/convert_pure.py`
**Tools:** ODA FileConverter (DWG to DXF), FreeCAD 1.0.2 with OpenCASCADE kernel
**How it works:** ODA converts DWG to DXF. FreeCAD's `importDXF` module reads the DXF and uses OpenCASCADE (OCCT) for geometry. Meshes are exported via FreeCAD's Mesh module.

### Result: FAILED

FreeCAD explicitly rejects 3DSOLID entities:

```
Unsupported DXF features:
Entity type '3DSOLID': 7 time(s) first at line 10590
```

OpenCASCADE supports STEP, IGES, and BREP geometry formats, but NOT the ACIS format used inside 3DSOLID entities. There is no way to make FreeCAD read ACIS data without a commercial ACIS license.

| File | 3DSOLID | FreeCAD objects created | GLB produced |
|------|---------|------------------------|--------------|
| Manhole | 7 (all unsupported) | 0 usable | No |
| Launch shaft | 1621 (all unsupported) | 0 usable | No |

---

## Method 4: Autodesk Platform Services (APS) API

**Script:** `converters/aps/convert_aps.py` (upload + translate) + `converters/aps/svf_to_glb.js` (extract GLB)
**Tools:** APS Model Derivative API (cloud), forge-convert-utils (Node.js, SVF to glTF), gltf-pipeline (glTF to GLB)
**How it works:** DWG is uploaded to Autodesk's cloud. The Model Derivative API translates it to SVF format using Autodesk's proprietary geometry engine (which includes the ACIS kernel). `forge-convert-utils` reads the SVF binary and outputs glTF. `gltf-pipeline` packages it into GLB.

### Result: SUCCESS

| File | 3DSOLID | Meshes | Vertices | Faces | GLB size |
|------|---------|--------|----------|-------|----------|
| Manhole | 7 | 11 | 540 | 464 | 31 KB |
| Launch shaft | 1621 | 14 meshpacks | -- | -- | 16 MB |

The APS API successfully tessellated all 3DSOLID entities because Autodesk's server-side engine has native ACIS support (Autodesk owns the DWG format and licenses the ACIS kernel).

**Pipeline steps:**
1. Authenticate via 2-legged OAuth v2 (client credentials)
2. Upload DWG to OSS bucket (signed S3 upload)
3. Submit Model Derivative translation job (DWG to SVF)
4. Poll until translation completes (~10-40 seconds)
5. Extract GLB from SVF via forge-convert-utils

**Requirements:**
- Autodesk developer account (free tier available)
- APS application with Data Management API + Model Derivative API enabled
- Internet connection (cloud-based translation)
- Node.js (for forge-convert-utils)

---

## Summary

| Method | Can read DXF | Can tessellate 3DSOLID | GLB output | Cost |
|--------|-------------|----------------------|------------|------|
| ezdxf + trimesh | Yes | No | No | Free |
| Blender 4.2 | No (addon removed) | No | No | Free |
| FreeCAD + ODA | Yes (partial) | No | No | Free |
| **APS API** | **N/A (reads DWG directly)** | **Yes** | **Yes** | **Free tier available** |

**Conclusion:** For DWG files containing 3DSOLID (ACIS) geometry, the only programmatic conversion path that works is the Autodesk Platform Services API. All open-source tools fail because they lack the proprietary ACIS geometry kernel required to tessellate B-rep solid data into triangle meshes.
