#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Build FIPS-compliant Istio images (Envoy with BoringSSL, Go with BoringCrypto)."""

import argparse
import atexit
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
IMAGES = ["proxyv2", "pilot"]


def detect_arch() -> str:
    machine = platform.machine()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    sys.exit(f"Unsupported architecture: {machine}")


def run(
    cmd: str | list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = False,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command, printing it first."""
    if isinstance(cmd, str):
        print(f"+ {cmd}", flush=True)
    else:
        print(f"+ {' '.join(cmd)}", flush=True)
    return subprocess.run(
        cmd,
        shell=isinstance(cmd, str),
        env=env,
        check=check,
        capture_output=capture,
        text=True,
        cwd=cwd,
    )


# ---------------------------------------------------------------------------
# Version resolution
# ---------------------------------------------------------------------------


def resolve_version(version_arg: str | None) -> str:
    if not version_arg:
        return os.environ.get("ISTIO_VERSION", "1.29.0")

    if version_arg == "latest":
        resp = urlopen("https://api.github.com/repos/istio/istio/releases/latest")
        tag = json.loads(resp.read())["tag_name"]
        if not tag:
            sys.exit("Error: Failed to fetch latest Istio version")
        return tag

    req = Request(
        f"https://api.github.com/repos/istio/istio/releases/tags/{version_arg}",
        method="HEAD",
    )
    try:
        urlopen(req)
    except HTTPError:
        sys.exit(
            f"Error: Istio version {version_arg} does not exist\n"
            "Check available versions at: https://github.com/istio/istio/releases"
        )
    return version_arg


# ---------------------------------------------------------------------------
# Local registry
# ---------------------------------------------------------------------------


def start_registry() -> None:
    run("docker run --rm -d -p 5000:5000 --name local-registry registry:2")
    time.sleep(2)
    atexit.register(lambda: run("docker rm local-registry -f", check=False))


# ---------------------------------------------------------------------------
# Build Envoy proxy with FIPS BoringSSL
# ---------------------------------------------------------------------------


def build_envoy(version: str) -> None:
    run(
        f"git clone https://github.com/istio/proxy.git --depth 1 --branch {version} --single-branch"
    )

    # https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/security/ssl#fips-140-2
    Path("proxy/user.bazelrc").write_text("build --define boringssl=fips\n")
    run("make build_envoy", cwd="proxy")


# ---------------------------------------------------------------------------
# Build Istio control-plane and data-plane images
# ---------------------------------------------------------------------------


def build_istio(version: str, build_hub: str, tags: str, arch: str) -> None:
    run(
        f"git clone https://github.com/istio/istio.git --depth 1 --branch {version} --single-branch"
    )

    istio_dir = Path("istio")

    # Place pre-built envoy so the Istio build doesn't download from GCS
    deps = json.loads((istio_dir / "istio.deps").read_text())
    proxy_sha = next(d["lastStableSHA"] for d in deps if d["name"] == "PROXY_REPO_SHA")

    release_dir = istio_dir / f"out/linux_{arch}/release"
    release_dir.mkdir(parents=True, exist_ok=True)

    envoy_src = Path("proxy/bazel-bin/envoy")
    (release_dir / f"envoy-{proxy_sha}").write_bytes(envoy_src.read_bytes())
    (release_dir / "envoy").write_bytes(envoy_src.read_bytes())

    # Enable BoringCrypto for Go binaries
    # https://github.com/tetratelabs/istio/blob/tetrate-workflow/tetrateci/docs/fips.md
    makefile = istio_dir / "Makefile.core.mk"
    content = makefile.read_text()
    if "GOEXPERIMENT=boringcrypto" not in content:
        makefile.write_text(
            content.replace(
                "GOOS=linux", "CGO_ENABLED=1 GOEXPERIMENT=boringcrypto GOOS=linux"
            )
        )

    # Envoy with BoringSSL needs libstdc++ in the iptables image
    iptables_yaml = istio_dir / "docker/iptables.yaml"
    content = iptables_yaml.read_text()
    if "libstdc++" not in content:
        iptables_yaml.write_text(
            re.sub(r"(- libgcc\n)", r"\1    - libstdc++\n", content)
        )

    # Build base images with apko
    go_bin = Path.home() / "go/bin"
    env = {**os.environ, "PATH": f"{go_bin}:{os.environ['PATH']}"}
    run(
        f"apko publish --arch={arch} docker/iptables.yaml {build_hub}/iptables:{tags}",
        env=env,
        cwd="istio",
    )
    run(
        f"apko publish --arch={arch} {SCRIPT_DIR}/distroless.yaml {build_hub}/distroless:{tags}",
        env=env,
        cwd="istio",
    )

    # Build pilot and proxyv2
    build_env = {
        **env,
        "BASE_VERSION": tags,
        "ISTIO_BASE_REGISTRY": build_hub,
        "HUB": build_hub,
        "DOCKER_BUILD_VARIANTS": "distroless",
        "TARGET_OS": "linux",
        "TARGET_ARCH": arch,
    }
    run("make docker.proxyv2 docker.pilot", env=build_env, cwd="istio")


# ---------------------------------------------------------------------------
# Verify, tag, and push
# ---------------------------------------------------------------------------


def verify_images(build_hub: str, tags: str) -> None:
    print("--- Verifying built images ---", flush=True)
    run(
        f'docker run --rm --entrypoint="" {build_hub}/proxyv2:{tags}-distroless envoy --version'
    )
    run(
        f'docker run --rm --entrypoint="" {build_hub}/proxyv2:{tags}-distroless pilot-agent version'
    )
    run(
        f'docker run --rm --entrypoint="" {build_hub}/pilot:{tags}-distroless pilot-discovery version'
    )


def tag_and_push(
    build_hub: str, export_hub: str, tags: str, minor_tag: str, arch: str
) -> None:
    # Authenticate to GHCR if applicable
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if export_hub.startswith("ghcr.io/") and github_token:
        print("Authenticating with GitHub Container Registry...", flush=True)
        actor = os.environ.get("GITHUB_ACTOR", os.environ.get("USER", ""))
        run(f"echo $GITHUB_TOKEN | docker login ghcr.io -u {actor} --password-stdin")

    for image in IMAGES:
        src = f"{build_hub}/{image}:{tags}-distroless"
        for tag in (tags, minor_tag):
            dest = f"{export_hub}/{image}:{tag}-{arch}"
            run(f"docker tag {src} {dest}")
            run(f"docker push {dest}")


# ---------------------------------------------------------------------------
# GitHub Actions job summary
# ---------------------------------------------------------------------------


def get_version_output(hub: str, image: str, tag: str, cmd: str) -> str:
    result = run(
        f'docker run --rm --entrypoint="" {hub}/{image}:{tag} {cmd}',
        capture=True,
        check=False,
    )
    output = (result.stdout or "").strip().splitlines()
    return output[0] if output else "unknown"


def write_summary(
    export_hub: str, version: str, tags: str, minor_tag: str, arch: str
) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    envoy_ver = get_version_output(
        export_hub, "proxyv2", f"{minor_tag}-{arch}", "envoy --version"
    )
    agent_ver = get_version_output(
        export_hub, "proxyv2", f"{minor_tag}-{arch}", "pilot-agent version"
    )
    discovery_ver = get_version_output(
        export_hub, "pilot", f"{minor_tag}-{arch}", "pilot-discovery version"
    )

    with open(summary_path, "a") as f:
        f.write(
            f"""## Istio FIPS Build Summary ({arch})

| | |
|---|---|
| **Istio Version** | {version} |
| **Architecture** | {arch} |
| **Tags** | {tags}-{arch}, {minor_tag}-{arch} |
| **Export Registry** | {export_hub} |

### Component Versions
- **envoy:** {envoy_ver}
- **pilot-agent:** {agent_ver}
- **pilot-discovery:** {discovery_ver}
"""
        )


# ===========================================================================
# Main
# ===========================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="Build FIPS-compliant Istio images")
    parser.add_argument(
        "--version", dest="version", default=None, help="Istio version or 'latest'"
    )
    parser.add_argument(
        "--arch",
        dest="arch",
        default=None,
        help="Target architecture (amd64, arm64). Defaults to host arch.",
    )
    args = parser.parse_args()

    # Resolve version and arch
    version = resolve_version(args.version)
    arch = args.arch or detect_arch()
    print(f"Istio version: {version}")
    print(f"Architecture: {arch}")

    major_version = version.rsplit(".", 1)[0]
    tags = f"{major_version}-fips"
    minor_tag = f"{version}-fips"

    # Build configuration
    build_hub = os.environ.get("BUILD_HUB", "localhost:5000")
    export_hub = os.environ.get("EXPORT_HUB", build_hub).lower()

    # Set env vars needed by Istio's Makefile
    os.environ.update(
        {
            "ISTIO_VERSION": version,
            "GOOS": "linux",
            "GOARCH": arch,
            "TARGET_OS": "linux",
            "TARGET_ARCH": arch,
            "BUILD_WITH_CONTAINER": "0",
            "BAZEL_BUILD_ARGS": "--config=release --verbose_failures --sandbox_debug",
        }
    )

    config: dict[str, str] = {
        "ISTIO_VERSION": version,
        "ARCH": arch,
        "MAJOR_ISTIO_VERSION": major_version,
        "TAGS": tags,
        "BUILD_HUB": build_hub,
        "EXPORT_HUB": export_hub,
    }
    for k, v in config.items():
        print(f"  {k}={v}")

    start_registry()
    build_envoy(version)
    build_istio(version, build_hub, tags, arch)
    verify_images(build_hub, tags)
    tag_and_push(build_hub, export_hub, tags, minor_tag, arch)
    write_summary(export_hub, version, tags, minor_tag, arch)

    print(f"Done. Images pushed to {export_hub}")


if __name__ == "__main__":
    main()
