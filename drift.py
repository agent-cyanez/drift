#!/usr/bin/env python3
"""Drift — Docker image update notifier with ntfy alerts.

Checks running containers against their upstream registries and notifies
when newer images are available. Zero dependencies — stdlib only.
"""

import http.client
import json
import logging
import os
import signal
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("drift")

DOCKER_SOCKET = os.environ.get("DOCKER_HOST", "/var/run/docker.sock")
INTERVAL = int(os.environ.get("INTERVAL", "21600"))  # 6 hours
NTFY_URL = os.environ.get("NTFY_URL", "http://127.0.0.1:8888")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "vela")
COOLDOWN = int(os.environ.get("COOLDOWN", "86400"))  # 24 hours
TIMEOUT = int(os.environ.get("TIMEOUT", "30"))
IMAGES = os.environ.get("IMAGES", "")

_shutdown = False


def handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    log.info("Shutting down (signal %d)", signum)


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# --- Docker socket client ---

class DockerSocket(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost")
        self._socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._socket_path)


def docker_get(path):
    conn = DockerSocket(DOCKER_SOCKET)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    if resp.status != 200:
        raise RuntimeError(f"Docker API {path}: {resp.status} {data[:200]}")
    return json.loads(data)


# --- Image reference parsing ---

def parse_image_ref(image):
    """Parse a Docker image reference into (registry, repository, tag).

    Returns None if the image is a local build (no registry).
    """
    # Pinned digest references like image@sha256:abc — can't check for updates
    if "@sha256:" in image:
        return None

    # Split tag from the last path component only
    last_slash = image.rfind("/")
    if last_slash >= 0:
        prefix = image[:last_slash + 1]
        suffix = image[last_slash + 1:]
    else:
        prefix = ""
        suffix = image

    if ":" in suffix:
        tag = suffix.split(":", 1)[1]
        base = prefix + suffix.split(":", 1)[0]
    else:
        base = image
        tag = "latest"

    parts = base.split("/")

    if len(parts) == 1:
        if "." in parts[0] or parts[0] == "localhost":
            return None
        return ("registry-1.docker.io", f"library/{parts[0]}", tag)

    first = parts[0]
    if "." in first or ":" in first or first == "localhost":
        # localhost registries are local — skip
        host = first.split(":")[0]
        if host == "localhost":
            return None
        registry = first
        repository = "/".join(parts[1:])
    elif len(parts) == 2:
        registry = "registry-1.docker.io"
        repository = "/".join(parts)
    else:
        return None

    if not repository:
        return None

    return (registry, repository, tag)


def is_local_build(image_name):
    """Heuristic: local builds have no dots/slashes or look like compose names."""
    if "/" not in image_name and "." not in image_name:
        return True
    return False


# --- Registry authentication ---

def get_auth_token(registry, repository):
    """Get an anonymous pull token for public images."""
    if registry == "registry-1.docker.io":
        url = (
            "https://auth.docker.io/token"
            f"?service=registry.docker.io&scope=repository:{repository}:pull"
        )
    elif registry == "ghcr.io":
        url = (
            "https://ghcr.io/token"
            f"?service=ghcr.io&scope=repository:{repository}:pull"
        )
    else:
        return _try_www_authenticate(registry, repository)

    try:
        req = urllib.request.Request(url)
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            data = json.loads(resp.read())
            return data.get("token") or data.get("access_token")
    except Exception as e:
        log.debug("Token fetch failed for %s/%s: %s", registry, repository, e)
        return None


def _try_www_authenticate(registry, repository):
    """Try to get a token by following WWW-Authenticate from a 401."""
    try:
        url = f"https://{registry}/v2/"
        req = urllib.request.Request(url)
        ctx = ssl.create_default_context()
        urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx)
        return None  # No auth needed
    except urllib.error.HTTPError as e:
        if e.code != 401:
            return None
        auth_header = e.headers.get("WWW-Authenticate", "")
        if not auth_header.lower().startswith("bearer "):
            return None
        params = {}
        for part in auth_header[7:].split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                params[k.strip()] = v.strip().strip('"')
        realm = params.get("realm")
        service = params.get("service", "")
        if not realm:
            return None
        token_url = f"{realm}?service={service}&scope=repository:{repository}:pull"
        try:
            req2 = urllib.request.Request(token_url)
            with urllib.request.urlopen(req2, timeout=TIMEOUT, context=ctx) as resp2:
                data = json.loads(resp2.read())
                return data.get("token") or data.get("access_token")
        except Exception:
            return None
    except Exception:
        return None


# --- Registry digest check ---

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def get_remote_digest(registry, repository, tag, token=None):
    """Get the digest of an image tag from the remote registry."""
    url = f"https://{registry}/v2/{repository}/manifests/{tag}"
    headers = {"Accept": MANIFEST_ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, method="HEAD", headers=headers)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            return resp.headers.get("Docker-Content-Digest")
    except urllib.error.HTTPError as e:
        log.warning("Registry check failed for %s/%s:%s — HTTP %d", registry, repository, tag, e.code)
        return None
    except Exception as e:
        log.warning("Registry check failed for %s/%s:%s — %s", registry, repository, tag, e)
        return None


# --- Local digest ---

def get_local_digest(image_name):
    """Get the repo digest of a locally-pulled image from Docker."""
    try:
        encoded = urllib.parse.quote(image_name, safe="")
        data = docker_get(f"/images/{encoded}/json")
        digests = data.get("RepoDigests", [])
        if digests:
            # Format: "registry/repo@sha256:abc..."
            return digests[0].split("@")[-1] if "@" in digests[0] else None
        return None
    except Exception as e:
        log.debug("Could not get local digest for %s: %s", image_name, e)
        return None


# --- Container listing ---

def get_running_containers():
    """List running containers with their image references."""
    containers = docker_get("/containers/json")
    result = []
    for c in containers:
        name = (c.get("Names") or ["/unknown"])[0].lstrip("/")
        image = c.get("Image", "")
        result.append({"name": name, "image": image})
    return result


# --- Filtering ---

def parse_filter(filter_str):
    if not filter_str.strip():
        return []
    return [p.strip() for p in filter_str.split(",") if p.strip()]


def should_check(image_name, filters):
    """Determine if an image should be checked.

    Skips local builds. If filters are set, only checks matching images.
    """
    if is_local_build(image_name):
        return False
    if not filters:
        return True
    import fnmatch
    for pattern in filters:
        if pattern.startswith("!"):
            if fnmatch.fnmatch(image_name, pattern[1:]):
                return False
        elif fnmatch.fnmatch(image_name, pattern):
            return True
    # If there are positive patterns and none matched, exclude
    has_positive = any(not p.startswith("!") for p in filters)
    return not has_positive


# --- Notification ---

def send_notification(container_name, image, tag, priority="default"):
    """Send an ntfy notification about an available update."""
    url = f"{NTFY_URL}/{NTFY_TOPIC}"
    message = f"Update available for {container_name}: {image}:{tag}"
    headers = {
        "Title": "Drift — Image Update",
        "Priority": priority,
        "Tags": "package",
    }
    try:
        req = urllib.request.Request(
            url, data=message.encode(), headers=headers, method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
        log.info("Notification sent: %s", message)
    except Exception as e:
        log.error("Failed to send notification: %s", e)


# --- Main loop ---

def check_updates(filters):
    """Check all running containers for image updates. Returns list of updates found."""
    containers = get_running_containers()
    updates = []

    for container in containers:
        image = container["image"]
        name = container["name"]

        if not should_check(image, filters):
            log.debug("Skipping %s (%s) — filtered or local build", name, image)
            continue

        ref = parse_image_ref(image)
        if ref is None:
            log.debug("Skipping %s (%s) — cannot parse reference", name, image)
            continue

        registry, repository, tag = ref
        log.info("Checking %s (%s/%s:%s)", name, registry, repository, tag)

        local_digest = get_local_digest(image)
        if not local_digest:
            log.warning("No local digest for %s — skipping", image)
            continue

        token = get_auth_token(registry, repository)
        remote_digest = get_remote_digest(registry, repository, tag, token)
        if not remote_digest:
            log.warning("Could not get remote digest for %s — skipping", image)
            continue

        if local_digest != remote_digest:
            log.info("UPDATE AVAILABLE: %s (%s) local=%s remote=%s",
                     name, image, local_digest[:19], remote_digest[:19])
            updates.append({
                "container": name,
                "image": image,
                "registry": registry,
                "repository": repository,
                "tag": tag,
                "local_digest": local_digest,
                "remote_digest": remote_digest,
            })
        else:
            log.info("Up to date: %s (%s)", name, image)

    return updates


def main():
    log.info("Drift starting — interval=%ds, cooldown=%ds", INTERVAL, COOLDOWN)
    filters = parse_filter(IMAGES)
    if filters:
        log.info("Image filters: %s", filters)

    cooldowns = {}  # image -> last notification time

    while not _shutdown:
        try:
            updates = check_updates(filters)
            now = time.time()

            for update in updates:
                image_key = update["image"]
                last_notified = cooldowns.get(image_key, 0)
                if now - last_notified >= COOLDOWN:
                    send_notification(
                        update["container"],
                        update["repository"],
                        update["tag"],
                    )
                    cooldowns[image_key] = now
                else:
                    remaining = int(COOLDOWN - (now - last_notified))
                    log.info("Cooldown active for %s (%ds remaining)", image_key, remaining)

            if not updates:
                log.info("All images up to date")

        except Exception as e:
            log.error("Check cycle failed: %s", e)

        for _ in range(INTERVAL):
            if _shutdown:
                break
            time.sleep(1)

    log.info("Drift stopped")


if __name__ == "__main__":
    main()
