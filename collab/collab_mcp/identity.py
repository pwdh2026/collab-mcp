"""调用者身份识别 — 基于 SSH 公钥映射的 COLLAB_IDENTITY。

架构背景：所有队友都经中枢机端口转发（8022）连到 VM，从 VM 看
SSH_CONNECTION 都是同一个来源地址，无法靠 IP 区分是谁。因此身份必须
由 SSH 公钥决定：

1. VM 的 /root/.ssh/authorized_keys 里为每个队友的公钥加前缀：
       environment="COLLAB_IDENTITY=PC-B",no-port-forwarding ssh-ed25519 AAAA... PC-B
2. sshd_config 开启 PermitUserEnvironment yes
3. server.py 由该 SSH 会话启动，os.environ['COLLAB_IDENTITY'] 即调用者身份

未设置该变量时视为「本地 / 未知身份」：为兼容历史行为不做强制过滤，
但工具响应会带 identity 字段方便排查。

生产环境建议设置 REQUIRE_IDENTITY=1：未绑定身份（COLLAB_IDENTITY 为空）的
会话将被拒绝访问任务与注册类工具，彻底消除"空身份=全权限"的过渡期风险。
"""

import os

# 中枢身份约定（可用 COLLAB_ROLE=hub 覆盖，供自定义中枢名）
HUB_IDENTITY = "PC-A"


def current_identity() -> str:
    """返回当前 SSH 会话对应的队友身份，未设置时返回空字符串。"""
    return os.environ.get("COLLAB_IDENTITY", "").strip()


def is_hub() -> bool:
    """是否为中枢（拥有全量权限）。"""
    ident = current_identity()
    return ident == HUB_IDENTITY or os.environ.get("COLLAB_ROLE", "").strip() == "hub"


def identity_note() -> dict:
    """供工具响应携带的身份信息。"""
    ident = current_identity()
    if ident:
        return {"identity": ident, "role": "hub" if is_hub() else "teammate"}
    return {"identity": None, "role": "local"}


def require_identity() -> bool:
    """是否强制要求身份（REQUIRE_IDENTITY=1/true/yes）。"""
    return os.environ.get("REQUIRE_IDENTITY", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def assert_identity_allowed(action: str) -> str | None:
    """返回 None 表示放行，否则返回拒绝原因（供工具直接 fail）。"""
    ident = current_identity()
    if not ident and require_identity():
        msg = (
            f"{action} 需要调用者身份：请在中枢 VM 的 authorized_keys 中为你的"
            f"公钥绑定 environment=\"COLLAB_IDENTITY=<名字>\"（并设置 "
            f"REQUIRE_IDENTITY=1 时强制生效）。"
        )
        _audit_deny(action, msg)
        return msg
    return None


def _audit_deny(action: str, reason: str) -> None:
    """权限拒绝审计日志（含身份，便于排查越权尝试）。"""
    from .logging_setup import logger

    logger.warning(f"⛔ 权限拒绝 [{action}] identity={current_identity() or 'unset'} — {reason}")
