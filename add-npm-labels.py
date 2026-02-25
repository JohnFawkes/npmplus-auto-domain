#!/usr/bin/env python3
"""
add-npm-labels.py  —  Inject NPMplus Docker labels into compose files from a .env.

Reads per-service label config from a .env file and adds (or updates) the
npm.* labels on matching services in one or more Docker Compose files.
Existing non-npm labels are left untouched.  Stale npm.* labels that are no
longer in the .env are removed.

Usage
-----
  # Edit a single compose file; reads .env from the current directory
  python add-npm-labels.py --compose docker-compose.yaml

  # Specify both an env file and a compose file
  python add-npm-labels.py --env prod.env --compose /srv/app/compose.yaml

  # Scan an entire directory tree and edit every compose file found
  python add-npm-labels.py --env labels.env --scan /home/user/projects

  # Preview changes without writing anything
  python add-npm-labels.py --compose compose.yaml --dry-run

.env format
-----------
  NPM_<SERVICE>_DOMAIN=app.example.com       # required to enable the service
  NPM_<SERVICE>_PORT=3000                    # optional
  NPM_<SERVICE>_SCHEME=http                  # optional, default http
  NPM_<SERVICE>_SSL=create                   # optional: "create" or cert name from NPMplus UI
  NPM_<SERVICE>_FORCE_HTTPS=true             # optional
  NPM_<SERVICE>_ENABLE=true                  # optional, defaults to true when DOMAIN is set
  NPM_<SERVICE>_IP=192.168.1.10              # optional
  NPM_<SERVICE>_CONTAINERNAME=false          # optional

  <SERVICE> is the compose service name in UPPERCASE.
  Underscores in service names are supported (e.g. NPM_MY_APP_DOMAIN for service my_app).

Example .env
------------
  NPM_PORTTRACKER_DOMAIN=porttracker.example.com
  NPM_PORTTRACKER_PORT=8080
  NPM_PORTTRACKER_SSL=*.example.com
  NPM_PORTTRACKER_FORCE_HTTPS=true

  NPM_MY_APP_DOMAIN=myapp.example.com
  NPM_MY_APP_PORT=3000
  NPM_MY_APP_SSL=create
  NPM_MY_APP_FORCE_HTTPS=true

YAML library
------------
  ruamel.yaml is preferred because it preserves comments and formatting.
  PyYAML is used as a fallback but will reformat the file.
  Install ruamel.yaml for best results:  pip install ruamel.yaml
"""

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# YAML library detection
# ---------------------------------------------------------------------------
try:
    from ruamel.yaml import YAML as _RuamelYAML
    from ruamel.yaml.comments import CommentedMap as _CommentedMap
    _RUAMEL = True
except ImportError:
    _RUAMEL = False

try:
    import yaml as _pyyaml
    _PYYAML = True
except ImportError:
    _PYYAML = False

if not _RUAMEL and not _PYYAML:
    print(
        "ERROR: No YAML library found.\n"
        "Install one with:  pip install ruamel.yaml",
        file=sys.stderr,
    )
    sys.exit(1)

if not _RUAMEL:
    print(
        "WARNING: ruamel.yaml not installed — falling back to PyYAML.\n"
        "         Comments and formatting in compose files will be lost.\n"
        "         Install ruamel.yaml for best results:  pip install ruamel.yaml\n",
        file=sys.stderr,
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COMPOSE_FILENAMES = frozenset({
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
})

# env-var suffix → npm label name
# Longer suffixes must come first so "FORCE_HTTPS" is matched before "HTTPS"
# would (if it existed), and "CONTAINERNAME" before "NAME" (if it existed).
LABEL_SETTINGS: dict[str, str] = {
    "FORCE_HTTPS":   "npm.force_https",
    "CONTAINERNAME": "npm.containername",
    "SCHEME":        "npm.scheme",
    "DOMAIN":        "npm.domain",
    "ENABLE":        "npm.enable",
    "PORT":          "npm.port",
    "SSL":           "npm.ssl",
    "IP":            "npm.ip",
}

# ---------------------------------------------------------------------------
# .env parsing
# ---------------------------------------------------------------------------

def _strip_inline_comment(value: str) -> str:
    """Remove a trailing inline comment (space/tab + #) from a value string."""
    for sep in (" #", "\t#"):
        idx = value.find(sep)
        if idx != -1:
            value = value[:idx]
    return value.strip()


def parse_env_file(env_path: Path) -> dict[str, dict[str, str]]:
    """
    Parse *env_path* and return:
        { lowercase_service_name: { "npm.label": "value", ... }, ... }

    Only NPM_<SERVICE>_<SETTING>=<value> lines are processed.
    """
    services: dict[str, dict[str, str]] = {}

    with env_path.open() as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, _, value = line.partition("=")
            key = key.strip()

            # Strip surrounding quotes then any trailing inline comment
            value = value.strip()
            if value and value[0] in ('"', "'"):
                value = value.strip(value[0])
            value = _strip_inline_comment(value)

            if not key.startswith("NPM_"):
                continue

            rest = key[4:]  # strip leading "NPM_"

            # Match the known setting suffix from the right so that
            # underscores in the service name are handled correctly.
            label_name = None
            svc_upper = None
            for suffix, lbl in LABEL_SETTINGS.items():
                if rest.endswith("_" + suffix):
                    svc_upper = rest[: -(len(suffix) + 1)]
                    label_name = lbl
                    break

            if svc_upper is None:
                print(
                    f"  WARNING: line {lineno}: unrecognised NPM_ variable '{key}' — skipping",
                    file=sys.stderr,
                )
                continue

            if not svc_upper:
                print(
                    f"  WARNING: line {lineno}: empty service name in '{key}' — skipping",
                    file=sys.stderr,
                )
                continue

            svc_key = svc_upper.lower()
            services.setdefault(svc_key, {})[label_name] = value

    # Auto-set npm.enable=true for any service that has a domain configured
    for svc in services.values():
        if "npm.domain" in svc:
            svc.setdefault("npm.enable", "true")

    return services


# ---------------------------------------------------------------------------
# YAML I/O helpers
# ---------------------------------------------------------------------------

def _load_yaml(path: Path):
    """Return (yaml_engine, parsed_data).  yaml_engine is None for PyYAML."""
    if _RUAMEL:
        yml = _RuamelYAML()
        yml.preserve_quotes = True
        with path.open() as fh:
            return yml, yml.load(fh)
    with path.open() as fh:
        return None, _pyyaml.safe_load(fh)


def _dump_yaml(path: Path, yml_engine, data) -> None:
    if _RUAMEL:
        with path.open("w") as fh:
            yml_engine.dump(data, fh)
    else:
        with path.open("w") as fh:
            _pyyaml.dump(
                data, fh,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )


def _labels_to_dict(raw) -> dict[str, str]:
    """Normalise compose labels to a plain dict (handles both list and dict formats)."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    # List format: ["key=value", "flag-only-label"]
    result = {}
    for item in raw:
        k, _, v = str(item).partition("=")
        result[k] = v
    return result


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def apply_to_compose(
    compose_path: Path,
    service_labels: dict[str, dict[str, str]],
    dry_run: bool,
) -> int:
    """
    Add/update/remove npm.* labels on matching services in *compose_path*.
    Returns the number of services that were (or would be) changed.
    """
    yml_engine, data = _load_yaml(compose_path)

    if not isinstance(data, dict):
        print(f"  SKIP — not a valid YAML mapping", file=sys.stderr)
        return 0

    services_block = data.get("services") or {}
    if not services_block:
        print(f"  SKIP — no 'services' block found")
        return 0

    modified = 0

    for svc_name, svc_cfg in services_block.items():
        desired = service_labels.get(str(svc_name).lower())
        if not desired:
            continue

        # svc_cfg can be None when a service is declared but has no keys
        if svc_cfg is None:
            svc_cfg = {}
            services_block[svc_name] = svc_cfg

        current = _labels_to_dict(svc_cfg.get("labels"))
        changed = {k: v for k, v in desired.items() if current.get(k) != v}
        removed = [k for k in current if k.startswith("npm.") and k not in desired]

        if not changed and not removed:
            print(f"  {svc_name}: up-to-date")
            continue

        # Report changes
        for k, v in sorted(changed.items()):
            old = current.get(k, "<not set>")
            print(f"  {svc_name}: {k}  {old!r} → {v!r}")
        for k in sorted(removed):
            print(f"  {svc_name}: {k}  {current[k]!r} → <removed>")

        if not dry_run:
            _write_labels(svc_cfg, current, desired)

        modified += 1

    if not dry_run and modified:
        _dump_yaml(compose_path, yml_engine, data)

    return modified


def _write_labels(svc_cfg, current: dict, desired: dict) -> None:
    """Mutate svc_cfg['labels'] to reflect the desired npm.* labels."""
    existing_block = svc_cfg.get("labels")

    if _RUAMEL and isinstance(existing_block, _CommentedMap):
        # Preserve ruamel structure so comments and ordering are kept
        # Remove stale npm keys
        for k in [k for k in list(existing_block.keys())
                  if k.startswith("npm.") and k not in desired]:
            del existing_block[k]
        # Add / update
        for k, v in desired.items():
            existing_block[k] = v
    else:
        # Rebuild: non-npm labels unchanged, npm labels replaced in full
        new_labels = {k: v for k, v in current.items() if not k.startswith("npm.")}
        new_labels.update(desired)
        svc_cfg["labels"] = new_labels


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inject NPMplus labels into Docker Compose files from a .env config.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--env", default=".env", metavar="FILE",
        help="Path to the .env label config file (default: .env)",
    )

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--compose", metavar="FILE",
        help="Path to a single compose file to edit",
    )
    target.add_argument(
        "--scan", metavar="DIR",
        help="Scan this directory tree for compose files and edit all of them",
    )

    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing any files",
    )
    args = parser.parse_args()

    # Locate .env
    env_path = Path(args.env)
    if not env_path.exists():
        print(f"ERROR: env file not found: {env_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading label config from: {env_path}")
    service_labels = parse_env_file(env_path)
    if not service_labels:
        print("No NPM_* label entries found in the env file — nothing to do.")
        sys.exit(0)

    print(f"Services configured: {', '.join(sorted(service_labels))}")
    if args.dry_run:
        print("DRY RUN — no files will be modified")
    print()

    # Collect compose files
    if args.compose:
        compose_files = [Path(args.compose)]
        if not compose_files[0].exists():
            print(f"ERROR: compose file not found: {compose_files[0]}", file=sys.stderr)
            sys.exit(1)
    else:
        scan_root = Path(args.scan)
        if not scan_root.is_dir():
            print(f"ERROR: scan target is not a directory: {scan_root}", file=sys.stderr)
            sys.exit(1)
        compose_files = sorted(
            p for p in scan_root.rglob("*") if p.name in COMPOSE_FILENAMES
        )
        if not compose_files:
            print(f"No compose files found under {scan_root}")
            sys.exit(0)
        print(f"Found {len(compose_files)} compose file(s)\n")

    total = 0
    for cf in compose_files:
        print(f"{cf}:")
        total += apply_to_compose(cf, service_labels, dry_run=args.dry_run)
        print()

    action = "would be modified" if args.dry_run else "modified"
    print(f"Done — {total} service(s) {action}.")


if __name__ == "__main__":
    main()
