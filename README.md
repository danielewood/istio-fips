# istio-fips

FIPS-compliant Istio images built with BoringSSL (Envoy) and BoringCrypto (Go).

## What this builds

- **proxyv2** — Envoy compiled with `boringssl=fips`, pilot-agent with `GOEXPERIMENT=boringcrypto`
- **pilot** — pilot-discovery with `GOEXPERIMENT=boringcrypto`

Images are distroless (Wolfi-based) and published to `ghcr.io/danielewood/istio-fips`.

## How it works

1. `discover.py` checks [endoflife.date](https://endoflife.date/istio) for supported Istio versions, then checks GHCR to skip versions that already have images
2. `build.py` clones the Istio proxy and control plane repos for a given version, patches them for FIPS, builds everything, and pushes the images

## Automated builds

A GitHub Actions workflow (`.github/workflows/build.yaml`) runs daily and on push to `main`/`master`. It builds all supported Istio versions that don't already have images in the registry.

Manual builds can target a specific version via workflow dispatch.

## Local usage

```sh
# Build a specific version
uv run ./build.py --version 1.26.2

# Build the latest release
uv run ./build.py --version latest

# Push to a custom registry
EXPORT_HUB=myregistry.example.com/istio uv run ./build.py --version 1.26.2
```

## Images

```sh
docker pull ghcr.io/danielewood/istio-fips/proxyv2:1.26-fips
docker pull ghcr.io/danielewood/istio-fips/pilot:1.26-fips
```
