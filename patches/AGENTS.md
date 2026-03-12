# Upstream Patch Process

Use this directory for narrow, version-scoped fixes when an upstream Istio, proxy, Envoy, Bazel, or Go dependency issue breaks the build.

## Layout

`build.py` auto-applies patch files immediately after cloning a repo.

- Single patch file:
  - `patches/<repo>/<version>.diff`
  - `patches/<repo>/<version>.patch`
- Multiple ordered patch files:
  - `patches/<repo>/<version>/*.diff`
  - `patches/<repo>/<version>/*.patch`

`<repo>` is currently `proxy` or `istio`.

## Rules

- Patch the smallest possible surface.
- Scope every fix to the exact failing version unless multiple versions are proven to need the same change.
- Prefer fixing missing Bazel metadata or dependency declarations over downgrading dependencies.
- Do not put one-off rewrite logic in `build.py` if a checked-in patch can express the change.
- Leave a clear trail so the patch can be removed once upstream ships a fix.

## Triage

1. Find the failing job and download the raw log.
1. Grep for the first real compiler or Bazel error, not the final `Target //:envoy failed to build` wrapper.
1. Identify the exact module, package, and missing import or repository.
1. Check the pinned upstream source at the exact commit or tag used by the failing build.

Useful commands:

```bash
gh run list --repo danielewood/istio-fips --workflow build.yaml --limit 5
gh api repos/danielewood/istio-fips/actions/jobs/<job-id>/logs > /tmp/job.log
rg -n 'missing strict dependencies|No dependencies were provided|ERROR:' /tmp/job.log
```

If the failure is in an upstream Go module, inspect its imports and pinned dependency versions:

```bash
go mod download -json <module>@<version>
curl -fsSL https://raw.githubusercontent.com/<org>/<repo>/<sha>/<path>
```

## Choosing Where To Patch

- Patch `patches/proxy/<version>.diff` when the break is inside `istio/proxy` or inside the Envoy archive fetched by `istio/proxy`.
- Patch `patches/istio/<version>.diff` when the break is in `istio/istio`.
- If `proxy` fetches Envoy with `http_archive`, patch `proxy`'s `WORKSPACE` to add an Envoy patch file under `proxy/bazel/`, then modify Envoy from that inner patch.

This keeps the top-level build logic generic and the version-specific workaround local to the version that needs it.

## Implementation Pattern

For direct repo fixes:

1. Create `patches/<repo>/<version>.diff`.
1. Patch the cloned repo exactly as needed.

For nested Envoy fixes from `istio/proxy`:

1. Update `proxy`'s `WORKSPACE` patch list in `patches/proxy/<version>.diff`.
1. Add a new inner patch file under `proxy/bazel/NNNN-description.patch`.
1. Use the inner patch to modify Envoy's files after `http_archive` fetches the pinned Envoy source.

Prefer fixes like:

- adding missing `go_repository(...)` entries
- adding Gazelle `build_directives`
- adding missing Bazel deps

Avoid:

- broad dependency downgrades unless there is no cleaner option
- version-agnostic hacks in `build.py`
- unrelated cleanup in the same patch

## Validation

Validate both layers before pushing.

1. Patch loader:

```bash
python3 - <<'PY' /tmp/proxy
import sys
from pathlib import Path
import build
build.apply_version_patches('proxy', '1.29.1', Path(sys.argv[1]))
print('OK')
PY
```

1. Outer patch applies to a fresh clone:

```bash
git clone --depth 1 --branch 1.29.1 --single-branch https://github.com/istio/proxy.git /tmp/proxy
git -C /tmp/proxy apply --check patches/proxy/1.29.1.diff
git -C /tmp/proxy apply patches/proxy/1.29.1.diff
```

1. Inner patch applies to the pinned upstream source:

```bash
mkdir -p /tmp/envoy/bazel
curl -fsSL https://raw.githubusercontent.com/envoyproxy/envoy/<sha>/bazel/dependency_imports.bzl \
  > /tmp/envoy/bazel/dependency_imports.bzl
git -C /tmp/envoy apply --check /tmp/proxy/bazel/0006-afero-strict-deps.patch
```

1. Sanity-check local Python after any patch loader changes:

```bash
python3 -m py_compile build.py
```

Full builds are optional for patch authoring if they are too expensive, but apply checks are not optional.

## Updating An Existing Patch

When a rerun exposes the next missing dependency:

1. Expand the existing version patch instead of creating a second unrelated workaround.
1. Keep all related fixes for that upstream break in the same versioned patch chain.
1. Re-run the apply checks.
1. Trigger another workflow and repeat until the build passes or the failure changes domains.

## Removal

Once upstream fixes the issue:

1. Confirm the version builds without the local patch.
1. Delete the versioned patch file or directory.
1. Keep the process doc; remove only the workaround.
