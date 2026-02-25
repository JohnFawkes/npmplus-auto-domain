#!/usr/bin/env python3
"""
add-npm-labels.py  —  Inject NPMplus Docker labels into Docker Compose services.

Two modes
---------

  --from-npm   Connect to the NPMplus API, read every existing proxy host,
               and match them to services in compose files by forward_host
               name.  No separate label config file needed — the data comes
               straight from NPMplus.

  --env FILE   Read per-service label config from a .env file (original mode).

Both modes accept --compose FILE or --scan DIR and support --dry-run.

--from-npm usage
----------------
  # Scan all compose files under /srv; read NPMplus creds from env vars
  python add-npm-labels.py --from-npm --scan /srv

  # Override the NPMplus host for this run
  python add-npm-labels.py --from-npm --npm-host 192.168.1.10:81 --scan /srv

  # Read credentials from a .env file instead of environment variables
  python add-npm-labels.py --from-npm --env /srv/.env --scan /srv

  # Dry-run — show what would be written without touching any file
  python add-npm-labels.py --from-npm --scan /srv --dry-run

--from-npm credentials (checked in this order)
----------------------------------------------
  1. CLI flags          --npm-host / --npm-user / --npm-pass / --npm-https
  2. Environment vars   NPMPLUS_HOST / NPMPLUS_USER / NPMPLUS_PASS / NPMPLUS_HTTPS
  3. .env file          same NPMPLUS_* variable names (given with --env)

Matching logic
--------------
  NPMplus stores forward_host (usually the Docker container / service name).
  A compose service matches a proxy host when the service name equals the
  proxy host's forward_host (case-insensitive).
  Proxy hosts whose forward_host looks like an IP address are listed as
  unmatched after the run.

--env format (original mode)
----------------------------
  NPM_<SERVICE>_DOMAIN=app.example.com       # required to enable the service
  NPM_<SERVICE>_PORT=3000                    # optional
  NPM_<SERVICE>_SCHEME=http                  # optional, default http
  NPM_<SERVICE>_SSL=create                   # optional: "create" or cert name
  NPM_<SERVICE>_FORCE_HTTPS=true             # optional
  NPM_<SERVICE>_ENABLE=true                  # optional, defaults to true
  NPM_<SERVICE>_IP=192.168.1.10              # optional
  NPM_<SERVICE>_CONTAINERNAME=false          # optional

  <SERVICE> is the compose service name in UPPERCASE.
  Underscores in service names are supported (NPM_MY_APP_DOMAIN → my_app).

YAML library
------------
  ruamel.yaml is preferred — it preserves comments and formatting.
  PyYAML is used as a fallback but will reformat the file.
  pip install ruamel.yaml
"""

import argparse
import os
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
# Longer suffixes must come first so e.g. FORCE_HTTPS is matched before
# a hypothetical HTTPS would be.
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
# .env / environment helpers
# ---------------------------------------------------------------------------

def _strip_inline_comment(value: str) -> str:
    """Remove a trailing inline comment (space/tab + #) from a value string."""
    for sep in (" #", "\t#"):
        idx = value.find(sep)
        if idx != -1:
            value = value[:idx]
    return value.strip()


def _read_dotenv(env_path: Path) -> dict[str, str]:
    """Return a plain {KEY: value} dict for every assignment in *env_path*."""
    result: dict[str, str] = {}
    with env_path.open() as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if value and value[0] in ('"', "'"):
                value = value.strip(value[0])
            result[key] = _strip_inline_comment(value)
    return result


def parse_env_file(env_path: Path) -> dict[str, dict[str, str]]:
    """
    Parse *env_path* for NPM_<SERVICE>_<SETTING> entries and return:
        { lowercase_service_name: { "npm.label": "value", ... }, ... }
    """
    services: dict[str, dict[str, str]] = {}
    raw = _read_dotenv(env_path)

    for lineno, (key, value) in enumerate(raw.items(), 1):
        if not key.startswith("NPM_"):
            continue

        rest = key[4:]  # strip leading "NPM_"

        label_name = None
        svc_upper = None
        for suffix, lbl in LABEL_SETTINGS.items():
            if rest.endswith("_" + suffix):
                svc_upper = rest[: -(len(suffix) + 1)]
                label_name = lbl
                break

        if svc_upper is None:
            print(
                f"  WARNING: unrecognised NPM_ variable '{key}' — skipping",
                file=sys.stderr,
            )
            continue

        if not svc_upper:
            print(
                f"  WARNING: empty service name in '{key}' — skipping",
                file=sys.stderr,
            )
            continue

        svc_key = svc_upper.lower()
        services.setdefault(svc_key, {})[label_name] = value

    for svc in services.values():
        if "npm.domain" in svc:
            svc.setdefault("npm.enable", "true")

    return services


# ---------------------------------------------------------------------------
# NPMplus API client  (used only by --from-npm mode)
# ---------------------------------------------------------------------------

def _get_credentials(args, env_path: Path | None) -> tuple[str, str, str, bool]:
    """
    Resolve NPMplus credentials.  Priority:
      1. CLI flags (--npm-host / --npm-user / --npm-pass / --npm-https)
      2. Environment variables (NPMPLUS_HOST / _USER / _PASS / _HTTPS)
      3. Values read from *env_path* (same NPMPLUS_* variable names)
    """
    # Start with any .env file values
    file_vals: dict[str, str] = {}
    if env_path and env_path.exists():
        file_vals = _read_dotenv(env_path)

    def _get(cli_val, env_key: str, default: str = "") -> str:
        if cli_val:
            return cli_val
        if os.environ.get(env_key):
            return os.environ[env_key].strip()
        return file_vals.get(env_key, default).strip()

    host = _get(getattr(args, "npm_host", None), "NPMPLUS_HOST")
    user = _get(getattr(args, "npm_user", None), "NPMPLUS_USER")
    passwd = _get(getattr(args, "npm_pass", None), "NPMPLUS_PASS")

    # --npm-https flag or NPMPLUS_HTTPS env / file var
    use_https = getattr(args, "npm_https", False)
    if not use_https:
        raw_https = _get(None, "NPMPLUS_HTTPS", "false")
        use_https = raw_https.lower() in ("true", "1", "yes")

    return host, user, passwd, use_https


def _connect_npm(host: str, user: str, passwd: str, use_https: bool):
    """
    Authenticate with NPMplus and return (requests.Session, api_base_url).
    Handles HTTP → HTTPS redirect automatically.
    """
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        print(
            "ERROR: 'requests' library is required for --from-npm mode.\n"
            "Install it with:  pip install requests",
            file=sys.stderr,
        )
        sys.exit(1)

    scheme = "https" if use_https else "http"
    api_base = f"{scheme}://{host}/api"

    session = requests.Session()
    session.verify = False

    try:
        r = session.post(
            f"{api_base}/tokens",
            json={"identity": user, "secret": passwd},
            timeout=15,
            allow_redirects=False,
        )
        # Auto-upgrade to HTTPS when NPMplus issues a redirect
        if r.status_code in (301, 302, 307, 308):
            location = r.headers.get("Location", "")
            if location.startswith("https://"):
                api_base = api_base.replace("http://", "https://", 1)
                r = session.post(
                    f"{api_base}/tokens",
                    json={"identity": user, "secret": passwd},
                    timeout=15,
                )
        r.raise_for_status()
    except Exception as exc:
        print(f"ERROR: NPMplus authentication failed: {exc}", file=sys.stderr)
        sys.exit(1)

    return session, api_base


def _fetch_proxy_hosts(session, api_base: str) -> list[dict]:
    r = session.get(f"{api_base}/nginx/proxy-hosts", timeout=15)
    r.raise_for_status()
    return r.json()


def _fetch_certs(session, api_base: str) -> dict[int, str]:
    """Return {cert_id: nice_name} for every certificate in NPMplus."""
    r = session.get(f"{api_base}/nginx/certificates", timeout=15)
    r.raise_for_status()
    return {c["id"]: c.get("nice_name", "") for c in r.json()}


def npm_hosts_to_service_labels(
    proxy_hosts: list[dict],
    certs: dict[int, str],
) -> tuple[dict[str, dict[str, str]], list[dict]]:
    """
    Convert NPMplus proxy-host objects into the service_labels format used
    by apply_to_compose().

    Returns:
        service_labels  — { forward_host_lower: { "npm.*": value, ... }, ... }
        unmatched       — proxy host objects whose forward_host looks like an
                          IP address (can't be matched to a compose service name)
    """
    service_labels: dict[str, dict[str, str]] = {}
    unmatched: list[dict] = []

    for host in proxy_hosts:
        forward_host = (host.get("forward_host") or "").strip()
        domain_names = host.get("domain_names") or []

        if not forward_host or not domain_names:
            continue

        # Detect IP addresses — these can't be matched by service name
        import re
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", forward_host):
            unmatched.append(host)
            continue

        labels: dict[str, str] = {
            "npm.enable": "true",
            "npm.domain": domain_names[0],
            "npm.port":   str(host.get("forward_port", "")),
            "npm.scheme": host.get("forward_scheme") or "http",
        }

        if host.get("ssl_forced"):
            labels["npm.force_https"] = "true"

        cert_id = host.get("certificate_id") or 0
        if cert_id and cert_id in certs and certs[cert_id]:
            labels["npm.ssl"] = certs[cert_id]

        service_labels[forward_host.lower()] = labels

    return service_labels, unmatched


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
    """Normalise compose labels to a plain dict (handles list and dict formats)."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    result = {}
    for item in raw:
        k, _, v = str(item).partition("=")
        result[k] = v
    return result


# ---------------------------------------------------------------------------
# Core apply logic  (shared by both modes)
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
    try:
        yml_engine, data = _load_yaml(compose_path)
    except Exception as exc:
        print(f"  SKIP — YAML parse error: {exc}", file=sys.stderr)
        return 0

    if not isinstance(data, dict):
        print("  SKIP — not a valid YAML mapping", file=sys.stderr)
        return 0

    services_block = data.get("services") or {}
    if not services_block:
        print("  SKIP — no 'services' block found")
        return 0

    modified = 0

    for svc_name, svc_cfg in services_block.items():
        desired = service_labels.get(str(svc_name).lower())
        if not desired:
            continue

        if svc_cfg is None:
            svc_cfg = {}
            services_block[svc_name] = svc_cfg

        current = _labels_to_dict(svc_cfg.get("labels"))
        changed = {k: v for k, v in desired.items() if current.get(k) != v}
        removed = [k for k in current if k.startswith("npm.") and k not in desired]

        if not changed and not removed:
            print(f"  {svc_name}: up-to-date")
            continue

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
        for k in [k for k in list(existing_block.keys())
                  if k.startswith("npm.") and k not in desired]:
            del existing_block[k]
        for k, v in desired.items():
            existing_block[k] = v
    else:
        new_labels = {k: v for k, v in current.items() if not k.startswith("npm.")}
        new_labels.update(desired)
        svc_cfg["labels"] = new_labels


def _collect_compose_files(args) -> list[Path]:
    """Return the list of compose files to process based on CLI args."""
    if args.compose:
        p = Path(args.compose)
        if not p.exists():
            print(f"ERROR: compose file not found: {p}", file=sys.stderr)
            sys.exit(1)
        return [p]

    scan_root = Path(args.scan)
    if not scan_root.is_dir():
        print(f"ERROR: scan target is not a directory: {scan_root}", file=sys.stderr)
        sys.exit(1)
    files = sorted(p for p in scan_root.rglob("*") if p.name in COMPOSE_FILENAMES)
    if not files:
        print(f"No compose files found under {scan_root}")
        sys.exit(0)
    print(f"Found {len(files)} compose file(s)\n")
    return files


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inject NPMplus labels into Docker Compose files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Source of label data — mutually exclusive modes
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--from-npm", action="store_true",
        help="Fetch proxy host config from the NPMplus API (no .env label config needed)",
    )
    mode.add_argument(
        "--env", metavar="FILE",
        help="Read per-service label config from this .env file",
    )

    # Target compose file(s)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--compose", metavar="FILE", help="Single compose file to edit")
    target.add_argument("--scan", metavar="DIR", help="Directory tree to scan for compose files")

    # NPMplus credentials (--from-npm mode; also used to locate .env for credentials)
    npm = parser.add_argument_group("NPMplus credentials (--from-npm)")
    npm.add_argument("--npm-env", metavar="FILE", default=".env",
                     help="Load NPMPLUS_* credentials from this file (default: .env)")
    npm.add_argument("--npm-host", metavar="HOST",
                     help="NPMplus host, e.g. 192.168.1.10:81  (overrides NPMPLUS_HOST)")
    npm.add_argument("--npm-user", metavar="EMAIL",
                     help="NPMplus admin e-mail  (overrides NPMPLUS_USER)")
    npm.add_argument("--npm-pass", metavar="PASS",
                     help="NPMplus admin password  (overrides NPMPLUS_PASS)")
    npm.add_argument("--npm-https", action="store_true",
                     help="Connect to NPMplus over HTTPS  (overrides NPMPLUS_HTTPS)")

    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing any files")

    args = parser.parse_args()

    # ── --from-npm mode ─────────────────────────────────────────────────────
    if args.from_npm:
        env_path = Path(args.npm_env)
        host, user, passwd, use_https = _get_credentials(args, env_path if env_path.exists() else None)

        missing = [n for n, v in [("host", host), ("user", user), ("pass", passwd)] if not v]
        if missing:
            print(
                f"ERROR: NPMplus credentials missing: {', '.join(missing)}\n"
                f"Set NPMPLUS_HOST / NPMPLUS_USER / NPMPLUS_PASS in the environment,\n"
                f"in a .env file (--npm-env), or via --npm-host / --npm-user / --npm-pass.",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"Connecting to NPMplus at {host} …")
        session, api_base = _connect_npm(host, user, passwd, use_https)
        print("Authenticated.")

        print("Fetching proxy hosts …")
        proxy_hosts = _fetch_proxy_hosts(session, api_base)
        print(f"  {len(proxy_hosts)} proxy host(s) found")

        print("Fetching certificates …")
        certs = _fetch_certs(session, api_base)
        print(f"  {len(certs)} certificate(s) found\n")

        service_labels, unmatched = npm_hosts_to_service_labels(proxy_hosts, certs)
        print(f"Proxy hosts matchable by service name: {len(service_labels)}")
        for fwd_host, labels in sorted(service_labels.items()):
            print(f"  {fwd_host:30s}  →  {labels.get('npm.domain', '?')}")

        if unmatched:
            print(f"\nProxy hosts with IP forward_host (skipped — cannot match to a service name):")
            for h in unmatched:
                domains = ", ".join(h.get("domain_names") or [])
                print(f"  {h.get('forward_host')}  ({domains})")

        if not service_labels:
            print("\nNo matchable proxy hosts — nothing to do.")
            sys.exit(0)

    # ── --env mode ───────────────────────────────────────────────────────────
    else:
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
        print("\nDRY RUN — no files will be modified")
    print()

    # ── Apply to compose files ───────────────────────────────────────────────
    compose_files = _collect_compose_files(args)

    total = 0
    for cf in compose_files:
        print(f"{cf}:")
        total += apply_to_compose(cf, service_labels, dry_run=args.dry_run)
        print()

    action = "would be modified" if args.dry_run else "modified"
    print(f"Done — {total} service(s) {action}.")


if __name__ == "__main__":
    main()
