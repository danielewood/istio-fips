#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Discover which supported Istio versions need FIPS builds.

Checks endoflife.date for supported versions, then checks GHCR for
existing images. Outputs a JSON list of versions that need building.

Environment variables:
    GITHUB_TOKEN   - GitHub token for GHCR auth
    EXPORT_HUB     - Container registry (e.g. ghcr.io/danielewood/istio-fips)
    INPUT_VERSION  - Optional: single version override (skips discovery)
    GITHUB_OUTPUT  - GitHub Actions output file
    GITHUB_STEP_SUMMARY - GitHub Actions summary file (optional)
"""

import json
import os
import sys
from base64 import b64encode
from datetime import date
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def write_output(key: str, value: str) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"{key}={value}\n")


def write_summary(text: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(text + "\n")


def get_ghcr_token(image_path: str, github_token: str) -> str | None:
    token_url = f"https://ghcr.io/token?scope=repository:{image_path}/proxyv2:pull"
    req = Request(token_url)
    auth = b64encode(f"token:{github_token}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    try:
        return json.loads(urlopen(req).read())["token"]
    except HTTPError:
        print("Warning: Could not authenticate with GHCR, will build all versions")
        return None


def image_exists(image_path: str, tag: str, ghcr_token: str) -> bool:
    manifest_url = f"https://ghcr.io/v2/{image_path}/proxyv2/manifests/{tag}"
    req = Request(manifest_url, method="HEAD")
    req.add_header("Authorization", f"Bearer {ghcr_token}")
    req.add_header(
        "Accept",
        "application/vnd.oci.image.index.v1+json, "
        "application/vnd.docker.distribution.manifest.v2+json",
    )
    try:
        urlopen(req)
        return True
    except HTTPError:
        return False


def get_supported_versions() -> list[str]:
    print("Fetching supported Istio versions from endoflife.date...")
    resp = urlopen("https://endoflife.date/api/istio.json")
    versions = json.loads(resp.read())

    today = date.today().isoformat()
    supported = [
        v["latest"] for v in versions if isinstance(v["eol"], str) and v["eol"] >= today
    ]
    print(f"Supported versions: {supported}")
    return supported


def main() -> None:
    export_hub = os.environ["EXPORT_HUB"].lower()
    github_token = os.environ["GITHUB_TOKEN"]
    input_version = os.environ.get("INPUT_VERSION", "")

    # Manual single-version override
    if input_version:
        versions = [input_version]
        print(f"Manual version requested: {input_version}")
        write_output("versions", json.dumps(versions))
        write_summary(f"### Building 1 Istio version (manual)\n\n- {input_version}")
        sys.exit(0)

    supported = get_supported_versions()
    image_path = export_hub.removeprefix("ghcr.io/")
    ghcr_token = get_ghcr_token(image_path, github_token)

    versions: list[str] = []
    for version in supported:
        tag = f"{version}-fips"

        exists = bool(ghcr_token) and image_exists(image_path, tag, ghcr_token)
        status = "EXISTS (skipping)" if exists else "NOT FOUND (will build)"
        print(f"  {export_hub}/proxyv2:{tag} ... {status}")

        if not exists:
            versions.append(version)

    print(f"\nVersions to build: {json.dumps(versions)}")
    write_output("versions", json.dumps(versions))

    if versions:
        lines = [f"### Building {len(versions)} Istio version(s)", ""]
        lines += [f"- {v}" for v in versions]
        write_summary("\n".join(lines))
    else:
        write_summary("### All supported Istio versions are already built")


if __name__ == "__main__":
    main()
