"""Streamable HTTP 静态 Bearer 鉴权回归测试。

只测试鉴权切片，不启动完整 56 工具 server，也不碰真实 collab 目录。
"""

import asyncio
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from collab_mcp.http_auth import (
    ENV_HTTP_ALLOWED_IDENTITIES,
    ENV_HTTP_IDENTITY,
    ENV_HTTP_MODE,
    ENV_HTTP_TOKEN,
    StaticBearerTokenVerifier,
    allowed_http_identities,
    build_auth_settings,
    build_token_verifier,
    validate_http_identity,
)

for _env in (ENV_HTTP_TOKEN, ENV_HTTP_IDENTITY, ENV_HTTP_ALLOWED_IDENTITIES):
    os.environ.pop(_env, None)


def _make_app(verifier: StaticBearerTokenVerifier):
    return MCPServer(
        name="http-auth-test",
        token_verifier=verifier,
        auth=build_auth_settings(),
    ).streamable_http_app(
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


class StaticBearerVerifierTest(unittest.TestCase):
    def test_accepts_correct_token_and_maps_identity(self):
        verifier = StaticBearerTokenVerifier("secret-token", "PC-B")
        token = asyncio.run(verifier.verify_token("secret-token"))
        self.assertIsNotNone(token)
        self.assertEqual(token.client_id, "PC-B")
        self.assertEqual(token.subject, "PC-B")
        self.assertEqual(token.claims, {"collab_identity": "PC-B"})

    def test_rejects_wrong_or_empty_token(self):
        verifier = StaticBearerTokenVerifier("secret-token", "PC-B")
        self.assertIsNone(asyncio.run(verifier.verify_token("wrong")))
        self.assertIsNone(asyncio.run(verifier.verify_token("")))


class StaticBearerConfigTest(unittest.TestCase):
    def test_build_token_verifier_requires_configured_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_HTTP_TOKEN, None)
            self.assertIsNone(build_token_verifier())

        with patch.dict(os.environ, {ENV_HTTP_TOKEN: "abc", ENV_HTTP_IDENTITY: "PC-C"}):
            verifier = build_token_verifier()
            self.assertIsInstance(verifier, StaticBearerTokenVerifier)

    def test_auth_settings_have_empty_required_scopes(self):
        self.assertEqual(build_auth_settings().required_scopes, [])


class StreamableHttpAuthTest(unittest.TestCase):
    def test_missing_bearer_returns_401(self):
        app = _make_app(StaticBearerTokenVerifier("abc", "PC-A"))
        with TestClient(app) as client:
            resp = client.post("/mcp", json={})
            self.assertEqual(resp.status_code, 401)
            self.assertIn("invalid_token", resp.text)

    def test_valid_bearer_reaches_mcp_handler(self):
        app = _make_app(StaticBearerTokenVerifier("abc", "PC-A"))
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        }
        with TestClient(app) as client:
            resp = client.post(
                "/mcp",
                json=payload,
                headers={"Authorization": "Bearer abc"},
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body.get("jsonrpc"), "2.0")
            self.assertIn("result", body)


class IdentityAllowlistTest(unittest.TestCase):
    """COLLAB_HTTP_ALLOWED_IDENTITIES 启动白名单校验。"""

    def test_absent_env_skips_validation(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_HTTP_ALLOWED_IDENTITIES, None)
            os.environ.pop(ENV_HTTP_IDENTITY, None)
            self.assertIsNone(allowed_http_identities())
            self.assertIsNone(validate_http_identity())

    def test_identity_in_allowlist_passes(self):
        env = {
            ENV_HTTP_IDENTITY: "PC-B",
            ENV_HTTP_ALLOWED_IDENTITIES: "PC-A, PC-B ,PC-C",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(
                allowed_http_identities(), ["PC-A", "PC-B", "PC-C"]
            )
            self.assertIsNone(validate_http_identity())

    def test_identity_not_in_allowlist_rejected(self):
        env = {
            ENV_HTTP_IDENTITY: "PC-X",
            ENV_HTTP_ALLOWED_IDENTITIES: "PC-A,PC-B",
        }
        with patch.dict(os.environ, env, clear=False):
            reason = validate_http_identity()
            self.assertIsNotNone(reason)
            self.assertIn("PC-X", reason)
            self.assertIn(ENV_HTTP_ALLOWED_IDENTITIES, reason)

    def test_default_identity_honors_allowlist(self):
        # 未显式设身份时走默认 PC-A，仍须受白名单约束。
        with patch.dict(os.environ, {ENV_HTTP_ALLOWED_IDENTITIES: "PC-B,PC-C"}, clear=False):
            os.environ.pop(ENV_HTTP_IDENTITY, None)
            reason = validate_http_identity()
            self.assertIsNotNone(reason)
            self.assertIn("PC-A", reason)

    def test_empty_allowlist_rejects_any_identity(self):
        env = {
            ENV_HTTP_IDENTITY: "PC-B",
            ENV_HTTP_ALLOWED_IDENTITIES: " , ",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(allowed_http_identities(), [])
            reason = validate_http_identity()
            self.assertIsNotNone(reason)
            self.assertIn("白名单为空", reason)


class HttpStartupWiringTest(unittest.TestCase):
    """v3.36.3：server.py 启动顺序——校验先于 app 构建（verifier 用被校验身份）。"""

    SERVER = Path(__file__).resolve().parent.parent / "server.py"

    def _run(self, args, extra_env):
        # 剥掉环境里的 COLLAB_*（SSH 注入的 COLLAB_IDENTITY/COLLAB_DIR 等），保证本用例可控
        env = {k: v for k, v in os.environ.items() if not k.startswith("COLLAB_")}
        env["PYTHONUTF8"] = "1"
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(self.SERVER), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=60,
        )

    def test_version_ignores_misconfigured_allowlist(self):
        # 非 HTTP 模式不应被白名单配置阻断（校验只在 HTTP 分支内生效）
        p = self._run(["--version"], {
            ENV_HTTP_IDENTITY: "PC-X",
            ENV_HTTP_ALLOWED_IDENTITIES: "PC-A",
        })
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Claude 协作 MCP Server v", p.stdout)

    def test_http_bad_identity_exits_before_serving(self):
        # 身份不在白名单 → 拒绝启动且不进入服务态（app 构建发生在此校验之后）
        p = self._run([], {
            ENV_HTTP_MODE: "1",
            ENV_HTTP_TOKEN: "smoke-token",
            ENV_HTTP_IDENTITY: "PC-X",
            ENV_HTTP_ALLOWED_IDENTITIES: "PC-A,PC-B",
        })
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("白名单", p.stderr)
        self.assertNotIn("已启动", p.stdout)

    def test_http_missing_token_exits(self):
        p = self._run([], {
            ENV_HTTP_MODE: "1",
            ENV_HTTP_IDENTITY: "PC-A",
            ENV_HTTP_ALLOWED_IDENTITIES: "PC-A",
        })
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn(ENV_HTTP_TOKEN, p.stderr)


if __name__ == "__main__":
    unittest.main()
