#!/usr/bin/env python3
"""Credential-free contract tests for SSRS Sigma authentication."""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import get_token  # noqa: E402
import sigma_rest  # noqa: E402


BASE = "https://api.sigmacomputing.com"
MINTED = "2099-10-05T20:00:00Z"
PROVIDER = {
    "SIGMA_API_TOKEN": "refreshed-token",
    "SIGMA_TOKEN_MINTED_AT": MINTED,
    "SIGMA_AUTH_METHOD": "browser",
}


class ProviderContractTest(unittest.TestCase):
    def test_browser_mode_uses_keychain_cache_without_client_credentials(self):
        keychain = {
            "refresh-token": "refresh-token",
            "access-token": "browser-token",
            "access-expiry": "2000",
            "access-minted-at": MINTED,
        }
        with mock.patch.dict(
            os.environ, {"SIGMA_BASE_URL": BASE}, clear=True
        ), mock.patch.object(
            get_token, "_load_neutral_env"
        ), mock.patch.object(
            get_token, "_keychain_backend", return_value="libsecret"
        ), mock.patch.object(
            get_token,
            "_kc_get",
            side_effect=lambda _backend, key: keychain.get(key, ""),
        ), mock.patch.object(
            get_token.time, "time", return_value=1000
        ), mock.patch.object(
            get_token, "_mint_client_credentials"
        ) as client, mock.patch.object(
            get_token, "_verify_token", side_effect=lambda result: result
        ):
            result = get_token.mint_token("browser")

        self.assertEqual("browser-token", result.token)
        self.assertEqual("browser", result.auth_method)
        self.assertEqual(MINTED, result.minted_at)
        client.assert_not_called()

    def test_auto_falls_back_to_client_credentials(self):
        expected = get_token.TokenResult(
            BASE, "client-token", MINTED, "client-credentials"
        )
        with mock.patch.dict(
            os.environ,
            {
                "SIGMA_BASE_URL": BASE,
                "SIGMA_CLIENT_ID": "client-id",
                "SIGMA_CLIENT_SECRET": "client-secret",
            },
            clear=True,
        ), mock.patch.object(
            get_token, "_load_neutral_env"
        ), mock.patch.object(
            get_token,
            "_mint_browser_refresh",
            side_effect=get_token.BrowserUnavailable("no keychain"),
        ), mock.patch.object(
            get_token, "_mint_client_credentials", return_value=expected
        ) as client, mock.patch.object(
            get_token, "_verify_token", side_effect=lambda result: result
        ):
            result = get_token.mint_token()

        self.assertEqual(expected, result)
        client.assert_called_once_with(BASE, "client-id", "client-secret")


class RestAuthenticationContractTest(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(
            os.environ,
            {"SIGMA_BASE_URL": BASE, "SIGMA_API_TOKEN": "caller-token"},
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        sigma_rest.reset_runtime_state()
        self.addCleanup(sigma_rest.reset_runtime_state)

    def test_valid_environment_bearer_is_used_without_provider(self):
        with mock.patch.object(
            sigma_rest,
            "_send",
            return_value=sigma_rest._Resp(200, b'{"ok":true}'),
        ) as send, mock.patch.object(
            sigma_rest, "token_provider_result"
        ) as provider:
            result = sigma_rest.request("get", "/v2/whoami")

        self.assertEqual({"ok": True}, result)
        self.assertEqual(
            "Bearer caller-token", send.call_args.args[2]["Authorization"]
        )
        provider.assert_not_called()

    def test_auth_json_bearer_and_metadata_are_reused(self):
        with tempfile.TemporaryDirectory() as workdir:
            auth_path = Path(workdir) / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "SIGMA_API_TOKEN": "file-token",
                        "SIGMA_BASE_URL": BASE,
                        "SIGMA_TOKEN_MINTED_AT": MINTED,
                        "SIGMA_AUTH_METHOD": "browser",
                    }
                ),
                encoding="utf-8",
            )
            os.environ.clear()
            sigma_rest._load_auth_json(cwd=workdir)
            with mock.patch.object(
                sigma_rest,
                "_send",
                return_value=sigma_rest._Resp(200, b'{"ok":true}'),
            ) as send, mock.patch.object(
                sigma_rest, "token_provider_result"
            ) as provider:
                result = sigma_rest.request("get", "/v2/whoami")

        self.assertEqual({"ok": True}, result)
        self.assertEqual(
            "Bearer file-token", send.call_args.args[2]["Authorization"]
        )
        self.assertEqual("browser", os.environ["SIGMA_AUTH_METHOD"])
        provider.assert_not_called()

    def test_known_stale_token_refreshes_proactively(self):
        os.environ["SIGMA_TOKEN_MINTED_AT"] = "2000-01-01T00:00:00Z"
        with mock.patch.object(
            sigma_rest, "token_provider_result", return_value=PROVIDER
        ) as provider:
            token = sigma_rest.auth_token()

        self.assertEqual("refreshed-token", token)
        provider.assert_called_once_with()

    def test_401_refreshes_once_and_retries_with_new_bearer(self):
        responses = [
            sigma_rest._Resp(401, b"expired", "Unauthorized"),
            sigma_rest._Resp(200, b'{"ok":true}'),
        ]
        authorizations = []

        def send(_method, _url, headers, _body, _timeout):
            authorizations.append(headers["Authorization"])
            return responses.pop(0)

        with mock.patch.object(
            sigma_rest, "_send", side_effect=send
        ), mock.patch.object(
            sigma_rest, "token_provider_result", return_value=PROVIDER
        ) as provider:
            result = sigma_rest.request("get", "/v2/whoami")

        self.assertEqual({"ok": True}, result)
        self.assertEqual(
            ["Bearer caller-token", "Bearer refreshed-token"],
            authorizations,
        )
        provider.assert_called_once_with()

    def test_second_401_is_not_retried(self):
        responses = [
            sigma_rest._Resp(401, b"expired", "Unauthorized"),
            sigma_rest._Resp(401, b"denied", "Unauthorized"),
        ]
        with mock.patch.object(
            sigma_rest, "_send", side_effect=lambda *_args: responses.pop(0)
        ), mock.patch.object(
            sigma_rest, "token_provider_result", return_value=PROVIDER
        ):
            with self.assertRaises(sigma_rest.SigmaHttpError) as raised:
                sigma_rest.request("get", "/v2/whoami")

        self.assertEqual(401, raised.exception.status)
        self.assertEqual([], responses)

    def test_unsafe_host_is_rejected_before_bearer_send(self):
        os.environ["SIGMA_BASE_URL"] = "https://api.sigmacomputing.com.evil.test"
        with mock.patch.object(sigma_rest, "_send") as send:
            with self.assertRaises(sigma_rest.SigmaSecurityError):
                sigma_rest.request("get", "/v2/whoami")
        send.assert_not_called()

    def test_absolute_request_url_is_rejected(self):
        with mock.patch.object(sigma_rest, "_send") as send:
            with self.assertRaisesRegex(
                sigma_rest.SigmaSecurityError, "origin-relative"
            ):
                sigma_rest.request("get", "https://attacker.example/collect")
        send.assert_not_called()

    def test_redirect_handler_refuses_without_forwarding_authorization(self):
        handler = sigma_rest.RejectRedirectHandler()
        request = urllib.request.Request(
            f"{BASE}/v2/whoami",
            headers={"Authorization": "Bearer secret-token"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            handler.redirect_request(
                request,
                io.BytesIO(),
                302,
                "Found",
                {"Location": "https://attacker.example/collect"},
                "https://attacker.example/collect",
            )

        self.assertEqual(302, raised.exception.code)
        self.assertIn("redirect refused", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
