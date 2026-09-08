"""可选 Streamable HTTP 入口的静态 Bearer Token 鉴权。

设计约束：默认部署仍走 SSH stdio + COLLAB_IDENTITY。只有显式打开
COLLAB_HTTP 并设置 COLLAB_HTTP_BEARER_TOKEN 时，MCP Server 才会启用
HTTP 传输；否则现有 stdio 路径完全不变。

静态 token 模型不替代 SSH authorized_keys 身份体系，而是给「无需
逐队友配公钥」的可信内网/本机 API 入口一个最小门禁：一个 token
对应一个明确的协作身份。

可选加固：设置 COLLAB_HTTP_ALLOWED_IDENTITIES（逗号分隔身份白名单）
后，HTTP 启动时校验映射身份必须在白名单内，防止 token 被误映射到
任意未授权身份；未设置时行为与 v3.36.0 完全一致。
"""

import os
import secrets

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

ENV_HTTP_MODE = "COLLAB_HTTP"
ENV_HTTP_TOKEN = "COLLAB_HTTP_BEARER_TOKEN"
ENV_HTTP_IDENTITY = "COLLAB_HTTP_IDENTITY"
ENV_HTTP_ALLOWED_IDENTITIES = "COLLAB_HTTP_ALLOWED_IDENTITIES"
DEFAULT_HTTP_IDENTITY = "PC-A"


class StaticBearerTokenVerifier:
    """比较固定的明文 Bearer Token，并映射到单一协作身份。"""

    def __init__(self, token: str, identity: str) -> None:
        self._token = token
        self._identity = identity

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not secrets.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id=self._identity,
            scopes=[],
            subject=self._identity,
            claims={"collab_identity": self._identity},
        )


def http_identity() -> str:
    """返回 HTTP token 对应的协作身份；未配置 token 时也给出默认值。"""
    return (os.environ.get(ENV_HTTP_IDENTITY) or "").strip() or DEFAULT_HTTP_IDENTITY


def allowed_http_identities() -> list[str] | None:
    """解析 COLLAB_HTTP_ALLOWED_IDENTITIES 逗号白名单。

    环境变量未设置时返回 None（表示不启用白名单校验，保持旧行为）；
    设置但解析后为空列表表示配置错误，由 validate_http_identity 拒绝启动。
    """
    raw = os.environ.get(ENV_HTTP_ALLOWED_IDENTITIES)
    if raw is None:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def validate_http_identity() -> str | None:
    """HTTP 启动前的身份白名单校验；返回 None 表示通过，否则返回拒绝原因。

    目的：防止 Bearer token 被误映射到任意未授权身份（例如把
    COLLAB_HTTP_IDENTITY 设成从未注册的名字，或白名单配置为空）。
    """
    allowed = allowed_http_identities()
    if allowed is None:
        return None
    identity = http_identity()
    if not allowed:
        return (
            f"{ENV_HTTP_ALLOWED_IDENTITIES} 已设置但白名单为空，"
            f"拒绝以身份 {identity} 启动 HTTP 入口。"
        )
    if identity not in allowed:
        return (
            f"HTTP 身份 {identity} 不在 {ENV_HTTP_ALLOWED_IDENTITIES} "
            f"白名单 {allowed} 内，拒绝启动。"
        )
    return None


def configured_http_token() -> str | None:
    token = os.environ.get(ENV_HTTP_TOKEN, "").strip()
    return token or None


def build_auth_settings() -> AuthSettings:
    """构造静态 Bearer 鉴权所需的最小 AuthSettings。

    issuer_url/resource_server_url 只用于 OAuth 元数据；本实现不启动授权
    服务器，token 由环境变量预共享。使用 .invalid 保留域避免被误认为真实
    外部端点。
    """
    return AuthSettings(
        issuer_url="https://collab.invalid/oauth",
        resource_server_url="https://collab.invalid/mcp",
        required_scopes=[],
    )


def build_token_verifier() -> StaticBearerTokenVerifier | None:
    token = configured_http_token()
    if not token:
        return None
    return StaticBearerTokenVerifier(token, http_identity())
