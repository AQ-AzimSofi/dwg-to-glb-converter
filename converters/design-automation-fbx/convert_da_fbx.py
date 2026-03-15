"""
Convert DWG to GLB via Design Automation API (3ds Max engine).

Pipeline:
1. Upload DWG to APS bucket
2. Submit WorkItem (runs 3ds Max in the cloud, exports FBX)
3. Download FBX
4. Convert FBX to GLB with colors from DXF layer mapping
"""

import argparse
import base64
import ctypes
import hashlib
import os
import sys
import time
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

APS_BASE = "https://developer.api.autodesk.com"
DA_BASE = f"{APS_BASE}/da/us-east/v3"
CLIENT_ID = os.environ.get("APS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("APS_CLIENT_SECRET", "")
BUCKET_KEY = f"matterport-dwg-{CLIENT_ID[:8].lower()}"
NICKNAME = CLIENT_ID[:12].lower()
ACTIVITY_ID = f"{NICKNAME}.ExportToFbx+prod"


def get_token():
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        f"{APS_BASE}/authentication/v2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials&scope=data:read data:write data:create bucket:create bucket:read code:all",
    )
    resp.raise_for_status()
    print("[auth] got token")
    return resp.json()["access_token"]


def ensure_bucket(token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{APS_BASE}/oss/v2/buckets",
        headers=headers,
        json={"bucketKey": BUCKET_KEY, "policyKey": "transient"},
    )
    if resp.status_code == 409:
        print(f"[bucket] '{BUCKET_KEY}' already exists")
    elif resp.status_code == 200:
        print(f"[bucket] created '{BUCKET_KEY}'")
    else:
        resp.raise_for_status()


def upload_file(token, filepath):
    filename = Path(filepath).name
    filesize = os.path.getsize(filepath)
    safe_name = hashlib.md5(filename.encode()).hexdigest()[:12] + ".dwg"
    print(f"[upload] uploading {filename} as {safe_name} ({filesize / 1024:.0f} KB)")

    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(
        f"{APS_BASE}/oss/v2/buckets/{BUCKET_KEY}/objects/{safe_name}/signeds3upload",
        headers=headers,
        params={"firstPart": 1, "parts": 1},
    )
    resp.raise_for_status()
    data = resp.json()
    upload_key = data["uploadKey"]
    upload_url = data["urls"][0]

    with open(filepath, "rb") as f:
        s3_resp = requests.put(upload_url, data=f)
    s3_resp.raise_for_status()

    resp = requests.post(
        f"{APS_BASE}/oss/v2/buckets/{BUCKET_KEY}/objects/{safe_name}/signeds3upload",
        headers={**headers, "Content-Type": "application/json"},
        json={"uploadKey": upload_key},
    )
    resp.raise_for_status()
    object_id = resp.json()["objectId"]
    print(f"[upload] done. objectId: {object_id}")
    return object_id, safe_name


def get_signed_download_url(token, object_key):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        f"{APS_BASE}/oss/v2/buckets/{BUCKET_KEY}/objects/{object_key}/signeds3download",
        headers=headers,
    )
    resp.raise_for_status()
    return resp.json()["url"]


def get_signed_upload_url(token, object_key):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        f"{APS_BASE}/oss/v2/buckets/{BUCKET_KEY}/objects/{object_key}/signeds3upload",
        headers=headers,
        params={"firstPart": 1, "parts": 1},
    )
    resp.raise_for_status()
    data = resp.json()
    return data["urls"][0], data["uploadKey"]


def submit_workitem(token, dwg_download_url, fbx_upload_url, log_upload_url=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "activityId": ACTIVITY_ID,
        "arguments": {
            "inputFile": {
                "url": dwg_download_url,
                "verb": "get",
            },
            "outputFile": {
                "url": fbx_upload_url,
                "verb": "put",
            },
        },
    }
    if log_upload_url:
        payload["arguments"]["logFile"] = {
            "url": log_upload_url,
            "verb": "put",
        }
    resp = requests.post(f"{DA_BASE}/workitems", headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    workitem_id = data["id"]
    print(f"[workitem] submitted: {workitem_id}")
    return workitem_id


def poll_workitem(token, workitem_id, timeout=600):
    headers = {"Authorization": f"Bearer {token}"}
    start = time.time()
    while time.time() - start < timeout:
        resp = requests.get(
            f"{DA_BASE}/workitems/{workitem_id}",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "unknown")
        print(f"[poll] status={status}")

        if status == "success":
            report_url = data.get("reportUrl", "")
            if report_url:
                print(f"[poll] report: {report_url}")
            return data
        if status in ("failedDownload", "failedInstructions", "failedUpload", "cancelled"):
            print(f"[poll] WorkItem FAILED: {status}")
            report_url = data.get("reportUrl", "")
            if report_url:
                print(f"[poll] report: {report_url}")
                report = requests.get(report_url).text
                print(report[-2000:])
            sys.exit(1)

        time.sleep(5)

    print(f"[poll] timed out after {timeout}s")
    sys.exit(1)


def download_fbx(token, object_key, output_path):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        f"{APS_BASE}/oss/v2/buckets/{BUCKET_KEY}/objects/{object_key}/signeds3download",
        headers=headers,
    )
    resp.raise_for_status()
    download_url = resp.json()["url"]

    resp = requests.get(download_url)
    resp.raise_for_status()

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(resp.content)
    print(f"[download] saved {output_path} ({len(resp.content) / 1024:.1f} KB)")


def fbx_to_glb(fbx_path, glb_path):
    import pyassimp
    import pyassimp.postprocess
    import trimesh

    default_color = [200, 200, 200, 255]

    with pyassimp.load(
        fbx_path,
        processing=pyassimp.postprocess.aiProcess_Triangulate,
    ) as assimp_scene:
        print(f"[convert] loaded FBX: {len(assimp_scene.meshes)} meshes")

        # Read material colors from FBX (set by wirecolor-to-material MAXScript)
        mat_colors = {}
        for i, mat in enumerate(assimp_scene.materials):
            props = dict(mat.properties)
            diff = props.get("diffuse", [0.8, 0.8, 0.8])
            rgba = [int(c * 255) for c in diff[:3]] + [255]
            mat_colors[i] = rgba
            print(f"[material] {i}: {mat.properties.get('name', '?')} -> RGB({rgba[0]},{rgba[1]},{rgba[2]})")

        ptr_to_idx = {}
        for i, mesh in enumerate(assimp_scene.meshes):
            ptr = ctypes.cast(mesh, ctypes.c_void_p).value
            ptr_to_idx[ptr] = i

        mesh_entries = []

        def walk(node, parent_transform):
            local = np.array(node.transformation, dtype=np.float64).reshape(4, 4)
            world = parent_transform @ local

            for mesh_ref in node.meshes:
                ptr = ctypes.cast(mesh_ref, ctypes.c_void_p).value
                if ptr in ptr_to_idx:
                    idx = ptr_to_idx[ptr]
                    mat_idx = assimp_scene.meshes[idx].materialindex
                    color = mat_colors.get(mat_idx, default_color)
                    mesh_entries.append((idx, world.copy(), color))

            for child in node.children:
                walk(child, world)

        walk(assimp_scene.rootnode, np.eye(4))

        scene = trimesh.Scene()
        matched = 0
        skipped = 0

        for entry_i, (idx, world_tf, color) in enumerate(mesh_entries):
            mesh = assimp_scene.meshes[idx]
            verts = np.array(mesh.vertices, dtype=np.float64)
            faces = np.array(mesh.faces, dtype=np.int64)
            if len(verts) == 0 or len(faces) == 0:
                continue

            # Apply world transform to vertices
            ones = np.ones((len(verts), 1), dtype=np.float64)
            verts_h = np.hstack([verts, ones])
            verts_world = (world_tf @ verts_h.T).T[:, :3]

            # Skip line-like meshes (2D lines/arcs from DWG)
            bbox_min = verts_world.min(axis=0)
            bbox_max = verts_world.max(axis=0)
            dims = sorted(bbox_max - bbox_min)
            if dims[0] < 0.1 and dims[1] < 0.1 and len(faces) <= 8:
                skipped += 1
                continue

            if color != default_color:
                matched += 1

            face_colors = np.tile(color, (len(faces), 1)).astype(np.uint8)
            tmesh = trimesh.Trimesh(
                vertices=verts_world, faces=faces,
                face_colors=face_colors, process=False,
            )
            scene.add_geometry(tmesh, node_name=f"mesh_{entry_i}")

    bounds = scene.bounds
    if bounds is not None:
        center = (bounds[0] + bounds[1]) / 2.0
        for geom in scene.geometry.values():
            geom.vertices -= center

    total_verts = sum(len(g.vertices) for g in scene.geometry.values())
    total_meshes = len(scene.geometry)
    print(f"[filter] skipped {skipped} line-like meshes")
    print(f"[color] {matched}/{total_meshes} meshes colored")
    print(f"[convert] {total_meshes} meshes, {total_verts} vertices")
    scene.export(glb_path, file_type="glb")
    size_kb = os.path.getsize(glb_path) / 1024
    print(f"[convert] saved {glb_path} ({size_kb:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Convert DWG to GLB via Design Automation (3ds Max FBX pipeline)"
    )
    parser.add_argument("input", help="Path to DWG file")
    parser.add_argument("-o", "--output", help="Output directory")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f"File not found: {input_path}")
        sys.exit(1)

    output_dir = Path(args.output) if args.output else Path("output_glb_da_fbx")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not CLIENT_ID or not CLIENT_SECRET:
        print("Set APS_CLIENT_ID and APS_CLIENT_SECRET in .env")
        sys.exit(1)

    stem = input_path.stem
    fbx_object_key = hashlib.md5(stem.encode()).hexdigest()[:12] + ".fbx"
    log_object_key = hashlib.md5(stem.encode()).hexdigest()[:12] + ".log.txt"

    token = get_token()
    ensure_bucket(token)

    object_id, dwg_key = upload_file(token, str(input_path))

    dwg_download_url = get_signed_download_url(token, dwg_key)
    fbx_upload_url, fbx_upload_key = get_signed_upload_url(token, fbx_object_key)
    log_upload_url, log_upload_key = get_signed_upload_url(token, log_object_key)

    workitem_id = submit_workitem(
        token, dwg_download_url, fbx_upload_url, log_upload_url=log_upload_url
    )
    poll_workitem(token, workitem_id)

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    requests.post(
        f"{APS_BASE}/oss/v2/buckets/{BUCKET_KEY}/objects/{fbx_object_key}/signeds3upload",
        headers=headers,
        json={"uploadKey": fbx_upload_key},
    )
    requests.post(
        f"{APS_BASE}/oss/v2/buckets/{BUCKET_KEY}/objects/{log_object_key}/signeds3upload",
        headers=headers,
        json={"uploadKey": log_upload_key},
    )

    fbx_path = output_dir / f"{stem}.fbx"
    glb_path = output_dir / f"{stem}.glb"
    log_path = output_dir / f"{stem}.log.txt"
    download_fbx(token, fbx_object_key, str(fbx_path))

    try:
        download_fbx(token, log_object_key, str(log_path))
        print(f"[log] saved {log_path}")
        with open(log_path) as f:
            print(f.read())
    except Exception as e:
        print(f"[log] could not download log: {e}")

    fbx_to_glb(str(fbx_path), str(glb_path))

    print(f"\n[done] {input_path.name} -> {glb_path}")


if __name__ == "__main__":
    main()
