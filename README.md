# istio-fips

FIPS-compliant Istio images built with BoringSSL (Envoy) and BoringCrypto (Go).

## What this builds

- **proxyv2** — Envoy compiled with `boringssl=fips`, pilot-agent with `GOEXPERIMENT=boringcrypto`
- **pilot** — pilot-discovery with `GOEXPERIMENT=boringcrypto`

Images are distroless (Wolfi-based) and published to `ghcr.io/danielewood/istio-fips`.

## How it works

1. `discover.py` checks [endoflife.date](https://endoflife.date/istio) for supported Istio versions, then checks GHCR to skip versions that already have images
2. `build.py` clones the Istio proxy and control plane repos for a given version, patches them for FIPS, and builds everything in two stages:
   - **envoy** — compiles the Envoy proxy with `boringssl=fips` via Bazel (the slow part, ~2-5 hours)
   - **istio** — builds Go control-plane binaries with `GOEXPERIMENT=boringcrypto`, assembles distroless images, and pushes to GHCR

## CI workflow

The GitHub Actions workflow (`.github/workflows/build.yaml`) splits envoy and istio into separate jobs. Manual dispatch supports filtering by version and architecture.

The envoy build uses Bazel disk caching (`bazel-contrib/setup-bazel`) so that timed-out builds save progress and resume on re-trigger.

## Local usage

```sh
# Build a specific version (both stages)
uv run ./build.py --version 1.29.0

# Build just envoy (useful for iteration)
uv run ./build.py --version 1.29.0 --stage envoy

# Build just istio images (expects envoy binary at proxy/bazel-bin/envoy)
uv run ./build.py --version 1.29.0 --stage istio

# Build the latest release
uv run ./build.py --version latest

# Target a specific architecture
uv run ./build.py --version 1.29.0 --arch arm64

# Push to a custom registry
EXPORT_HUB=myregistry.example.com/istio uv run ./build.py --version 1.29.0
```

## Images

```sh
docker pull ghcr.io/danielewood/istio-fips/proxyv2:1.29-fips
docker pull ghcr.io/danielewood/istio-fips/pilot:1.29-fips
```
