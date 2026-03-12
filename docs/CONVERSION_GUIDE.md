# DWG to GLB Conversion Guide

## Pipeline Overview

```
Customer DWG --> APS API (cloud) --> SVF --> forge-convert-utils --> GLB
                  (step 1-4)                    (step 5)
```

DWG files containing 3DSOLID entities use the proprietary ACIS geometry format.
No open-source tool can tessellate ACIS data. The only programmatic conversion
path is the Autodesk Platform Services (APS) API, which has native ACIS support.

## Prerequisites

- Autodesk developer account with APS application (free tier available)
- APS app must have **Data Management API** and **Model Derivative API** enabled
- Node.js (for forge-convert-utils)
- Python 3.12+ (for the upload/translate script)
- Internet connection (APS is cloud-based)

## Setup

1. Create a `.env` file in the project root:
   ```
   APS_CLIENT_ID=your_client_id
   APS_CLIENT_SECRET=your_client_secret
   ```

2. Install Python dependencies:
   ```bash
   source .venv/bin/activate
   pip install requests python-dotenv
   ```

3. Install Node.js dependencies:
   ```bash
   npm install
   ```

## Running the Conversion

### Step 1: Upload and translate DWG

```bash
source .venv/bin/activate
python3 converters/aps/convert_aps.py input_dwg/your_file.dwg
```

This script:
1. Authenticates via 2-legged OAuth v2 (client credentials)
2. Creates an OSS bucket (if not exists)
3. Uploads the DWG via signed S3 upload
4. Submits a Model Derivative translation job (DWG to SVF)
5. Polls until translation completes (~10-40 seconds)
6. Prints the base64 URN needed for the next step

### Step 2: Extract GLB from SVF

```bash
node converters/aps/svf_to_glb.js <urn> [output_dir]
```

- `<urn>`: The base64-encoded URN from step 1
- `[output_dir]`: Optional, defaults to `output_glb_aps/`

This script:
1. Authenticates via v2 OAuth
2. Fetches the translation manifest
3. Reads SVF graphics data via `forge-convert-utils`
4. Writes glTF + .bin files to the output directory

### Step 3: Package as GLB (if needed)

If the output is glTF (`.gltf` + `.bin`), package into a single `.glb`:
```bash
npx gltf-pipeline -i output_glb_aps/output.gltf -o output_glb_aps/output.glb
```

For complex models with validation errors, repair with:
```bash
npx @gltf-transform/cli copy output_glb_aps/output.gltf output_glb_aps/output_fixed.glb
```

## File Structure

```
matterport/
  converters/
    aps/
      convert_aps.py     -- upload + translate DWG via APS API
      svf_to_glb.js      -- extract GLB from SVF translation result
    opensource-attempts/  -- failed open-source approaches (for reference)
      trimesh/            -- ezdxf + trimesh (cannot tessellate 3DSOLID)
      blender/            -- Blender 4.2 (DXF addon removed)
      freecad/            -- FreeCAD + ODA (cannot read ACIS)
  input_dwg/             -- customer DWG files
  output_glb_aps/        -- GLB output from APS pipeline
  report_dwg_to_glb.md   -- comparison report of all methods tried
  .env                   -- APS credentials (not committed)
```

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| SSL certificate error | Corporate proxy intercepting HTTPS | Disable proxy or set `REQUESTS_CA_BUNDLE` |
| 403 on upload | Japanese/unicode filename | Script auto-hashes filenames to ASCII |
| Translation stuck | Wrong output format | Ensure SVF (not SVF2) is requested |
| forge-server-utils 404 | Library uses deprecated v1 auth | Script pre-generates v2 token and passes `{ token }` |
| ACCESSOR_TOO_LONG in GLB | forge-convert-utils bug on complex models | Use `gltf-transform copy` to repair |

## Why Not Open-Source?

See `report_dwg_to_glb.md` for detailed test results. In short:
- **ezdxf + trimesh**: Can parse DXF metadata but has no geometry kernel for ACIS
- **Blender 4.2**: DXF import addon was removed entirely
- **FreeCAD + ODA**: OpenCASCADE kernel reads STEP/IGES/BREP but not ACIS

All three fail on 3DSOLID entities, which are the primary geometry type in these DWG files.
