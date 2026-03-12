# Converter Scripts Explained

## Working Solution: APS Pipeline

### converters/aps/convert_aps.py (Python)

Handles the first half of the pipeline: authentication, upload, and translation.

**Key functions:**
- `get_token()` -- 2-legged OAuth v2 via client credentials
- `ensure_bucket()` -- Creates OSS bucket if it doesn't exist
- `upload_file_s3()` -- Signed S3 upload (the old PUT endpoint is deprecated)
- `safe_urn()` -- Generates ASCII-safe filename using md5 hash (avoids URL encoding issues with Japanese filenames)
- `translate_to_obj()` -- Submits Model Derivative translation job (DWG to SVF)
- `poll_translation()` -- Polls until translation completes

**Auth details:**
- Endpoint: `https://developer.api.autodesk.com/authentication/v2/token`
- Scope: `data:read data:write data:create bucket:create bucket:read`
- Bucket key: `matterport-dwg-{CLIENT_ID[:8].lower()}`

### converters/aps/svf_to_glb.js (Node.js)

Handles the second half: extracting GLB from the SVF translation result.

**How it works:**
1. Loads credentials from `.env`
2. Authenticates via v2 OAuth (using raw `https` module, not forge-server-utils auth which uses deprecated v1)
3. Passes pre-generated token as `{ token }` to `ModelDerivativeClient`
4. Fetches manifest and filters for `application/autodesk-svf` MIME type
5. Uses `SvfReader.FromDerivativeService()` to read SVF binary data
6. Uses `GltfWriter` with `{ deduplicate: true, skipUnusedUvs: true, center: true }` to write glTF

**Dependencies:**
- `forge-server-utils` -- Provides `ModelDerivativeClient` and `ManifestHelper`
- `forge-convert-utils` -- Provides `SvfReader` and `GltfWriter`

---

## Failed Open-Source Attempts

These scripts are archived under `converters/opensource-attempts/` for reference.
All three fail on 3DSOLID entities because they lack the ACIS kernel.

### converters/opensource-attempts/trimesh/convert_pure.py

Uses ezdxf to parse DXF and trimesh to export GLB. Can handle 3DFACE and MESH
entities but skips all 3DSOLID entities. Since the test DWG files contain only
3DSOLID geometry, no GLB is produced.

### converters/opensource-attempts/blender/convert_pure.py

Attempts to use Blender 4.2 in headless mode. Fails immediately because the
`io_import_dxf` addon was removed in Blender 4.2. Even older versions that
included the addon could not handle 3DSOLID entities.

### converters/opensource-attempts/freecad/convert_pure.py

Uses ODA File Converter (DWG to DXF) then FreeCAD's `importDXF` module.
FreeCAD explicitly rejects 3DSOLID entities because its OpenCASCADE kernel
supports STEP/IGES/BREP but not ACIS.
