"""眼睛侧观察采集脚本：vision_capture.py（v3.12 眼睛-大脑 Step 1；v3.14 新增 --watch 连续观察）。

设计来源：progress/collab-v3.12-vision-eyes-brain-design.md
从摄像头/静态图片采集一帧，做基础感知（亮度/主色/运动 + 可选 Haar 人脸检测），
产出**结构化观察**（observation-<id>.json + frame-<id>.jpg + latest.json）写入共享目录，
供大脑侧 MCP 工具 analyze_observation 读取推理。

v3.14 Step 3：--watch 连续观察——按 --interval 秒周期采集，滚动用上一帧算运动差异，
每轮写入 observation + latest.json；Ctrl+C 优雅退出。

依赖：OpenCV **可选**（惰性 import；缺 cv2 时 --demo 仍可产出观察，便于离线联调/测试）。
用法（PowerShell 直跑，无需 .ps1）：
    python collab/scripts/vision_capture.py --camera 0
    python collab/scripts/vision_capture.py --image some.jpg
    python collab/scripts/vision_capture.py --demo
    python collab/scripts/vision_capture.py --camera 0 --prev prev.jpg --out-dir C:\\myshare\\collab\\vision
    python collab/scripts/vision_capture.py --watch --camera 0 --interval 5 --rounds 100
    python collab/scripts/vision_capture.py --watch --demo --interval 2 --rounds 10 --json
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

SCHEMA = "collab-vision-observation-v1"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "vision"  # collab/vision/

_INTERVAL_MIN = 1
_INTERVAL_MAX = 3600


def _now_local_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _atomic_write_json(path: Path, data: dict) -> bool:
    """原子写入 JSON（同目录 .tmp 再 os.replace），避免共享盘写一半。"""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (IOError, OSError) as e:
        print(f"[error] 写入失败 {path}: {e}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _load_cv2():
    """惰性加载 cv2（返回模块或 None）。"""
    try:
        import cv2  # type: ignore
        return cv2
    except ImportError:
        return None


def _imread_utf8(cv2, path):
    """cv2.imread 的 UTF-8 路径版本（Windows 非 ASCII 路径兼容；v3.15.1 吸收 PC-C 中危）。"""
    import numpy as np  # noqa: PLC0415（cv2 的硬依赖，惰性导入）

    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _imwrite_utf8(cv2, path, frame) -> bool:
    """cv2.imwrite 的 UTF-8 路径版本（Windows 非 ASCII 路径兼容；v3.15.1 吸收 PC-C 中危）。"""
    import numpy as np  # noqa: PLC0415

    ext = Path(path).suffix.lower() or ".jpg"
    ok, buf = cv2.imencode(ext, frame)
    if not ok:
        return False
    buf.tofile(str(path))
    return True


def _dominant_color(cv2, frame) -> str:
    """主色：RGB 每通道量化到 4 级（16^3 桶），取最频桶的中间值 → #rrggbb。"""
    import collections
    q = frame[:, :, ::-1] // 64  # BGR->RGB 再量化
    counts = collections.Counter(map(tuple, q.reshape(-1, 3)))
    (r, g, b), _ = counts.most_common(1)[0]
    return "#%02x%02x%02x" % (r * 64 + 32, g * 64 + 32, b * 64 + 32)


def _dominant_colors(cv2, frame) -> list:
    """top-3 颜色桶（v3.15 感知增强）：RGB 量化 4 级桶，返回 [{color, share}] 占比降序。"""
    import collections
    q = frame[:, :, ::-1] // 64  # BGR->RGB 再量化
    counts = collections.Counter(map(tuple, q.reshape(-1, 3)))
    total = len(q.reshape(-1, 3))
    out = []
    for (r, g, b), n in counts.most_common(3):
        out.append({"color": "#%02x%02x%02x" % (r * 64 + 32, g * 64 + 32, b * 64 + 32),
                    "share": round(n / total, 3)})
    return out


def _detect_faces(cv2, gray):
    """Haar 正面人脸检测（级联文件由 opencv-python 自带，无外网依赖）。"""
    cascade_path = Path(getattr(cv2.data, "haarcascades", "")) / "haarcascade_frontalface_default.xml"
    if not cascade_path.exists():
        return [], ["Haar 级联文件缺失，跳过人脸检测"]
    cascade = cv2.CascadeClassifier(str(cascade_path))
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
    # Haar 无标定置信度，统一记 0.5（heuristic）
    return [{"kind": "face", "confidence": 0.5, "bbox": [int(x), int(y), int(w), int(h)]}
            for (x, y, w, h) in faces], []


def _motion_diff(cv2, prev, cur) -> float:
    """与上一帧的归一化平均绝对差（0..1）；尺寸不一致返回 0。"""
    if prev is None or prev.shape != cur.shape:
        return 0.0
    diff = cv2.absdiff(prev, cur)
    return float(diff.mean() / 255.0)


def _analyze(cv2, frame, prev) -> tuple:
    """感知分析（v3.15 增强）：除亮度/运动/主色外，新增 HSV 饱和度与亮度、边缘密度、
    灰度方差、top-3 颜色桶——给大脑更丰富的场景特征（区分开灯/放目标/转视角等）。"""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    edges = cv2.Canny(gray, 100, 200)
    stats = {
        "width": int(frame.shape[1]),
        "height": int(frame.shape[0]),
        "brightness": round(float(gray.mean() / 255.0), 4),
        "motion": round(_motion_diff(cv2, prev, frame), 4),
        "dominant_color": _dominant_color(cv2, frame),
        "sat_mean": round(float(hsv[:, :, 1].mean()), 3),
        "val_mean": round(float(hsv[:, :, 2].mean()), 3),
        "edge_density": round(float((edges > 0).mean()), 4),
        "gray_var": round(float(gray.var()), 1),
        "dominant_colors": _dominant_colors(cv2, frame),
    }
    detections, notes = _detect_faces(cv2, gray)
    return stats, detections, notes


def _capture(cv2, camera_index: int):
    cap = cv2.VideoCapture(int(camera_index))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开摄像头 #{camera_index}（检查设备/权限）")
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise RuntimeError(f"摄像头 #{camera_index} 读取帧失败")
    return frame


def _build_observation(cv2, frame, source: str, prev, synthetic: bool, out_dir: Path) -> dict:
    obs_id = "obs-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    if frame is not None:
        stats, detections, notes = _analyze(cv2, frame, prev)
    else:
        stats = {"width": 0, "height": 0, "brightness": 0.0, "motion": 0.0, "dominant_color": "#000000",
                 "sat_mean": 0.0, "val_mean": 0.0, "edge_density": 0.0, "gray_var": 0.0,
                 "dominant_colors": []}
        detections = []
        notes = ["demo 模式：未采集真实帧（可用 --camera/--image 采集真实画面）"]
    obs = {
        "schema": SCHEMA,
        "observation_id": obs_id,
        "timestamp": _now_local_iso(),
        "source": source,
        "image_path": "",
        "synthetic": bool(synthetic),
        "stats": stats,
        "detections": detections,
        "capture_duration_ms": 0,
        "notes": notes,
    }
    if frame is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        frame_path = out_dir / f"frame-{obs_id}.jpg"
        if not _imwrite_utf8(cv2, frame_path, frame):
            raise RuntimeError(f"帧写入失败: {frame_path}")
        # v3.25 语义统一（PC-C v3.23 低-1）：image_path 相对观察 JSON 所在目录（与帧共置，
        # 即纯文件名）；消费侧 vision._image_data_uri 按观察文件目录解析，旧格式可回退。
        obs["image_path"] = frame_path.name
    return obs


def _acquire_frame(args, cv2):
    """按参数采集一帧；demo 返回 (None,'demo',True,0)；失败抛 RuntimeError。

    v3.14 重构：把单帧采集逻辑从 main 抽出，供单次与 --watch 循环复用。
    """
    if args.demo:
        return None, "demo", True, 0
    if cv2 is None:
        raise RuntimeError("未安装 OpenCV。请安装后重试：pip install opencv-python-headless")
    if args.camera is not None and args.image:
        raise RuntimeError("--camera 与 --image 只能二选一")
    if args.image:
        img = Path(args.image)
        if not img.exists():
            raise RuntimeError(f"图片不存在: {img}")
        t0 = time.time()
        frame = _imread_utf8(cv2, img)
        if frame is None:
            raise RuntimeError(f"无法解码图片: {img}")
        source = f"image:{img}"
        synthetic = False
    else:
        idx = args.camera if args.camera is not None else 0
        t0 = time.time()
        frame = _capture(cv2, idx)
        source = f"camera:{idx}"
        synthetic = False
    duration_ms = int((time.time() - t0) * 1000)
    return frame, source, synthetic, duration_ms


def _load_prev(args, cv2):
    """加载 --prev 静态上一帧（仅首轮用；watch 后续轮次滚动传 frame）。"""
    if not args.prev:
        return None
    p = Path(args.prev)
    if not p.exists():
        return None
    if cv2 is None:
        return None
    return _imread_utf8(cv2, p)


def _run_once(args, cv2, prev, out_dir):
    """采集一帧 → 构建观察 → 写 observation + latest。返回 (rc, obs, frame)。"""
    try:
        frame, source, synthetic, duration_ms = _acquire_frame(args, cv2)
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2, None, None
    obs = _build_observation(cv2, frame, source, prev, synthetic, out_dir)
    if frame is not None:
        obs["capture_duration_ms"] = duration_ms
    obs_path = out_dir / f"observation-{obs['observation_id']}.json"
    if not _atomic_write_json(obs_path, obs):
        return 1, None, None
    if not _atomic_write_json(out_dir / "latest.json", obs):
        return 1, None, None
    return 0, obs, frame


def _print_obs(obs: dict, json_mode: bool) -> None:
    """打印一次观察结果（--json 逐行 JSON；否则紧凑摘要）。"""
    if json_mode:
        print(json.dumps(obs, ensure_ascii=False))
        return
    print(f"[ok] {obs['observation_id']} source={obs['source']} "
          f"stats={obs['stats']} detections={len(obs.get('detections') or [])} "
          f"image={obs['image_path'] or '(无)'}")


def main(argv=None) -> int:
    # Windows 管道下 stdout 默认 GBK，--json 输出会变乱码/解码失败 → 统一 UTF-8（平台约定）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="眼睛侧观察采集（vision_capture v1；v3.14 --watch）")
    ap.add_argument("--camera", type=int, default=None, help="摄像头索引（默认 None）")
    ap.add_argument("--image", type=str, default=None, help="静态图片路径")
    ap.add_argument("--demo", action="store_true", help="演示模式（无需相机/cv2，产出合成观察）")
    ap.add_argument("--prev", type=str, default=None, help="上一帧图片路径（计算运动差异；watch 首轮用）")
    ap.add_argument("--out-dir", type=str, default=None, help=f"输出目录（默认 {DEFAULT_OUT_DIR}）")
    ap.add_argument("--json", action="store_true", help="stdout 逐行输出观察 JSON（供脚本调用）")
    ap.add_argument("--watch", action="store_true", help="连续观察模式（v3.14）")
    ap.add_argument("--interval", type=int, default=5, help=f"watch 间隔秒（钳制 {_INTERVAL_MIN}..{_INTERVAL_MAX}，默认 5）")
    ap.add_argument("--rounds", type=int, default=0, help="watch 轮数（0=无限，Ctrl+C 停止；默认 0）")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir or DEFAULT_OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    cv2 = _load_cv2()

    if not args.watch:
        rc, obs, _ = _run_once(args, cv2, _load_prev(args, cv2), out_dir)
        if rc == 0 and obs is not None:
            _print_obs(obs, args.json)
        return rc

    # --watch 连续观察
    interval = _clamp_int(args.interval, _INTERVAL_MIN, _INTERVAL_MAX, 5)
    rounds = _clamp_int(args.rounds, 0, 10 ** 6, 0)
    prev = _load_prev(args, cv2)
    run = 0
    try:
        while True:
            run += 1
            rc, obs, frame = _run_once(args, cv2, prev, out_dir)
            if rc != 0:
                print(f"[watch] 第 {run} 轮失败（rc={rc}），退出", file=sys.stderr)
                return rc
            _print_obs(obs, args.json)
            prev = frame  # 滚动：下一轮用本帧算运动差异
            if rounds and run >= rounds:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n[watch] 已停止（Ctrl+C），共采集 {run} 轮", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
