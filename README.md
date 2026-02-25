# NPMplus Auto Domain

> **AI-generated project notice**
> This project was built with [Claude AI](https://claude.ai). The code and documentation were written by an AI assistant. Please review all code yourself before running it in any environment you care about — especially anything that touches Docker, networking, or external APIs. Issues and PRs are welcome if you spot problems.

Automatically create and remove [NPMplus](https://github.com/ZoeyVid/NPMplus) proxy hosts by reading Docker container labels. When a labelled container starts, a proxy host is registered in NPMplus. When the container stops, the proxy host is cleaned up.

The watcher connects to Docker through [tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) so the raw Docker socket is never exposed inside the watcher container.

---

## Labels

Add these labels to any container you want NPMplus to proxy:

| Label | Required | Default | Description |
|---|---|---|---|
| `npm.enable` | yes | — | Set to `true` to enable auto-proxying for this container |
| `npm.domain` | yes | — | Domain name to register in NPMplus (e.g. `app.example.com`) |
| `npm.port` | no | auto | Port the container listens on. Auto-detected from `ExposedPorts` if omitted |
| `npm.scheme` | no | `http` | Forward scheme: `http` or `https` |
| `npm.ip` | no | — | Forward to this IP or hostname verbatim. Overrides all other forward-host logic. Useful when the container runs with `network_mode: host` and you need NPMplus to forward to the host IP (e.g. `192.168.1.10`) |
| `npm.containername` | no | `true` | Set to `false` to forward to the container's auto-detected Docker bridge IP instead of its name. Useful when NPMplus uses `network_mode: host` but the target container is on a bridge network |

### Port auto-detection

When `npm.port` is not set the watcher uses the following priority order:

1. Lowest port declared in the container image's `ExposedPorts` (the container's own listening port, reachable via Docker internal DNS)
2. First host-mapped port from the container's port bindings (useful when NPMplus is outside Docker)

If no port can be determined the container is skipped and a warning is logged. Set `npm.port` explicitly to avoid ambiguity.

### Example — docker run

```bash
docker run -d \
  --name myapp \
  --label npm.enable=true \
  --label npm.domain=app.example.com \
  --label npm.port=3000 \
  --label npm.scheme=http \
  myimage
```

### Example — docker-compose service

```yaml
services:
  myapp:
    image: myimage
    labels:
      npm.enable: "true"
      npm.domain: "app.example.com"
      npm.port: "3000"       # optional
      npm.scheme: "http"     # optional
```

---

## Quick start

### 1. Clone this repository

```bash
git clone https://github.com/your-org/npmplus-auto-domain.git
cd npmplus-auto-domain
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` and fill in at minimum:

```dotenv
NPMPLUS_HOST=192.168.1.10:81   # IP:port or domain of your NPMplus instance
NPMPLUS_USER=admin@example.com
NPMPLUS_PASS=yourpassword
```

### 3. Start the watcher

```bash
docker compose up -d
```

The watcher will:
- Scan all running containers immediately on startup
- Listen for `start` / `stop` / `die` events and react in real time
- Persist its state in a named volume so it survives restarts

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `NPMPLUS_HOST` | **yes** | — | Host (and optional port) of NPMplus, **without** scheme. E.g. `192.168.1.10:81` or `npm.example.com` |
| `NPMPLUS_USER` | **yes** | — | NPMplus admin e-mail address |
| `NPMPLUS_PASS` | **yes** | — | NPMplus admin password |
| `NPMPLUS_HTTPS` | no | `false` | Set `true` if NPMplus is only reachable over HTTPS. Self-signed certificates are accepted |
| `CLEANUP_ON_STOP` | no | `true` | Delete proxy hosts when a container stops. Set `false` to keep them alive across restarts |
| `LOG_LEVEL` | no | `INFO` | Logging verbosity: `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |

---

## Networking

### How the forward host is resolved

The watcher decides what hostname or IP to write into NPMplus based on container labels. The priority is:

1. **`npm.ip=<value>`** — use the value verbatim. Highest priority, overrides everything else.
2. **`npm.containername=false`** — use the container's Docker bridge IP (auto-detected from `NetworkSettings`).
3. **Default** — use the Docker container name, resolved via Docker internal DNS.

### Scenario A — NPMplus on a shared Docker network (default)

NPMplus and the target containers share a Docker network. Docker DNS resolves container names automatically. No extra labels needed beyond `npm.enable` and `npm.domain`.

```yaml
# In your application's docker-compose.yml
services:
  myapp:
    image: myimage
    networks:
      - proxy          # shared with NPMplus
    labels:
      npm.enable: "true"
      npm.domain: "app.example.com"

networks:
  proxy:
    external: true     # created by the NPMplus stack
```

```yaml
# In npmplus-auto-domain/docker-compose.yml — add to npm-watcher:
    networks:
      - watcher-net
      - proxy          # so the watcher can reach NPMplus on this network

networks:
  watcher-net:
    driver: bridge
  proxy:
    external: true
```

### Scenario B — NPMplus uses `network_mode: host`, containers on bridge networks

NPMplus runs on the host network stack and cannot use Docker DNS. However, the host always has routes to Docker bridge IPs through `docker0` / `br-xxx` interfaces. Set `npm.containername=false` on each container and the watcher will auto-detect its bridge IP:

```yaml
services:
  myapp:
    image: myimage
    labels:
      npm.enable: "true"
      npm.domain: "app.example.com"
      npm.containername: "false"   # use bridge IP instead of container name
```

### Scenario C — Container also uses `network_mode: host`

Both NPMplus and the container are on the host network stack. There is no Docker bridge IP to detect. Use `npm.ip` to point NPMplus at the Docker host's IP and specify the port explicitly:

```yaml
services:
  myapp:
    image: myimage
    network_mode: host
    labels:
      npm.enable: "true"
      npm.domain: "app.example.com"
      npm.ip: "192.168.1.10"   # Docker host IP
      npm.port: "3000"
```

### The watcher only needs to reach the socket proxy and NPMplus

The watcher does **not** need to be on the same network as your application containers. It only needs:

1. `watcher-net` — to talk to the socket proxy
2. A path to the NPMplus API — add the NPMplus network to the `npm-watcher` service if needed

---

## Architecture

```
+---------------------+        +--------------------------+
|   Docker daemon      |<------| tecnativa/docker-socket  |
| /var/run/docker.sock | (ro)  |        -proxy            |
+---------------------+        +----------+---------------+
                                           | tcp://socket-proxy:2375
                                +----------v---------------+
                                |      npm-watcher         |
                                | (watches labels &        |
                                |  manages NPMplus API)    |
                                +----------+---------------+
                                           | HTTP(S) /api/...
                                +----------v---------------+
                                |         NPMplus          |
                                | (Nginx Proxy Manager +)  |
                                +--------------------------+
```

### State persistence

The watcher stores a `container_id → proxy_host_id` mapping in `/data/state.json` (backed by a named Docker volume). On restart it:

1. Loads the saved state
2. Removes entries for containers that no longer exist (and deletes their proxy hosts if `CLEANUP_ON_STOP=true`)
3. Scans all running containers to pick up anything started while it was offline
4. Resumes listening for events

---

## Building from source

```bash
docker compose build
```

---

## Viewing logs

```bash
docker compose logs -f npm-watcher
```
