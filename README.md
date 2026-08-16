# Drift

[![CI](https://github.com/agent-cyanez/drift/actions/workflows/ci.yml/badge.svg)](https://github.com/agent-cyanez/drift/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/agent-cyanez/drift)](https://github.com/agent-cyanez/drift/releases)
[![Container](https://img.shields.io/badge/ghcr.io-drift-blue)](https://ghcr.io/agent-cyanez/drift)

Docker image update notifier with [ntfy](https://ntfy.sh) alerts. Checks your running containers against upstream registries and notifies you when newer images are available.

**Notify, don't auto-update.** Unlike Watchtower, Drift only tells you about updates — you decide when and how to apply them.

Zero dependencies. Single Python file. Docker-native.

## Quick Start

```yaml
services:
  drift:
    image: ghcr.io/agent-cyanez/drift:latest
    container_name: drift
    restart: unless-stopped
    network_mode: host
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - NTFY_URL=http://your-ntfy:8888
      - NTFY_TOPIC=updates
```

## How It Works

1. Lists all running Docker containers
2. Skips locally-built images (compose builds, no registry reference)
3. For each registry image, compares the local digest with the remote digest
4. If they differ, sends an ntfy notification
5. Repeats on a configurable interval (default: 6 hours)

Supports Docker Hub, ghcr.io, and any registry implementing the [OCI Distribution Spec](https://github.com/opencontainers/distribution-spec) (Forgejo, GitLab, Codeberg, etc.).

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NTFY_URL` | `http://127.0.0.1:8888` | ntfy server URL |
| `NTFY_TOPIC` | `vela` | ntfy topic for notifications |
| `INTERVAL` | `21600` | Check interval in seconds (default: 6 hours) |
| `COOLDOWN` | `86400` | Per-image notification cooldown in seconds (default: 24 hours) |
| `TIMEOUT` | `30` | Registry request timeout in seconds |
| `IMAGES` | *(empty — all)* | Comma-separated image filter patterns (glob, `!` to exclude) |

### Image Filtering

```bash
# Only check specific registries
IMAGES=ghcr.io/*,docker.io/*

# Exclude specific images
IMAGES=!*test*,!*dev*

# Mix positive and negative patterns
IMAGES=ghcr.io/*,!ghcr.io/internal/*
```

## Part of the Monitoring Suite

| Tool | Purpose |
|------|---------|
| [Lookout](https://github.com/agent-cyanez/lookout) | Container lifecycle alerts |
| [Beacon](https://github.com/agent-cyanez/beacon) | Service status page |
| [Bosun](https://github.com/agent-cyanez/bosun) | Container log pattern alerts |
| [Sextant](https://github.com/agent-cyanez/sextant) | TLS certificate expiry monitor |
| **Drift** | Image update notifier |

## License

MIT
