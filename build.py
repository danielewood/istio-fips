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
import signal
import subprocess
import sys
import time
from pathlib import Path

from urllib.error import HTTPError
from urllib.request import Request, urlopen

# Flush every print immediately so CI logs appear in real time
sys.stdout.reconfigure(line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
IMAGES = ["proxyv2", "pilot"]
PATCH_SUFFIXES = (".diff", ".patch")


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
        print(f"+ {cmd}")
    else:
        print(f"+ {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        shell=isinstance(cmd, str),
        env=env,
        check=check,
        capture_output=capture,
        text=True,
        cwd=cwd,
    )


def find_version_patches(repo_name: str, version: str) -> list[Path]:
    repo_patch_dir = SCRIPT_DIR / "patches" / repo_name
    patch_files: list[Path] = []
    for suffix in PATCH_SUFFIXES:
        single_file = repo_patch_dir / f"{version}{suffix}"
        if single_file.is_file():
            patch_files.append(single_file)

    version_dir = repo_patch_dir / version
    if version_dir.is_dir():
        patch_files.extend(
            sorted(
                path
                for path in version_dir.iterdir()
                if path.is_file() and path.suffix in PATCH_SUFFIXES
            )
        )

    return patch_files


def apply_version_patches(repo_name: str, version: str, repo_dir: Path) -> None:
    patch_files = find_version_patches(repo_name, version)
    if not patch_files:
        return

    print(
        f"\n=== Applying {len(patch_files)} patch(es) for {repo_name} {version} ===\n"
    )
    for patch_file in patch_files:
        rel_patch = patch_file.relative_to(SCRIPT_DIR)
        print(f"Applying {rel_patch}")
        run(["git", "apply", "--check", str(patch_file)], cwd=repo_dir)
        run(["git", "apply", str(patch_file)], cwd=repo_dir)


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


def build_envoy(version: str, timeout_minutes: int | None = None) -> bool:
    """Build Envoy. Returns True if binary was produced, False if timed out."""
    print("\n=== Building Envoy with FIPS BoringSSL ===\n")
    run(
        f"git clone https://github.com/istio/proxy.git --depth 1 --branch {version} --single-branch"
    )
    proxy_dir = Path("proxy")
    apply_version_patches("proxy", version, proxy_dir)

    # Mark envoy build as clean — SOURCE_VERSION makes the workspace status
    # script report "Distribution" instead of reading git status.
    (proxy_dir / "SOURCE_VERSION").write_text(version)

    # Strip -dev from envoy's VERSION.txt. Bazel fetches envoy source via
    # http_archive in WORKSPACE; adding patch_cmds tells it to fix the version
    # after downloading. This changes the proxy repo's WORKSPACE (not envoy's).
    workspace = proxy_dir / "WORKSPACE"
    ws_content = workspace.read_text()
    if "patch_cmds" not in ws_content:
        workspace.write_text(
            ws_content.replace(
                'url = "https://github.com/" + ENVOY_ORG + "/" + ENVOY_REPO'
                ' + "/archive/" + ENVOY_SHA + ".tar.gz",',
                'url = "https://github.com/" + ENVOY_ORG + "/" + ENVOY_REPO'
                ' + "/archive/" + ENVOY_SHA + ".tar.gz",\n'
                '    patch_cmds = ["sed -i s/-dev// VERSION.txt"],',
            )
        )

    # https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/security/ssl#fips-140-2
    bazelrc_lines = ["build --define boringssl=fips"]
    disk_cache = os.environ.get("BAZEL_DISK_CACHE")
    if disk_cache:
        bazelrc_lines.append(f"build --disk_cache={disk_cache}")
    (proxy_dir / "user.bazelrc").write_text("\n".join(bazelrc_lines) + "\n")

    if timeout_minutes is None:
        run("make build_envoy", cwd=proxy_dir)
        return True

    # Run with timeout — gracefully kill Bazel so disk cache is saved
    cmd = "make build_envoy"
    print(f"+ {cmd}  (timeout: {timeout_minutes}m)")
    proc = subprocess.Popen(cmd, shell=True, cwd=proxy_dir, start_new_session=True)
    try:
        proc.wait(timeout=timeout_minutes * 60)
        if proc.returncode != 0:
            sys.exit(f"Build failed with exit code {proc.returncode}")
        return True
    except subprocess.TimeoutExpired:
        print(f"\n=== Build timeout reached ({timeout_minutes}m) ===")
        print("Sending SIGTERM to let Bazel save cache...")
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            print("Sending SIGKILL...")
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        return False


# ---------------------------------------------------------------------------
# Build Istio control-plane and data-plane images
# ---------------------------------------------------------------------------


def build_istio(version: str, build_hub: str, tags: str, arch: str) -> None:
    print("\n=== Building Istio images ===\n")
    run(
        f"git clone https://github.com/istio/istio.git --depth 1 --branch {version} --single-branch"
    )

    istio_dir = Path("istio").resolve()
    apply_version_patches("istio", version, istio_dir)

    # Compute output dirs matching what setup_env.sh would produce for this arch.
    # We must use absolute paths since these are passed as Make command-line overrides
    # to work around setup_env.sh running via $(shell) before Make vars are available.
    target_out = istio_dir / f"out/linux_{arch}"
    target_out_linux = target_out

    # Place pre-built envoy so the Istio build doesn't download from GCS
    deps = json.loads((istio_dir / "istio.deps").read_text())
    proxy_sha = next(d["lastStableSHA"] for d in deps if d["name"] == "PROXY_REPO_SHA")

    release_dir = target_out / "release"
    release_dir.mkdir(parents=True, exist_ok=True)

    envoy_src = Path("proxy/bazel-bin/envoy")
    for dest in (release_dir / f"envoy-{proxy_sha}", release_dir / "envoy"):
        dest.write_bytes(envoy_src.read_bytes())
        dest.chmod(0o755)

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

    # Build base images with apko (using our own configs with wolfi-baselayout and PATH)
    go_bin = Path.home() / "go/bin"
    env = {**os.environ, "PATH": f"{go_bin}:{os.environ['PATH']}"}
    run(
        f"apko publish --arch={arch} {SCRIPT_DIR}/iptables.yaml {build_hub}/iptables:{tags}",
        env=env,
        cwd="istio",
    )
    run(
        f"apko publish --arch={arch} {SCRIPT_DIR}/distroless.yaml {build_hub}/distroless:{tags}",
        env=env,
        cwd="istio",
    )

    # Build pilot and proxyv2
    # Pass TARGET_OUT and TARGET_OUT_LINUX as Make command-line overrides because
    # Istio's Makefile computes them via $(shell setup_env.sh) which runs before
    # command-line variables are available, defaulting to uname -m (host arch).
    build_env = {
        **env,
        "VERSION": version,
        "IGNORE_DIRTY_TREE": "1",
        "TAG": tags,
        "BASE_VERSION": tags,
        "ISTIO_BASE_REGISTRY": build_hub,
        "HUB": build_hub,
        "DOCKER_BUILD_VARIANTS": "distroless",
        "DOCKER_ARCHITECTURES": f"linux/{arch}",
    }
    run(
        f"make TARGET_OS=linux TARGET_ARCH={arch} GOARCH={arch}"
        f" TARGET_OUT={target_out} TARGET_OUT_LINUX={target_out_linux}"
        f" docker.proxyv2 docker.pilot",
        env=build_env,
        cwd="istio",
    )


# ---------------------------------------------------------------------------
# Verify, tag, and push
# ---------------------------------------------------------------------------


def verify_images(build_hub: str, tags: str) -> None:
    print("\n=== Verifying built images ===\n")
    run(
        f'docker run --rm --entrypoint="" {build_hub}/proxyv2:{tags}-distroless envoy --version',
    )
    run(
        f'docker run --rm --entrypoint="" {build_hub}/proxyv2:{tags}-distroless pilot-agent version',
    )
    run(
        f'docker run --rm --entrypoint="" {build_hub}/pilot:{tags}-distroless pilot-discovery version',
    )


def tag_and_push(
    build_hub: str, export_hub: str, tags: str, minor_tag: str, arch: str
) -> None:
    print("\n=== Tagging and pushing images ===\n")
    # Authenticate to GHCR if applicable
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if export_hub.startswith("ghcr.io/") and github_token:
        print("Authenticating with GitHub Container Registry...")
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
        f.write(f"""## Istio FIPS Build Summary ({arch})

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
""")


# ===========================================================================
# Main
# ===========================================================================

STAGES = ("envoy", "istio", "all")


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
    parser.add_argument(
        "--stage",
        dest="stage",
        default="all",
        choices=STAGES,
        help="Build stage: 'envoy' (compile proxy only), 'istio' (images only, "
        "expects envoy binary at proxy/bazel-bin/envoy), 'all' (default).",
    )
    parser.add_argument(
        "--timeout",
        dest="timeout",
        type=int,
        default=None,
        help="Envoy build timeout in minutes. Exits gracefully to save cache.",
    )
    args = parser.parse_args()

    # Resolve version and arch
    version = resolve_version(args.version)
    arch = args.arch or detect_arch()
    stage = args.stage
    print(f"Istio version: {version}")
    print(f"Architecture: {arch}")
    print(f"Stage: {stage}")

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
            "BAZEL_BUILD_ARGS": "--config=release --verbose_failures --local_ram_resources=HOST_RAM*.8 --worker_max_instances=2 --discard_analysis_cache",
        }
    )

    print("\n=== Build configuration ===\n")
    config: dict[str, str] = {
        "ISTIO_VERSION": version,
        "ARCH": arch,
        "STAGE": stage,
        "MAJOR_ISTIO_VERSION": major_version,
        "TAGS": tags,
        "BUILD_HUB": build_hub,
        "EXPORT_HUB": export_hub,
    }
    for k, v in config.items():
        print(f"  {k}={v}")

    # Stage: envoy — just compile the proxy, no docker needed
    if stage in ("envoy", "all"):
        completed = build_envoy(version, timeout_minutes=args.timeout)
        if not completed:
            print("Build will resume from cache on next run.")
            return

    if stage == "envoy":
        print("Envoy binary ready at proxy/bazel-bin/envoy")
        return

    # Stage: istio — needs envoy binary, builds and pushes images
    envoy_bin = Path("proxy/bazel-bin/envoy")
    if not envoy_bin.exists():
        sys.exit(
            f"Error: {envoy_bin} not found. "
            "Run with --stage envoy first, or use --stage all."
        )

    start_registry()
    build_istio(version, build_hub, tags, arch)
    tag_and_push(build_hub, export_hub, tags, minor_tag, arch)
    verify_images(build_hub, tags)
    write_summary(export_hub, version, tags, minor_tag, arch)

    print(f"Done. Images pushed to {export_hub}")


if __name__ == "__main__":
    main()
