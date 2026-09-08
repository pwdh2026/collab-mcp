#!/usr/bin/env python3
"""主动监控：环境健康巡检 + 告警（v3.8.0）。

设计来源：progress/collab-v3.8-health-monitor-design.md
盘点：notify_daemon（任务级 stale/claim）与 watchdog（wigolo/portproxy 保活）均不覆盖
「环境健康巡检 + 告警」——本脚本补齐：wigolo /health、scrapling 可用性、git 同步、
磁盘空间、inbox 积压五项巡检。

输出：
- state：results/health-monitor-state.json（last_status 翻转去重 + healthy_hour）
- log：results/health-monitor.log（异常/恢复全记，正常每小时一条）
- feed：状态翻转时写 notifications/feed.jsonl（type=health_alert；恢复也写 status=ok）
  → 队友经 get_notifications 可见；去重=仅状态翻转写，避免刷屏。

部署：Windows 计划任务每 5 分钟跑一次（pythonw.exe，同看门狗约定）；
环境变量覆盖（测试用）：HM_REPO / HM_STATE / HM_LOG / HM_FEED / HM_WIGOLO_URL /
HM_DISK_MIN_GB / HM_INBOX_MAX / HM_INBOX_DIR / HM_WATCH_INTERVAL。
"""

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_REPO = r"C:\myshare"
DEFAULT_STATE = r"C:\myshare\results\health-monitor-state.json"
DEFAULT_LOG = r"C:\myshare\results\health-monitor.log"
DEFAULT_FEED = r"C:\myshare\collab\notifications\feed.jsonl"
DEFAULT_WIGOLO = "http://127.0.0.1:3333/health"
DEFAULT_DISK_MIN_GB = 5
DEFAULT_INBOX_MAX = 20
DEFAULT_INBOX_DIR = r"C:\myshare\collab\inbox"
TIMEOUT = 5
MAX_FEED_LINES = 2000


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _as_check(ok: bool, detail: str) -> dict:
    return {"ok": bool(ok), "detail": str(detail)}


def check_wigolo(url: str) -> tuple[bool, str]:
    """探测 wigolo serve /health（status=healthy）。"""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if data.get("status") == "healthy":
            up = data.get("uptime_seconds")
            return True, f"healthy（uptime {up}s）" if up is not None else "healthy"
        return False, f"status={data.get('status')!r}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:120]


def _find_chromium() -> Path | None:
    """chromium 目录探测（对齐 v3.4 语义：ms-playwright/chromium-<build>/chrome-win64|win）。

    低1（PC-C v3.8）：标准 Playwright 布局为 chromium-<build>/ 版本号子目录，
    必须 glob("chromium-*") 进子目录再找 chrome.exe，固定目录名会探测不到（死检查）。
    """
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    root = Path(env) if env else Path.home() / "AppData" / "Local" / "ms-playwright"
    if not root.exists():
        return None
    for d in root.glob("chromium-*"):
        for name in ("chrome-win64", "chrome-win"):
            exe = d / name / "chrome.exe"
            if exe.exists():
                return d
    return None


def check_scrapling() -> tuple[bool, str]:
    """scrapling 可用性：fetchers 可导入 + chromium 目录（浏览器路径）。"""
    try:
        spec = importlib.util.find_spec("scrapling.fetchers")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return False, "scrapling.fetchers 未安装（HTTP 兜底不可用）"
    if _find_chromium():
        return True, "import OK + chromium 可用（HTTP/浏览器路径均可用）"
    return True, "import OK；chromium 未探测到（仅 HTTP 路径可用）"


def _git_rev(repo: str, ref: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo, "rev-parse", ref],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def check_git(repo: str) -> tuple[bool, str]:
    """git 同步：HEAD vs origin/master（本地 refs 只读比较，不 fetch）。"""
    head = _git_rev(repo, "HEAD")
    origin = _git_rev(repo, "origin/master")
    if not head:
        return False, f"无法读取 HEAD（{repo}）"
    if not origin:
        return False, "origin/master 本地 ref 缺失（未 fetch？）"
    if head == origin:
        return True, f"HEAD==origin/master（{head[:8]}）"
    return False, f"HEAD({head[:8]}) != origin/master({origin[:8]})"


def check_disk(min_gb: float, base: str) -> tuple[bool, str]:
    """磁盘可用空间。"""
    try:
        drive = os.path.splitdrive(os.path.abspath(base))[0] + os.sep
        free_gb = shutil.disk_usage(drive).free / (1024 ** 3)
        return free_gb >= min_gb, f"{free_gb:.1f} GB（阈值 {min_gb:.0f} GB）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:120]


def check_inbox(inbox_dir: str, max_count: int) -> tuple[bool, str]:
    """inbox 积压计数。"""
    try:
        n = len(list(Path(inbox_dir).glob("*.json")))
        return n <= max_count, f"{n} 个待办（阈值 {max_count}）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:120]


def default_cfg() -> dict:
    return {
        "repo": os.environ.get("HM_REPO", DEFAULT_REPO),
        "state": os.environ.get("HM_STATE", DEFAULT_STATE),
        "log": os.environ.get("HM_LOG", DEFAULT_LOG),
        "feed": os.environ.get("HM_FEED", DEFAULT_FEED),
        "wigolo_url": os.environ.get("HM_WIGOLO_URL", DEFAULT_WIGOLO),
        "disk_min_gb": float(os.environ.get("HM_DISK_MIN_GB", str(DEFAULT_DISK_MIN_GB))),
        "inbox_max": int(os.environ.get("HM_INBOX_MAX", str(DEFAULT_INBOX_MAX))),
        "inbox_dir": os.environ.get("HM_INBOX_DIR", DEFAULT_INBOX_DIR),
    }


def run_once(cfg: dict) -> dict:
    """单次巡检，返回 {ts, all_ok, checks}。"""
    checks = {
        "wigolo": _as_check(*check_wigolo(cfg["wigolo_url"])),
        "scrapling": _as_check(*check_scrapling()),
        "git": _as_check(*check_git(cfg["repo"])),
        "disk": _as_check(*check_disk(cfg["disk_min_gb"], cfg["repo"])),
        "inbox": _as_check(*check_inbox(cfg["inbox_dir"], cfg["inbox_max"])),
    }
    return {
        "ts": now(),
        "all_ok": all(c["ok"] for c in checks.values()),
        "checks": checks,
    }


def _load_state(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}
    except Exception:
        return {}


def _write_state(path: str, state: dict) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _append_feed(feed_path: str, events: list[dict]) -> None:
    feed = Path(feed_path)
    try:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        lines = feed.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > MAX_FEED_LINES:
            feed.write_text("\n".join(lines[-MAX_FEED_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def emit(cfg: dict, result: dict) -> list[dict]:
    """翻转去重：仅状态翻转写 feed 事件；更新 state last_status。返回写的事件。"""
    state = _load_state(cfg["state"])
    last = state.get("last_status", {})
    events = []
    for name, c in result["checks"].items():
        status = "ok" if c["ok"] else "fail"
        if last.get(name) == status:
            continue
        # 首次基线：ok 静默建立（不刷 feed），fail 才告警
        if name not in last and status == "ok":
            continue
        events.append({
            "id": hashlib.sha1(f"{result['ts']}:{name}:{status}".encode("utf-8")).hexdigest()[:12],
            "ts": result["ts"],
            "type": "health_alert",
            "check": name,
            "status": status,
            "detail": c["detail"],
        })
    if events:
        _append_feed(cfg["feed"], events)
    state["last_status"] = {name: ("ok" if c["ok"] else "fail") for name, c in result["checks"].items()}
    state["ts"] = result["ts"]
    _write_state(cfg["state"], state)
    return events


def log_result(cfg: dict, result: dict, events: list[dict]) -> None:
    """异常/恢复全记；正常每小时一条。"""
    log = Path(cfg["log"])
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        fails = [n for n, c in result["checks"].items() if not c["ok"]]
        with open(log, "a", encoding="utf-8") as f:
            if fails:
                f.write(f"{result['ts']} ALERT 异常项: {', '.join(fails)}；事件 {len(events)} 条\n")
                for name, c in result["checks"].items():
                    if not c["ok"]:
                        f.write(f"  - {name}: {c['detail']}\n")
            else:
                state = _load_state(cfg["state"])
                hour = datetime.now().strftime("%Y%m%d%H")
                if state.get("healthy_hour") != hour:
                    f.write(f"{result['ts']} HEALTHY 全部通过\n")
                    state["healthy_hour"] = hour
                    _write_state(cfg["state"], state)
    except OSError as e:
        print(f"health-monitor log error: {e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="主动监控：环境健康巡检 + 告警（v3.8.0）")
    parser.add_argument("--once", action="store_true", help="单次巡检（默认）")
    parser.add_argument("--watch", action="store_true", help="循环巡检")
    parser.add_argument("--interval", type=int, default=300, help="循环间隔秒（默认 300）")
    args = parser.parse_args(argv)
    cfg = default_cfg()
    if args.watch:
        interval = max(10, args.interval)
        try:
            while True:
                r = run_once(cfg)
                events = emit(cfg, r)
                log_result(cfg, r, events)
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0
        return 0
    r = run_once(cfg)
    events = emit(cfg, r)
    log_result(cfg, r, events)
    return 0 if r["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
