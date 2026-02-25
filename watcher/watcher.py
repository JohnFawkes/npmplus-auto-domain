#!/usr/bin/env python3
"""
NPMplus Auto Domain - Docker Label Watcher

Watches Docker container labels and automatically manages proxy hosts
in NPMplus (Nginx Proxy Manager Plus).

Labels watched on containers:
  npm.enable        - Set to "true" / "1" / "yes" to enable auto-proxying
  npm.domain        - Domain name for the proxy (required when npm.enable is set)
  npm.port          - Port to forward to (optional; auto-detected from ExposedPorts)
  npm.scheme        - Forward scheme: "http" or "https" (optional; defaults to "http")
  npm.ssl           - SSL certificate: "create" to request a new Let's Encrypt cert,
                      or the exact name shown in the NPMplus UI for an existing cert
  npm.force_https   - Set to "true" to add an HTTP→HTTPS redirect on the proxy host

Required environment variables:
  NPMPLUS_HOST  - NPMplus host, e.g. "192.168.1.10:81" or "npm.example.com"
  NPMPLUS_USER  - NPMplus admin e-mail
  NPMPLUS_PASS  - NPMplus admin password

Optional environment variables:
  NPMPLUS_HTTPS      - Use HTTPS to reach NPMplus API (default: false)
  LETSENCRYPT_EMAIL  - E-mail for Let's Encrypt registration when npm.ssl=create
                       (defaults to NPMPLUS_USER)
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
# E-mail used for Let's Encrypt registration when npm.ssl=create.
# Falls back to NPMPLUS_USER (which is already an e-mail address).
LETSENCRYPT_EMAIL = os.environ.get("LETSENCRYPT_EMAIL", "").strip() or NPMPLUS_USER
STATE_FILE = "/data/state.json"

# Validate required config
_missing = [k for k, v in [("NPMPLUS_HOST", NPMPLUS_HOST), ("NPMPLUS_USER", NPMPLUS_USER), ("NPMPLUS_PASS", NPMPLUS_PASS)] if not v]
if _missing:
    log.error("Missing required environment variables: %s", ", ".join(_missing))
    sys.exit(1)

_scheme = "https" if NPMPLUS_HTTPS else "http"
NPMPLUS_API = f"{_scheme}://{NPMPLUS_HOST}/api"  # may be upgraded to https at runtime

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
# NPMplus API helpers — cookie-based session authentication
#
# NPMplus authenticates exclusively via an httpOnly cookie named "token".
# The POST /api/tokens response body contains only {"expires": "<ISO8601>"},
# the actual JWT is delivered as a Set-Cookie header.  We use a
# requests.Session so that cookie is sent automatically on every request.
# ---------------------------------------------------------------------------
_npm_session: Optional[requests.Session] = None
_session_expires: float = 0.0


def _ensure_auth() -> Optional[requests.Session]:
    """Return an authenticated requests.Session, re-logging-in if expired."""
    global _npm_session, _session_expires, NPMPLUS_API

    if _npm_session and time.time() < _session_expires - 60:
        return _npm_session

    session = requests.Session()
    session.verify = False  # accept self-signed certificates

    try:
        # Disable automatic redirects so a 301/308 HTTP→HTTPS redirect does
        # not silently convert our POST to a GET (standard browser behaviour).
        r = session.post(
            f"{NPMPLUS_API}/tokens",
            json={"identity": NPMPLUS_USER, "secret": NPMPLUS_PASS},
            timeout=15,
            allow_redirects=False,
        )
        # Auto-upgrade to HTTPS when the server issues a redirect
        if r.status_code in (301, 302, 307, 308):
            location = r.headers.get("Location", "")
            if location.startswith("https://"):
                log.info("NPMplus redirected to HTTPS — upgrading API base URL")
                NPMPLUS_API = NPMPLUS_API.replace("http://", "https://", 1)
                r = session.post(
                    f"{NPMPLUS_API}/tokens",
                    json={"identity": NPMPLUS_USER, "secret": NPMPLUS_PASS},
                    timeout=15,
                )
            else:
                r = session.post(
                    location,
                    json={"identity": NPMPLUS_USER, "secret": NPMPLUS_PASS},
                    timeout=15,
                )
        r.raise_for_status()

        # Response body: {"expires": "2026-02-26T05:52:51.000Z"}
        # The token itself is in the Set-Cookie header (httpOnly, path=/api).
        body = r.json()
        expires_str = body.get("expires", "")
        if expires_str:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                _session_expires = dt.timestamp()
            except Exception:
                _session_expires = time.time() + 86400
        else:
            _session_expires = time.time() + 86400

        _npm_session = session
        log.info("Authenticated with NPMplus (session cookie acquired)")
        return session
    except Exception as exc:
        log.error("NPMplus authentication failed: %s", exc)
        return None


def _npm_api(method: str, path: str, _retry: bool = True, **kwargs) -> Optional[requests.Response]:
    """Make an authenticated request to the NPMplus API."""
    session = _ensure_auth()
    if not session:
        return None
    kwargs.setdefault("timeout", 15)
    try:
        r = session.request(
            method,
            f"{NPMPLUS_API}{path}",
            **kwargs,
        )
        if r.status_code == 401 and _retry:
            # Session may have been invalidated server-side; force re-auth
            global _npm_session
            _npm_session = None
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


def _create_proxy_host(
    domain: str,
    forward_host: str,
    forward_port: int,
    scheme: str = "http",
    ssl_forced: bool = False,
    certificate_id: Optional[int] = None,
) -> Optional[int]:
    """Create a proxy host in NPMplus. Returns the new host id on success."""
    payload = {
        "domain_names": [domain],
        "forward_scheme": scheme,
        "forward_host": forward_host,
        "forward_port": forward_port,
        "ssl_forced": ssl_forced,
    }
    if certificate_id is not None:
        payload["certificate_id"] = certificate_id
    r = _npm_api("POST", "/nginx/proxy-hosts", json=payload)
    if r and r.status_code == 201:
        host_id = r.json().get("id")
        log.info(
            "Created proxy host: %s -> %s://%s:%d  (id=%s, ssl_forced=%s, cert_id=%s)",
            domain,
            scheme,
            forward_host,
            forward_port,
            host_id,
            ssl_forced,
            certificate_id,
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


def _proxy_host_matches(
    existing: dict,
    forward_host: str,
    forward_port: int,
    scheme: str,
    ssl_forced: bool,
    certificate_id: Optional[int],
) -> bool:
    """Return True when the NPMplus proxy-host already reflects all desired values."""
    # NPMplus stores "no certificate" as 0; normalise both sides to None.
    existing_cert = existing.get("certificate_id") or None
    desired_cert = certificate_id or None
    return (
        existing.get("forward_host") == forward_host
        and int(existing.get("forward_port", 0)) == forward_port
        and existing.get("forward_scheme") == scheme
        and bool(existing.get("ssl_forced")) == ssl_forced
        and existing_cert == desired_cert
    )


def _update_proxy_host(
    host_id: int,
    domain: str,
    forward_host: str,
    forward_port: int,
    scheme: str = "http",
    ssl_forced: bool = False,
    certificate_id: Optional[int] = None,
) -> bool:
    """Update an existing proxy host in NPMplus. Returns True on success."""
    payload = {
        "domain_names": [domain],
        "forward_scheme": scheme,
        "forward_host": forward_host,
        "forward_port": forward_port,
        "ssl_forced": ssl_forced,
    }
    if certificate_id is not None:
        payload["certificate_id"] = certificate_id
    r = _npm_api("PUT", f"/nginx/proxy-hosts/{host_id}", json=payload)
    if r and r.status_code == 200:
        log.info(
            "Updated proxy host id=%d: %s -> %s://%s:%d  (ssl_forced=%s, cert_id=%s)",
            host_id, domain, scheme, forward_host, forward_port, ssl_forced, certificate_id,
        )
        return True
    if r:
        log.error("Failed to update proxy host id=%d: %s %s", host_id, r.status_code, r.text)
    return False


def _find_certificate_by_name(name: str) -> Optional[int]:
    """Look up an NPMplus certificate by its nice_name. Returns cert id or None."""
    r = _npm_api("GET", "/nginx/certificates")
    if r and r.status_code == 200:
        for cert in r.json():
            if cert.get("nice_name", "") == name:
                return cert.get("id")
        log.warning("No certificate named '%s' found in NPMplus", name)
    return None


def _create_certificate(domain: str) -> Optional[int]:
    """Request a new Let's Encrypt certificate for *domain* via NPMplus.

    Uses LETSENCRYPT_EMAIL for the LE registration address.
    The API call may block for ~30-60 s while NPMplus completes the ACME
    challenge, so a longer timeout (120 s) is used.
    Returns the new certificate id on success, or None on failure.
    """
    payload = {
        "domain_names": [domain],
        "meta": {
            "letsencrypt_email": LETSENCRYPT_EMAIL,
            "letsencrypt_agree": True,
            "dns_challenge": False,
        },
        "provider": "letsencrypt",
    }
    r = _npm_api("POST", "/nginx/certificates", json=payload, timeout=120)
    if r and r.status_code == 201:
        cert_id = r.json().get("id")
        log.info("Created Let's Encrypt certificate for %s (id=%s)", domain, cert_id)
        return cert_id
    if r:
        log.error("Failed to create certificate for %s: %s %s", domain, r.status_code, r.text)
    return None


def _resolve_certificate_id(domain: str, ssl_label: str) -> Optional[int]:
    """Resolve the npm.ssl label to an NPMplus certificate id.

    - "create"       → request a new Let's Encrypt certificate for *domain*
    - anything else  → look up an existing certificate by nice_name
    """
    if ssl_label.lower() == "create":
        return _create_certificate(domain)
    return _find_certificate_by_name(ssl_label)


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


def _container_ip(container) -> Optional[str]:
    """Return the first Docker bridge IP for the container, or None."""
    networks = container.attrs.get("NetworkSettings", {}).get("Networks") or {}
    for net_config in networks.values():
        ip = net_config.get("IPAddress", "")
        if ip:
            return ip
    return None


def _forward_host(container) -> Optional[str]:
    """
    Determine the forward host NPMplus should proxy to, via container labels.

    Priority:
      1. npm.ip=<value>         — use this value verbatim as the forward host.
                                  Lets you point NPMplus at the Docker host IP
                                  (e.g. 192.168.1.10) for containers that run
                                  with network_mode: host.
      2. npm.containername=false — use the container's Docker bridge IP
                                  (auto-detected from NetworkSettings).
                                  Useful when NPMplus is on host-mode and the
                                  target container is on a bridge network.
      3. default                 — use the Docker container name (Docker
                                  internal DNS).  Requires NPMplus and the
                                  target container to share a Docker network.
    """
    labels = container.labels

    # 1. Explicit IP / host override
    custom_ip = labels.get("npm.ip", "").strip()
    if custom_ip:
        log.debug("[%s] Using npm.ip label: %s", container.name, custom_ip)
        return custom_ip

    # 2. Auto-detected container bridge IP
    use_name = labels.get("npm.containername", "true").lower() not in ("false", "0", "no")
    if not use_name:
        ip = _container_ip(container)
        if not ip:
            log.warning(
                "[%s] npm.containername=false but no Docker bridge IP found",
                container.name,
            )
        return ip

    # 3. Default — container name via Docker internal DNS
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
    if not forward_host:
        log.warning("[%s] Could not determine forward_host, skipping", container.name)
        return

    # SSL certificate
    ssl_label = labels.get("npm.ssl", "").strip()
    certificate_id: Optional[int] = None
    if ssl_label:
        certificate_id = _resolve_certificate_id(domain, ssl_label)
        if certificate_id is None:
            log.warning(
                "[%s] Could not resolve SSL certificate '%s' — creating proxy host without SSL",
                container.name,
                ssl_label,
            )

    # Force HTTPS redirect
    force_https = labels.get("npm.force_https", "").lower() in ("true", "1", "yes")

    # Already tracked (e.g. watcher restarted or duplicate event)
    if container_id in _state:
        log.debug("[%s] Already tracked (proxy id=%d)", container.name, _state[container_id])
        return

    # Domain already registered in NPMplus (e.g. another container, or leftover)
    existing = _find_proxy_host(domain)
    if existing:
        host_id = existing["id"]
        if _proxy_host_matches(existing, forward_host, port, scheme, force_https, certificate_id):
            log.info(
                "[%s] Domain %s already exists in NPMplus (id=%d) — labels match, no update needed",
                container.name, domain, host_id,
            )
        else:
            log.info(
                "[%s] Domain %s already exists in NPMplus (id=%d) — labels differ, updating",
                container.name, domain, host_id,
            )
            _update_proxy_host(
                host_id, domain, forward_host, port, scheme,
                ssl_forced=force_https,
                certificate_id=certificate_id,
            )
        _state[container_id] = host_id
        _save_state()
        return

    # Create the proxy host
    host_id = _create_proxy_host(
        domain, forward_host, port, scheme,
        ssl_forced=force_https,
        certificate_id=certificate_id,
    )
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
    while not _ensure_auth():
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
