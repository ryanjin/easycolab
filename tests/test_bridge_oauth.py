"""Exercise the real CLI entry point and both Colab CLI credential backends offline."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import colab_bridge as bridge


class AuthLinkTest(unittest.TestCase):
    def run_bridge(self, home):
        return subprocess.run(
            [sys.executable, "-B", bridge.__file__, "auth_url"],
            env={**os.environ, "HOME": str(home), "USERPROFILE": str(home)},
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
        )

    def test_bundled_client_config_and_pkce(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_bridge(tmp)
            self.assertEqual(result.returncode, 0)
            response = json.loads(result.stdout)
            self.assertTrue(response["ok"])
            url = urlsplit(response["auth_url"])
            self.assertEqual((url.scheme, url.hostname), ("https", "accounts.google.com"))
            params = parse_qs(url.query)
            self.assertEqual(params["redirect_uri"], [bridge.REMOTE_REDIRECT_URI])
            self.assertEqual(params["code_challenge_method"], ["S256"])
            verifier = Path(tmp, ".config/colab-cli/pkce_verifier.txt")
            digest = hashlib.sha256(verifier.read_bytes()).digest()
            self.assertEqual(params["code_challenge"], [base64.urlsafe_b64encode(digest).decode().rstrip("=")])
            if os.name == "posix":
                self.assertEqual(verifier.stat().st_mode & 0o777, 0o600)
            self.assertFalse(Path(tmp, ".config/colab-cli/token.json").exists())

    def test_invalid_custom_config_returns_sanitized_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".colab-cli-oauth-config.json").write_text("malformed-test-value")
            result = self.run_bridge(tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["error_code"], "oauth_config")
            self.assertNotIn("malformed-test-value", result.stdout + result.stderr)

    def test_verifier_write_failure_returns_sanitized_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".config").write_text("reserved")
            result = self.run_bridge(tmp)
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error_code"], "oauth_verifier")
            self.assertNotIn("auth_url", payload)


class CredentialStoreTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.token = Path(temp.name, "token.json")
        self.verifier = Path(temp.name, "verifier")
        self.cache = Path(temp.name, "profile.json")
        self.enterContext(mock.patch.multiple(
            bridge, TOKEN_CONFIG_PATH=str(self.token), PKCE_VERIFIER_PATH=str(self.verifier),
            ACCOUNT_PROFILE_CACHE_PATH=str(self.cache), USES_KEYRING=True,
        ))
        self.store = self.enterContext(mock.patch.object(bridge, "colab_auth"))
        self.store._get_token_from_keyring.return_value = None
        self.enterContext(mock.patch.object(bridge.state, "_client", None))
        self.record = {
            "token": "local-test-access", "refresh_token": "local-test-refresh",
            "client_id": "test", "client_secret": "test", "expiry": "2099-01-01T00:00:00Z",
        }

    def test_status_with_no_credentials_never_starts_login(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            bridge.cmd_auth_status()
        self.assertEqual(json.loads(output.getvalue()), {"ok": True, "logged_in": False})
        self.assertIsNone(bridge.state._client)
        self.store._save_token_to_keyring.assert_not_called()

    def test_keyring_migration_removes_file_only_after_success(self):
        self.token.write_text(json.dumps(self.record))
        self.store._save_token_to_keyring.side_effect = OSError("sensitive backend details")
        with self.assertRaises(bridge.BridgeAuthError) as error:
            bridge._load_credentials()
        self.assertEqual(error.exception.code, "oauth_store")
        self.assertNotIn("sensitive backend details", str(error.exception))
        self.assertTrue(self.token.exists())

        self.store._save_token_to_keyring.side_effect = None
        creds = bridge._authenticated_credentials()
        self.assertEqual(creds.token, self.record["token"])
        saved = json.loads(self.store._save_token_to_keyring.call_args.args[0])
        self.assertEqual(saved["refresh_token"], self.record["refresh_token"])
        self.assertFalse(self.token.exists())
        self.assertIs(bridge.state._client.session.credentials, creds)

    def test_refresh_writes_to_original_backend(self):
        for use_keyring in (False, True):
            with self.subTest(keyring=use_keyring), mock.patch.object(bridge, "USES_KEYRING", use_keyring):
                expired = json.dumps({**self.record, "expiry": "2000-01-01T00:00:00Z"})
                if use_keyring:
                    self.token.unlink(missing_ok=True)
                    self.store._get_token_from_keyring.return_value = expired
                else:
                    self.token.write_text(expired)
                def refresh(creds, request):
                    creds.token = "refreshed-test-access"
                    creds.expiry = None
                with mock.patch.object(bridge.Credentials, "refresh", refresh):
                    self.assertEqual(bridge._load_credentials().token, "refreshed-test-access")
                if use_keyring:
                    saved = self.store._save_token_to_keyring.call_args.args[0]
                    self.assertFalse(self.token.exists())
                else:
                    saved = self.token.read_text()
                    self.store._save_token_to_keyring.assert_not_called()
                self.assertEqual(json.loads(saved)["token"], "refreshed-test-access")

    def test_code_exchange_uses_staged_proof_and_correct_store(self):
        for use_keyring in (False, True):
            with self.subTest(keyring=use_keyring), mock.patch.object(bridge, "USES_KEYRING", use_keyring):
                self.verifier.write_text("test-verifier")
                with (mock.patch.object(bridge, "_oauth_flow") as oauth,
                      mock.patch.object(bridge, "cmd_auth_status") as status):
                    flow = oauth.return_value
                    flow.credentials = bridge.Credentials.from_authorized_user_info(self.record)
                    bridge.cmd_auth_code(" local-test-code ")
                    self.assertEqual(flow.code_verifier, "test-verifier")
                    flow.fetch_token.assert_called_once_with(code="local-test-code")
                    status.assert_called_once()
                self.assertFalse(self.verifier.exists())
                if use_keyring:
                    self.store._save_token_to_keyring.assert_called_once()
                    self.assertFalse(self.token.exists())
                else:
                    self.assertTrue(self.token.exists())

    def test_logout_is_limited_to_upstream_item_and_local_auth_files(self):
        for path in (self.token, self.cache, self.verifier):
            path.write_text("test")
        self.store._get_token_from_keyring.return_value = json.dumps(self.record)
        self.store._run_with_timeout.side_effect = TimeoutError()
        with self.assertRaises(bridge.BridgeAuthError):
            bridge.cmd_auth_logout()
        self.assertTrue(self.token.exists())
        self.store._run_with_timeout.side_effect = None
        with contextlib.redirect_stdout(io.StringIO()) as output:
            bridge.cmd_auth_logout()
        self.assertFalse(json.loads(output.getvalue())["logged_in"])
        self.store._run_with_timeout.assert_called_with(
            self.store.keyring.delete_password, self.store.KEYRING_SERVICE_NAME,
            self.store.KEYRING_USERNAME, timeout=self.store.KEYRING_TIMEOUT_SECONDS,
        )
        for path in (self.token, self.cache, self.verifier):
            self.assertFalse(path.exists())
