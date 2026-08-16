"""Tests for Drift — Docker image update notifier."""

import json
import unittest
from unittest.mock import patch, MagicMock

import drift


class TestParseImageRef(unittest.TestCase):
    def test_docker_hub_library(self):
        self.assertEqual(
            drift.parse_image_ref("nginx:1.27-alpine"),
            ("registry-1.docker.io", "library/nginx", "1.27-alpine"),
        )

    def test_docker_hub_library_latest(self):
        self.assertEqual(
            drift.parse_image_ref("redis"),
            ("registry-1.docker.io", "library/redis", "latest"),
        )

    def test_docker_hub_user_repo(self):
        self.assertEqual(
            drift.parse_image_ref("nosferath/blog:abc123"),
            ("registry-1.docker.io", "nosferath/blog", "abc123"),
        )

    def test_docker_hub_user_repo_latest(self):
        self.assertEqual(
            drift.parse_image_ref("wallabag/wallabag"),
            ("registry-1.docker.io", "wallabag/wallabag", "latest"),
        )

    def test_ghcr(self):
        self.assertEqual(
            drift.parse_image_ref("ghcr.io/immich-app/immich-server:v2.5.6"),
            ("ghcr.io", "immich-app/immich-server", "v2.5.6"),
        )

    def test_codeberg(self):
        self.assertEqual(
            drift.parse_image_ref("codeberg.org/forgejo/forgejo:14.0.3"),
            ("codeberg.org", "forgejo/forgejo", "14.0.3"),
        )

    def test_data_forgejo(self):
        self.assertEqual(
            drift.parse_image_ref("data.forgejo.org/forgejo/runner:12.7.0"),
            ("data.forgejo.org", "forgejo/runner", "12.7.0"),
        )

    def test_ghcr_deep_path(self):
        self.assertEqual(
            drift.parse_image_ref("ghcr.io/gethomepage/homepage:v1.10.1"),
            ("ghcr.io", "gethomepage/homepage", "v1.10.1"),
        )

    def test_sha256_digest_returns_none(self):
        self.assertIsNone(
            drift.parse_image_ref("nginx@sha256:abcdef1234567890")
        )

    def test_localhost_returns_none(self):
        self.assertIsNone(drift.parse_image_ref("localhost:5000/myapp"))


class TestIsLocalBuild(unittest.TestCase):
    def test_compose_name(self):
        self.assertTrue(drift.is_local_build("beacon-beacon"))

    def test_simple_name(self):
        self.assertTrue(drift.is_local_build("myapp"))

    def test_docker_hub_bare_name(self):
        # Bare names like "nginx" look like local builds to this heuristic.
        # parse_image_ref handles them correctly — is_local_build is a fast filter
        # used by should_check, which also calls parse_image_ref.
        self.assertTrue(drift.is_local_build("nginx"))

    def test_ghcr(self):
        self.assertFalse(drift.is_local_build("ghcr.io/foo/bar"))

    def test_user_repo(self):
        self.assertFalse(drift.is_local_build("nosferath/blog"))


class TestShouldCheck(unittest.TestCase):
    def test_skips_local_build(self):
        self.assertFalse(drift.should_check("beacon-beacon", []))

    def test_allows_registry_image_no_filter(self):
        self.assertTrue(drift.should_check("ghcr.io/foo/bar", []))

    def test_positive_filter_match(self):
        self.assertTrue(drift.should_check("ghcr.io/foo/bar", ["ghcr.io/*"]))

    def test_positive_filter_no_match(self):
        self.assertFalse(drift.should_check("ghcr.io/foo/bar", ["docker.io/*"]))

    def test_negative_filter(self):
        self.assertFalse(drift.should_check("ghcr.io/foo/bar", ["!ghcr.io/*"]))

    def test_negative_only_allows_others(self):
        self.assertTrue(drift.should_check("docker.io/library/nginx", ["!ghcr.io/*"]))


class TestParseFilter(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(drift.parse_filter(""), [])

    def test_single(self):
        self.assertEqual(drift.parse_filter("ghcr.io/*"), ["ghcr.io/*"])

    def test_multiple(self):
        self.assertEqual(
            drift.parse_filter("ghcr.io/*, !*test*"),
            ["ghcr.io/*", "!*test*"],
        )

    def test_whitespace(self):
        self.assertEqual(drift.parse_filter("  "), [])


class TestGetLocalDigest(unittest.TestCase):
    @patch("drift.docker_get")
    def test_returns_digest(self, mock_get):
        mock_get.return_value = {
            "RepoDigests": ["nginx@sha256:abc123def456"]
        }
        self.assertEqual(drift.get_local_digest("nginx:latest"), "sha256:abc123def456")

    @patch("drift.docker_get")
    def test_no_repo_digests(self, mock_get):
        mock_get.return_value = {"RepoDigests": []}
        self.assertIsNone(drift.get_local_digest("local-build"))

    @patch("drift.docker_get")
    def test_api_error(self, mock_get):
        mock_get.side_effect = RuntimeError("not found")
        self.assertIsNone(drift.get_local_digest("missing"))


class TestGetRunningContainers(unittest.TestCase):
    @patch("drift.docker_get")
    def test_parses_containers(self, mock_get):
        mock_get.return_value = [
            {"Names": ["/nginx"], "Image": "nginx:1.27"},
            {"Names": ["/beacon"], "Image": "beacon-beacon"},
        ]
        result = drift.get_running_containers()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], {"name": "nginx", "image": "nginx:1.27"})
        self.assertEqual(result[1], {"name": "beacon", "image": "beacon-beacon"})


class TestCheckUpdates(unittest.TestCase):
    @patch("drift.get_remote_digest")
    @patch("drift.get_auth_token")
    @patch("drift.get_local_digest")
    @patch("drift.get_running_containers")
    def test_detects_update(self, mock_containers, mock_local, mock_token, mock_remote):
        mock_containers.return_value = [
            {"name": "web", "image": "ghcr.io/org/app:v1.0"}
        ]
        mock_local.return_value = "sha256:old111"
        mock_token.return_value = "token123"
        mock_remote.return_value = "sha256:new222"

        updates = drift.check_updates([])
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["container"], "web")
        self.assertEqual(updates[0]["local_digest"], "sha256:old111")
        self.assertEqual(updates[0]["remote_digest"], "sha256:new222")

    @patch("drift.get_remote_digest")
    @patch("drift.get_auth_token")
    @patch("drift.get_local_digest")
    @patch("drift.get_running_containers")
    def test_up_to_date(self, mock_containers, mock_local, mock_token, mock_remote):
        mock_containers.return_value = [
            {"name": "web", "image": "ghcr.io/org/app:v1.0"}
        ]
        mock_local.return_value = "sha256:same"
        mock_token.return_value = "token"
        mock_remote.return_value = "sha256:same"

        updates = drift.check_updates([])
        self.assertEqual(len(updates), 0)

    @patch("drift.get_running_containers")
    def test_skips_local_builds(self, mock_containers):
        mock_containers.return_value = [
            {"name": "beacon", "image": "beacon-beacon"}
        ]
        updates = drift.check_updates([])
        self.assertEqual(len(updates), 0)

    @patch("drift.get_remote_digest")
    @patch("drift.get_auth_token")
    @patch("drift.get_local_digest")
    @patch("drift.get_running_containers")
    def test_skips_no_local_digest(self, mock_containers, mock_local, mock_token, mock_remote):
        mock_containers.return_value = [
            {"name": "web", "image": "ghcr.io/org/app:v1.0"}
        ]
        mock_local.return_value = None

        updates = drift.check_updates([])
        self.assertEqual(len(updates), 0)
        mock_token.assert_not_called()


class TestSendNotification(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_sends_ntfy(self, mock_urlopen):
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock()
        drift.send_notification("web", "org/app", "v2.0")
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        self.assertIn(b"Update available", req.data)

    @patch("urllib.request.urlopen")
    def test_handles_failure(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("connection refused")
        # Should not raise
        drift.send_notification("web", "org/app", "v2.0")


if __name__ == "__main__":
    unittest.main()
