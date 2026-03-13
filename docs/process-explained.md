# DWG to GLB Conversion Process

## Why We Need Autodesk's Cloud

The DWG files contain **3DSOLID** entities. Inside each 3DSOLID, the actual 3D shape is stored in a format called **ACIS** -- a proprietary format owned by Dassault Systemes. It describes shapes mathematically (surfaces, curves, edges) rather than as triangles.

To turn that into GLB (which is just triangles), you need to **tessellate** it -- convert the math into triangles. Only Autodesk's own engine can do that, because they license the ACIS kernel. So we send the DWG to their cloud, they tessellate it, and we download the result.

## Overview

There are 2 scripts that run in sequence:

1. **`converters/aps/convert_aps.py`** (Python) -- uploads the DWG to Autodesk's cloud and triggers translation
2. **`converters/aps/svf_to_glb.js`** (Node.js) -- downloads the translated result and converts it to GLB

```
                YOUR MACHINE                          AUTODESK CLOUD
                ============                          ==============

convert_aps.py:
  [DWG file] --upload--> [OSS Bucket storage]
                          |
                          v
                          [Model Derivative API]
                          |  (reads DWG with ACIS kernel)
                          |  (tessellates 3DSOLID into triangles)
                          |  (outputs SVF format)
                          v
                          [SVF derivatives ready]

svf_to_glb.js:
  [SVF derivatives] <--download-- [Autodesk servers]
  |
  v
  [forge-convert-utils reads SVF binary]
  |
  v
  [GltfWriter outputs .gltf + .bin]
  |
  v
  [gltf-pipeline packages into .glb]
  |
  v
  [Final GLB file]
```

---

## Script 1: convert_aps.py (Python)

This script does 5 things:

### Step 1: Authentication (line 33-46)

```
Client ID + Client Secret --> Base64 encode --> POST to Autodesk --> Access token
```

Standard OAuth2 "client credentials" flow. You send your app's ID and secret to Autodesk, they give you a temporary token (expires in ~1 hour). Every subsequent API call uses this token in the `Authorization: Bearer <token>` header.

The endpoint is `https://developer.api.autodesk.com/authentication/v2/token`.

### Step 2: Create a storage bucket (line 49-64)

Autodesk's cloud needs somewhere to store your file. A "bucket" is like a folder on their servers. The script creates one called `matterport-dwg-{first 8 chars of your client ID}` with a `transient` policy (files auto-delete after 24 hours).

If the bucket already exists (409 response), it just moves on.

### Step 3: Upload the DWG file (line 67-123)

There are two upload methods:

**Method A** (line 67-91): Simple PUT upload -- sends the file directly. This is the old way and sometimes fails with a deprecation error.

**Method B** (line 94-123): S3 signed upload -- the newer approach. Three sub-steps:
1. Ask Autodesk for a temporary S3 upload URL (line 99-107)
2. PUT the file directly to Amazon S3 using that URL (line 110-112)
3. Tell Autodesk "the upload is done" to finalize it (line 115-122)

The script tries Method A first, and if it fails, falls back to Method B (line 87-88).

One detail: Japanese filenames cause URL encoding issues, so the script hashes the filename to an ASCII-safe name using md5 (line 72). The actual file content is unchanged.

After upload, you get back an `objectId` -- something like `urn:adsk.objects:os.object:matterport-dwg-abc12345/a1b2c3d4e5f6.dwg`.

### Step 4: Submit a translation job (line 130-153)

This is the key step. The script tells Autodesk: "take this DWG file and convert it to SVF format."

The `objectId` from step 3 is base64-encoded to create a **URN** (line 126-127). This URN is how Autodesk tracks the file across all their APIs.

The POST request to `/modelderivative/v2/designdata/job` tells Autodesk's **Model Derivative API** to start translating. On Autodesk's servers, their geometry engine (which has the ACIS kernel) reads the DWG, tessellates all the 3DSOLID entities into triangles, and packages everything into SVF format.

### Step 5: Poll until done (line 156-180)

Translation takes ~10-40 seconds. The script checks every 10 seconds by hitting the manifest endpoint (`/modelderivative/v2/designdata/{urn}/manifest`). The response contains `status` (pending/inprogress/success/failed) and `progress` (percentage).

When `status` becomes `"success"`, the manifest also contains the URN of the result -- which is what we pass to the next script.

---

## Script 2: svf_to_glb.js (Node.js)

This script takes the URN from Script 1 and extracts the actual 3D geometry.

### Step 1: Authentication (line 20-48)

Same OAuth2 flow as the Python script, but done in Node.js using the raw `https` module. It authenticates separately because `forge-server-utils` (the library we use next) internally tries to use the old v1 auth endpoint which is deprecated. So we pre-generate a v2 token ourselves and pass it as `{ token }` (line 70).

### Step 2: Fetch the manifest (line 73-81)

Uses `ModelDerivativeClient` from the `forge-server-utils` library to get the translation manifest. The manifest lists all the "derivatives" (output files) that Autodesk generated. We use `ManifestHelper` to search for entries with `type: "resource"` and `role: "graphics"` -- these are the actual 3D geometry files.

A typical manifest contains multiple derivative types:

| MIME type | What it is | Do we use it? |
|-----------|-----------|---------------|
| `application/autodesk-svf` | 3D graphics package | Yes -- this is the geometry |
| `application/autodesk-f2d` | 2D drawing sheets | No -- skip |
| `application/autodesk-svf2` | Newer 3D format | No -- not supported by forge-convert-utils |

The script filters for `application/autodesk-svf` only (line 85-88).

### What is inside an SVF derivative?

SVF is not a single file -- it's a package (like a zip) containing multiple binary files stored on Autodesk's CDN. When `forge-convert-utils` reads it, it downloads all these parts behind the scenes:

| Component | What it contains |
|-----------|-----------------|
| **Meshpacks** (`.pack` files) | The actual triangle mesh data -- vertices, faces, normals, UV coordinates. A simple model might have 1 meshpack, a complex model can have 10+. |
| **Fragment data** | The scene tree -- which mesh goes where. Contains transform matrices (position, rotation, scale) and material assignments for each object. |
| **Materials** | Color and appearance definitions for each object (diffuse color, opacity, etc.). |
| **Property database** | Metadata about each object (layer name, object ID, CAD properties). Not used for GLB but available. |

We never deal with these individual files directly. The `SvfReader` library handles downloading and parsing all of them, and gives us a single scene object with everything combined.

### Step 3: Read SVF and write glTF (line 84-101)

For each SVF derivative:

1. **`SvfReader.FromDerivativeService(urn, guid, auth)`** (line 91) -- connects to Autodesk's CDN and downloads all the SVF components listed above (meshpacks, fragments, materials). Each component is a separate HTTP request behind the scenes.

2. **`reader.read()`** (line 92) -- parses all the downloaded binary data into an in-memory scene object. This is where meshpacks get decoded into actual vertex/face arrays, fragments get resolved into a scene tree, and materials get attached to the right meshes.

3. **`GltfWriter`** (line 94-98) -- takes that scene and writes it as glTF files (a `.gltf` JSON file + a `.bin` binary file containing the vertex/face data). Options:
   - `deduplicate: true` -- removes duplicate meshes to reduce file size
   - `skipUnusedUvs: true` -- removes unused texture coordinates
   - `center: true` -- centers the model at origin

### Step 4: Package as GLB

The output from step 3 is glTF files. To get a single GLB file, run:

```bash
npx gltf-pipeline -i output.gltf -o output.glb
```

This just packages the `.gltf` + `.bin` into one binary file. No geometry processing happens here.

For complex models, the glTF might have validation errors from `forge-convert-utils`. In that case, use `gltf-transform` to repair:

```bash
npx @gltf-transform/cli copy output.gltf output_fixed.glb
```

---

## Key Terms

| Term | What it is |
|------|------------|
| **DWG** | Autodesk's proprietary CAD file format |
| **3DSOLID** | An entity type inside DWG that stores 3D shapes |
| **ACIS** | The proprietary geometry format inside 3DSOLID entities |
| **Tessellation** | Converting mathematical surfaces into triangle meshes |
| **APS** | Autodesk Platform Services (formerly Forge) -- Autodesk's cloud API |
| **OSS** | Object Storage Service -- Autodesk's cloud file storage |
| **Model Derivative API** | The APS API that translates CAD files into viewable formats |
| **SVF** | Simple Viewable Format -- Autodesk's viewer format containing tessellated meshes |
| **URN** | Base64-encoded identifier that Autodesk uses to track files |
| **glTF** | Open 3D format (JSON + binary). Human-readable scene description |
| **GLB** | Binary glTF. Same as glTF but packed into a single file |
| **forge-convert-utils** | Node.js library that reads SVF and outputs glTF |
| **gltf-pipeline** | Tool that packages glTF into GLB |

## SVF vs SVF2

- **SVF**: Older viewer format. Supported by `forge-convert-utils`. This is what we use.
- **SVF2**: Newer format. NOT supported by `forge-convert-utils` (no reader exists). Do not use.
