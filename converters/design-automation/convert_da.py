"""
Convert DWG to GLB via Design Automation API.

Pipeline:
1. Upload DWG to APS bucket
2. Submit WorkItem (runs AutoCAD in the cloud, exports STL)
3. Download STL
4. Convert STL to GLB with trimesh
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

APS_BASE = "https://developer.api.autodesk.com"
DA_BASE = f"{APS_BASE}/da/us-east/v3"
CLIENT_ID = os.environ.get("APS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("APS_CLIENT_SECRET", "")
BUCKET_KEY = f"matterport-dwg-{CLIENT_ID[:8].lower()}"
NICKNAME = CLIENT_ID[:12].lower()
ACTIVITY_ID = f"{NICKNAME}.ExportToStl+prod"


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
    print(f"[auth] got token")
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

    # Get signed upload URL
    resp = requests.get(
        f"{APS_BASE}/oss/v2/buckets/{BUCKET_KEY}/objects/{safe_name}/signeds3upload",
        headers=headers,
        params={"firstPart": 1, "parts": 1},
    )
    resp.raise_for_status()
    data = resp.json()
    upload_key = data["uploadKey"]
    upload_url = data["urls"][0]

    # Upload to S3
    with open(filepath, "rb") as f:
        s3_resp = requests.put(upload_url, data=f)
    s3_resp.raise_for_status()

    # Finalize
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


def submit_workitem(token, dwg_download_url, stl_upload_url):
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
                "url": stl_upload_url,
                "verb": "put",
            },
        },
    }
    resp = requests.post(f"{DA_BASE}/workitems", headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    workitem_id = data["id"]
    print(f"[workitem] submitted: {workitem_id}")
    return workitem_id


def poll_workitem(token, workitem_id, timeout=300):
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


def download_stl(token, object_key, output_path):
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


def stl_to_glb(stl_path, glb_path):
    import trimesh
    mesh = trimesh.load(stl_path)
    mesh.vertices -= mesh.centroid
    mesh.export(glb_path, file_type="glb")
    size_kb = os.path.getsize(glb_path) / 1024
    print(f"[convert] saved {glb_path} ({size_kb:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Convert DWG to GLB via Design Automation API"
    )
    parser.add_argument("input", help="Path to DWG file")
    parser.add_argument("-o", "--output", help="Output directory")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f"File not found: {input_path}")
        sys.exit(1)

    output_dir = Path(args.output) if args.output else Path("output_glb_da")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not CLIENT_ID or not CLIENT_SECRET:
        print("Set APS_CLIENT_ID and APS_CLIENT_SECRET in .env")
        sys.exit(1)

    stem = input_path.stem
    stl_object_key = hashlib.md5(stem.encode()).hexdigest()[:12] + ".stl"

    token = get_token()
    ensure_bucket(token)

    # Upload DWG
    object_id, dwg_key = upload_file(token, str(input_path))

    # Get signed URLs for WorkItem
    dwg_download_url = get_signed_download_url(token, dwg_key)
    stl_upload_url, stl_upload_key = get_signed_upload_url(token, stl_object_key)

    # Submit and poll WorkItem
    workitem_id = submit_workitem(token, dwg_download_url, stl_upload_url)
    poll_workitem(token, workitem_id)

    # Finalize the STL upload (DA engine uploaded directly to S3)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(
        f"{APS_BASE}/oss/v2/buckets/{BUCKET_KEY}/objects/{stl_object_key}/signeds3upload",
        headers=headers,
        json={"uploadKey": stl_upload_key},
    )

    # Download STL and convert to GLB
    stl_path = output_dir / f"{stem}.stl"
    glb_path = output_dir / f"{stem}.glb"
    download_stl(token, stl_object_key, str(stl_path))
    stl_to_glb(str(stl_path), str(glb_path))

    print(f"\n[done] {input_path.name} -> {glb_path}")


if __name__ == "__main__":
    main()
