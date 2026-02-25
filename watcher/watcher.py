#!/usr/bin/env python3
"""
NPMplus Auto Domain - Docker Label Watcher

Watches Docker container labels and automatically manages proxy hosts
in NPMplus (Nginx Proxy Manager Plus).

Labels watched on containers:
  npm.enable  - Set to "true" / "1" / "yes" to enable auto-proxying
  npm.domain  - Domain name for the proxy (required when npm.enable is set)
  npm.port    - Port to forward to (optional; auto-detected from ExposedPorts)
  npm.scheme  - Forward scheme: "http" or "https" (optional; defaults to "http")

Required environment variables:
  NPMPLUS_HOST  - NPMplus host, e.g. "192.168.1.10:81" or "npm.example.com"
  NPMPLUS_USER  - NPMplus admin e-mail
  NPMPLUS_PASS  - NPMplus admin password

Optional environment variables:
  NPMPLUS_HTTPS      - Use HTTPS to reach NPMplus API (default: false)
  DOCKER_HOST        - Docker socket-proxy URL (default: tcp://socket-proxy:2375)
  CLEANUP_ON_STOP    - Delete proxy hosts when a container stops (default: true)
  LOG_LEVEL          - Logging level: DEBUG / INFO / WARNING / ERROR (default: INFO)
"""

import json
import logging
import os
import sys
import time
from typing import Optional

import docker
import requests
import urllib3

# ---------------------------------------------------------------------------
# Suppress InsecureRequestWarning when verify=False is used for NPMplus HTTPS
# ---------------------------------------------------------------------------
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
NPMPLUS_HOST = os.environ.get("NPMPLUS_HOST", "").strip().rstrip("/")
NPMPLUS_USER = os.environ.get("NPMPLUS_USER", "").strip()
NPMPLUS_PASS = os.environ.get("NPMPLUS_PASS", "").strip()
NPMPLUS_HTTPS = os.environ.get("NPMPLUS_HTTPS", "false").lower() in ("true", "1", "yes")
DOCKER_HOST = os.environ.get("DOCKER_HOST", "tcp://socket-proxy:2375")
CLEANUP_ON_STOP = os.environ.get("CLEANUP_ON_STOP", "true").lower() in ("true", "1", "yes")
STATE_FILE = "/data/state.json"

# Validate required config
_missing = [k for k, v in [("NPMPLUS_HOST", NPMPLUS_HOST), ("NPMPLUS_USER", NPMPLUS_USER), ("NPMPLUS_PASS", NPMPLUS_PASS)] if not v]
if _missing:
    log.error("Missing required environment variables: %s", ", ".join(_missing))
    sys.exit(1)

_scheme = "https" if NPMPLUS_HTTPS else "http"
NPMPLUS_API = f"{_scheme}://{NPMPLUS_HOST}/api"

log.info("NPMplus API: %s", NPMPLUS_API)
log.info("Docker host: %s", DOCKER_HOST)
log.info("CLEANUP_ON_STOP: %s", CLEANUP_ON_STOP)

# ---------------------------------------------------------------------------
# Persisted state  (container_id -> proxy_host_id)
# ---------------------------------------------------------------------------
_state: dict[str, int] = {}


def _load_state() -> None:
    global _state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as fh:
                _state = json.load(fh)
            log.info("Loaded %d state entries from %s", len(_state), STATE_FILE)
    except Exception as exc:
        log.warning("Could not load state file (%s); starting fresh", exc)
        _state = {}


def _save_state() -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as fh:
            json.dump(_state, fh, indent=2)
    except Exception as exc:
        log.error("Could not save state file: %s", exc)


# ---------------------------------------------------------------------------
# NPMplus API helpers
# ---------------------------------------------------------------------------
_token: Optional[str] = None
_token_expires: float = 0.0


def _get_token() -> Optional[str]:
    """Return a valid NPMplus bearer token, refreshing if necessary."""
    global _token, _token_expires
    if _token and time.time() < _token_expires - 60:
        return _token
    try:
        r = requests.post(
            f"{NPMPLUS_API}/tokens",
            json={"identity": NPMPLUS_USER, "secret": NPMPLUS_PASS},
            timeout=15,
            verify=False,
        )
        r.raise_for_status()
        _token = r.json()["token"]
        _token_expires = time.time() + 86400  # tokens are valid for 1 day
        log.info("Authenticated with NPMplus")
        return _token
    except Exception as exc:
        log.error("NPMplus authentication failed: %s", exc)
        _token = None
        return None


def _npm_api(method: str, path: str, _retry: bool = True, **kwargs) -> Optional[requests.Response]:
    """Make an authenticated request to the NPMplus API."""
    token = _get_token()
    if not token:
        return None
    try:
        r = requests.request(
            method,
            f"{NPMPLUS_API}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            verify=False,
            **kwargs,
        )
        if r.status_code == 401 and _retry:
            # Token may have been invalidated server-side; force a refresh
            global _token
            _token = None
            return _npm_api(method, path, _retry=False, **kwargs)
        return r
    except Exception as exc:
        log.error("NPMplus API error (%s %s): %s", method, path, exc)
        return None


def _find_proxy_host(domain: str) -> Optional[dict]:
    """Return the NPMplus proxy-host object for *domain*, or None."""
    r = _npm_api("GET", "/nginx/proxy-hosts")
    if r and r.status_code == 200:
        for host in r.json():
            if domain in host.get("domain_names", []):
                return host
    return None


def _create_proxy_host(domain: str, forward_host: str, forward_port: int, scheme: str = "http") -> Optional[int]:
    """Create a proxy host in NPMplus. Returns the new host id on success."""
    payload = {
        "domain_names": [domain],
        "forward_scheme": scheme,
        "forward_host": forward_host,
        "forward_port": forward_port,
    }
    r = _npm_api("POST", "/nginx/proxy-hosts", json=payload)
    if r and r.status_code == 201:
        host_id = r.json().get("id")
        log.info(
            "Created proxy host: %s -> %s://%s:%d  (id=%s)",
            domain,
            scheme,
            forward_host,
            forward_port,
            host_id,
        )
        return host_id
    if r:
        log.error("Failed to create proxy host for %s: %s %s", domain, r.status_code, r.text)
    return None


def _delete_proxy_host(host_id: int) -> bool:
    """Delete a proxy host from NPMplus by id."""
    r = _npm_api("DELETE", f"/nginx/proxy-hosts/{host_id}")
    if r and r.status_code == 200:
        log.info("Deleted proxy host id=%d", host_id)
        return True
    if r:
        log.error("Failed to delete proxy host id=%d: %s %s", host_id, r.status_code, r.text)
    return False


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def _detect_port(container) -> Optional[int]:
    """
    Auto-detect the container's service port.

    Priority:
      1. Lowest port from ExposedPorts in the container Config
         (the container's own declared listening port, reachable via Docker DNS)
      2. First host-mapped port from NetworkSettings.Ports
         (useful if NPMplus is outside of Docker)
    """
    attrs = container.attrs

    # 1) ExposedPorts (e.g. {"80/tcp": None, "443/tcp": None})
    exposed = attrs.get("Config", {}).get("ExposedPorts") or {}
    if exposed:
        ports = sorted(int(p.split("/")[0]) for p in exposed.keys())
        log.debug("[%s] Auto-detected port %d from ExposedPorts", container.name, ports[0])
        return ports[0]

    # 2) Host-mapped ports  (e.g. {"8080/tcp": [{"HostPort": "32768"}]})
    bindings = attrs.get("NetworkSettings", {}).get("Ports") or {}
    for _port_proto, mapping in sorted(bindings.items(), key=lambda x: int(x[0].split("/")[0])):
        if mapping:
            host_port = int(mapping[0].get("HostPort", 0))
            if host_port:
                log.debug("[%s] Auto-detected port %d from host bindings", container.name, host_port)
                return host_port

    return None


def _forward_host(container) -> str:
    """
    Return the hostname NPMplus should forward to.

    Uses the container name (Docker internal DNS).  This requires NPMplus and
    the target container to be connected to a shared Docker network.
    """
    return container.name.lstrip("/")


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def _handle_start(container_id: str, client: docker.DockerClient) -> None:
    """Process a container-start event: create a proxy host if labelled."""
    try:
        container = client.containers.get(container_id)
    except docker.errors.NotFound:
        return

    labels = container.labels

    if labels.get("npm.enable", "").lower() not in ("true", "1", "yes"):
        return  # Not opted in

    domain = labels.get("npm.domain", "").strip()
    if not domain:
        log.warning("[%s] npm.enable is set but npm.domain label is missing", container.name)
        return

    # Scheme
    scheme = labels.get("npm.scheme", "http").strip().lower()
    if scheme not in ("http", "https"):
        log.warning("[%s] Invalid npm.scheme '%s', defaulting to http", container.name, scheme)
        scheme = "http"

    # Port
    port_label = labels.get("npm.port", "").strip()
    if port_label:
        try:
            port = int(port_label)
        except ValueError:
            log.warning("[%s] Invalid npm.port '%s', auto-detecting", container.name, port_label)
            port = _detect_port(container)
    else:
        port = _detect_port(container)

    if not port:
        log.warning(
            "[%s] Could not determine a port for domain %s — "
            "set the npm.port label explicitly",
            container.name,
            domain,
        )
        return

    forward_host = _forward_host(container)

    # Already tracked (e.g. watcher restarted or duplicate event)
    if container_id in _state:
        log.debug("[%s] Already tracked (proxy id=%d)", container.name, _state[container_id])
        return

    # Domain already registered in NPMplus (e.g. another container, or leftover)
    existing = _find_proxy_host(domain)
    if existing:
        log.info(
            "[%s] Domain %s already exists in NPMplus (id=%d) — associating",
            container.name,
            domain,
            existing["id"],
        )
        _state[container_id] = existing["id"]
        _save_state()
        return

    # Create the proxy host
    host_id = _create_proxy_host(domain, forward_host, port, scheme)
    if host_id:
        _state[container_id] = host_id
        _save_state()


def _handle_stop(container_id: str) -> None:
    """Process a container-stop/die event: remove the proxy host if tracked."""
    if container_id not in _state:
        return

    host_id = _state[container_id]

    if CLEANUP_ON_STOP:
        _delete_proxy_host(host_id)
    else:
        log.debug("CLEANUP_ON_STOP=false — keeping proxy host id=%d", host_id)

    # Always remove from in-memory state so the next start re-registers cleanly
    del _state[container_id]
    _save_state()


def _cleanup_stale(client: docker.DockerClient) -> None:
    """Remove proxy hosts whose containers no longer exist."""
    stale = []
    for cid in list(_state.keys()):
        try:
            client.containers.get(cid)
        except docker.errors.NotFound:
            stale.append(cid)

    for cid in stale:
        host_id = _state[cid]
        log.info("Stale container %s — removing proxy host id=%d", cid[:12], host_id)
        if CLEANUP_ON_STOP:
            _delete_proxy_host(host_id)
        del _state[cid]

    if stale:
        _save_state()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("NPMplus Auto Domain Watcher starting up …")

    _load_state()

    # Connect to Docker via the socket proxy
    client: Optional[docker.DockerClient] = None
    backoff = 2
    while client is None:
        try:
            client = docker.DockerClient(base_url=DOCKER_HOST)
            client.ping()
            log.info("Connected to Docker API")
        except Exception as exc:
            log.warning("Docker connect failed: %s — retrying in %ds …", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            client = None

    # Wait until NPMplus is reachable and credentials work
    backoff = 5
    while not _get_token():
        log.warning("NPMplus not ready — retrying in %ds …", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)

    # Clean up state entries whose containers are gone
    _cleanup_stale(client)

    # Register any already-running containers
    log.info("Scanning running containers …")
    for container in client.containers.list():
        _handle_start(container.id, client)

    # Main event loop
    log.info("Listening for Docker container events …")
    while True:
        try:
            for event in client.events(decode=True, filters={"type": "container"}):
                action = event.get("Action", "")
                cid = event.get("Actor", {}).get("ID", "")
                cname = event.get("Actor", {}).get("Attributes", {}).get("name", cid[:12])

                if action == "start":
                    log.info("[%s] Container started", cname)
                    _handle_start(cid, client)
                elif action in ("stop", "die", "kill"):
                    log.info("[%s] Container stopped (action=%s)", cname, action)
                    _handle_stop(cid)

        except KeyboardInterrupt:
            log.info("Received interrupt — shutting down")
            break
        except Exception as exc:
            log.error("Event loop error: %s — reconnecting in 5s …", exc)
            time.sleep(5)
            # Attempt to reconnect
            try:
                client = docker.DockerClient(base_url=DOCKER_HOST)
                client.ping()
                log.info("Reconnected to Docker API")
            except Exception as exc2:
                log.error("Reconnect failed: %s", exc2)


if __name__ == "__main__":
    main()
