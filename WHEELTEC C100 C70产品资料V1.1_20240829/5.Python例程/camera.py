from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import logging
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, List, Optional, Tuple

import cv2

TRIGGER_TOLERANCE_SECONDS = 1.0
SUPPORTED_FORMATS = {"jpg", "jpeg", "png"}
CAPTURE_WIDTH = 1920
CAPTURE_HEIGHT = 1080
CAPTURE_FPS = 10
CAPTURE_CODEC = "MJPG"
TIMEPOINT_PATTERN = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
CAMERA_ID_PATTERN = re.compile(
    r"^(?:0x)?([0-9a-fA-F]{4}):(?:0x)?([0-9a-fA-F]{4})$"
)
CAMERA_PATH_ID_PATTERN = re.compile(
    r"vid[_-]([0-9a-fA-F]{4}).*?pid[_-]([0-9a-fA-F]{4})",
    re.IGNORECASE,
)
CAMERA_ENUMERATION_ATTEMPTS = 2
CAMERA_ENUMERATION_RETRY_SECONDS = 0.25
_CAMERA_ENUMERATION_LOCK = threading.Lock()
_CAMERA_OPEN_LOCK = threading.Lock()


@dataclass(frozen=True)
class CameraTarget:
    vid: int
    pid: int
    name: Optional[str] = None
    path: Optional[str] = None

    @property
    def camera_id(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"


@dataclass(frozen=True)
class CameraDevice:
    """One physical camera with one or more Windows capture endpoints."""

    index: int
    backend: int
    name: str
    path: str
    vid: Optional[int]
    pid: Optional[int]
    alternatives: Tuple[Any, ...] = ()


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("camera_timer")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    if logger.handlers:
        return logger

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def parse_timepoint(raw: str) -> dt_time:
    if not TIMEPOINT_PATTERN.match(raw):
        raise ValueError(f"时间点格式错误: {raw}。请使用 HH:MM（如 08:00），例如：08:00,12:30")
    hour, minute = raw.split(":")
    return dt_time(hour=int(hour), minute=int(minute))


def parse_timepoints(raw: Optional[str]) -> List[dt_time]:
    if not raw:
        return []
    points = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        points.append(parse_timepoint(item))
    return points


def load_timepoints_from_file(path: Optional[str]) -> List[dt_time]:
    if not path:
        return []
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"时间点配置文件不存在: {path}")
    content = file_path.read_text(encoding="utf-8")
    payload = json.loads(content)
    timepoint_source = payload
    if isinstance(payload, dict):
        timepoint_source = payload.get("timepoints", [])
    if not isinstance(timepoint_source, list):
        raise ValueError("时间点配置文件格式错误：需要一个字符串数组（或对象中的 timepoints 字段）")
    points = []
    for raw in timepoint_source:
        if not isinstance(raw, str):
            raise ValueError("timepoints 中每一项必须是 HH:MM 字符串")
        points.append(parse_timepoint(raw.strip()))
    return points


def next_interval_after(anchor: datetime, interval_seconds: int) -> datetime:
    return anchor + timedelta(seconds=interval_seconds)


def advance_interval_due(base_time: datetime, now: datetime, interval_seconds: int) -> datetime:
    next_time = base_time
    while next_time <= now + timedelta(seconds=TRIGGER_TOLERANCE_SECONDS):
        next_time = next_interval_after(next_time, interval_seconds)
    return next_time


def next_timepoint_after(now: datetime, points: List[dt_time]) -> Optional[datetime]:
    if not points:
        return None
    today = now.date()
    for pt in points:
        candidate = datetime.combine(today, pt)
        if candidate >= now:
            return candidate
    tomorrow = today + timedelta(days=1)
    return datetime.combine(tomorrow, points[0])


def parse_camera_id(raw: str) -> Tuple[int, int]:
    match = CAMERA_ID_PATTERN.fullmatch(raw.strip())
    if not match:
        raise ValueError(
            f"摄像头 ID 格式错误: {raw}。请使用四位十六进制 VID:PID，例如 0c45:6366"
        )
    return int(match.group(1), 16), int(match.group(2), 16)


def _camera_vid_pid(camera_info: Any) -> Tuple[Optional[int], Optional[int]]:
    vid = getattr(camera_info, "vid", None)
    pid = getattr(camera_info, "pid", None)
    try:
        if vid is not None and pid is not None:
            parsed_vid = int(vid, 16) if isinstance(vid, str) else int(vid)
            parsed_pid = int(pid, 16) if isinstance(pid, str) else int(pid)
            return parsed_vid, parsed_pid
    except (TypeError, ValueError):
        pass

    # Some Windows/driver/backend combinations expose the USB identity only in
    # the symbolic device path. In particular this can happen behind a dock.
    match = CAMERA_PATH_ID_PATTERN.search(str(getattr(camera_info, "path", "")))
    if match:
        return int(match.group(1), 16), int(match.group(2), 16)
    return None, None


def camera_device_key(camera_info: Any) -> str:
    """Return a stable-enough key for de-duplicating Windows backends."""
    path = str(getattr(camera_info, "path", "")).strip().casefold()
    if path:
        return f"path:{path}"
    vid, pid = _camera_vid_pid(camera_info)
    name = str(getattr(camera_info, "name", "")).strip().casefold()
    return (
        f"device:{vid if vid is not None else 'none'}:"
        f"{pid if pid is not None else 'none'}:{name}:"
        f"{int(getattr(camera_info, 'backend', -1))}:"
        f"{int(getattr(camera_info, 'index', -1))}"
    )


def _camera_signature(camera_info: Any) -> Tuple[Optional[int], Optional[int], str]:
    vid, pid = _camera_vid_pid(camera_info)
    name = str(getattr(camera_info, "name", "")).strip().casefold()
    return vid, pid, name


def _merge_camera_backends(primary: List[Any], secondary: List[Any]) -> List[Any]:
    """Prefer DirectShow while retaining Media Foundation as an open fallback."""
    if not primary:
        return secondary
    if not secondary:
        return primary

    secondary_by_path = {
        str(getattr(camera, "path", "")).strip().casefold(): camera
        for camera in secondary
        if str(getattr(camera, "path", "")).strip()
    }
    primary_signatures: dict[Tuple[Optional[int], Optional[int], str], List[Any]] = {}
    secondary_signatures: dict[Tuple[Optional[int], Optional[int], str], List[Any]] = {}
    for camera in primary:
        primary_signatures.setdefault(_camera_signature(camera), []).append(camera)
    for camera in secondary:
        secondary_signatures.setdefault(_camera_signature(camera), []).append(camera)

    merged: List[Any] = []
    consumed_secondary: set[int] = set()
    for camera in primary:
        alternatives: List[Any] = list(getattr(camera, "alternatives", ()))
        path = str(getattr(camera, "path", "")).strip().casefold()
        fallback = secondary_by_path.get(path) if path else None
        signature = _camera_signature(camera)
        if fallback is None and (
            len(primary_signatures.get(signature, ())) == 1
            and len(secondary_signatures.get(signature, ())) == 1
        ):
            fallback = secondary_signatures[signature][0]
        if fallback is not None:
            alternatives.append(fallback)
            consumed_secondary.add(id(fallback))
        vid, pid = _camera_vid_pid(camera)
        merged.append(
            CameraDevice(
                index=int(getattr(camera, "index")),
                backend=int(getattr(camera, "backend")),
                name=str(getattr(camera, "name", "未知摄像头")),
                path=str(getattr(camera, "path", "")),
                vid=vid,
                pid=pid,
                alternatives=tuple(alternatives),
            )
        )

    # Keep devices found only by MSMF. This is important while a USB dock is
    # still settling and DirectShow has not published the device yet.
    primary_keys = {camera_device_key(camera) for camera in primary}
    for camera in secondary:
        if id(camera) in consumed_secondary:
            continue
        if camera_device_key(camera) not in primary_keys:
            merged.append(camera)
    return merged


def enumerate_windows_cameras() -> List[Any]:
    if sys.platform != "win32":
        raise RuntimeError("摄像头硬件识别功能仅支持 Windows")
    try:
        from cv2_enumerate_cameras import enumerate_cameras
    except ImportError as exc:
        raise RuntimeError(
            "缺少摄像头识别依赖。请运行: python -m pip install cv2-enumerate-cameras"
        ) from exc
    last_errors: List[str] = []
    with _CAMERA_ENUMERATION_LOCK:
        for attempt in range(CAMERA_ENUMERATION_ATTEMPTS):
            directshow: List[Any] = []
            media_foundation: List[Any] = []
            errors: List[str] = []
            try:
                directshow = list(enumerate_cameras(cv2.CAP_DSHOW))
            except Exception as exc:
                errors.append(f"DirectShow: {exc}")
            try:
                media_foundation = list(enumerate_cameras(cv2.CAP_MSMF))
            except Exception as exc:
                errors.append(f"Media Foundation: {exc}")

            cameras = _merge_camera_backends(directshow, media_foundation)
            if cameras:
                return cameras
            last_errors = errors
            if attempt + 1 < CAMERA_ENUMERATION_ATTEMPTS:
                time.sleep(CAMERA_ENUMERATION_RETRY_SECONDS)

    if last_errors:
        raise RuntimeError("摄像头枚举失败：" + " | ".join(last_errors))
    return []


def camera_info_id(camera_info: Any) -> Optional[str]:
    vid, pid = _camera_vid_pid(camera_info)
    if vid is None or pid is None:
        return None
    return f"{int(vid):04x}:{int(pid):04x}"


def print_camera_list() -> None:
    cameras = enumerate_windows_cameras()
    if not cameras:
        print("未发现摄像头。请检查 USB 连接和 Windows 相机权限。")
        return
    print("Windows 检测到以下摄像头：")
    for camera in cameras:
        print(
            f"- camera-id={camera_info_id(camera) or '未知'} | "
            f"name={getattr(camera, 'name', '未知')} | "
            f"path={getattr(camera, 'path', '未知')}"
        )


def resolve_target_camera(target: CameraTarget) -> Any:
    cameras = enumerate_windows_cameras()
    matches = [
        camera
        for camera in cameras
        if _camera_vid_pid(camera) == (target.vid, target.pid)
    ]

    if target.name:
        expected_name = target.name.casefold()
        matches = [
            camera
            for camera in matches
            if str(getattr(camera, "name", "")).casefold() == expected_name
        ]
    if target.path:
        expected_path = target.path.casefold()
        matches = [
            camera
            for camera in matches
            if str(getattr(camera, "path", "")).casefold() == expected_path
        ]

    if not matches:
        detected = ", ".join(
            f"{camera_info_id(camera) or '未知'} ({getattr(camera, 'name', '未知')})"
            for camera in cameras
        ) or "无"
        raise RuntimeError(
            f"未找到指定的 WHEELTEC 摄像头 {target.camera_id}。"
            f"当前检测到: {detected}。程序不会改用电脑内置摄像头。"
        )
    if len(matches) > 1:
        paths = "; ".join(str(getattr(camera, "path", "未知")) for camera in matches)
        raise RuntimeError(
            f"检测到多个相同 VID/PID 的摄像头 {target.camera_id}。"
            f"请在配置文件的 camera 对象中增加 path，候选路径: {paths}"
        )
    return matches[0]


def open_camera_info(camera_info: Any) -> Tuple[cv2.VideoCapture, Any]:
    """Open a resolved camera, falling back across Windows capture backends."""
    endpoints = (camera_info,) + tuple(getattr(camera_info, "alternatives", ()))
    errors: List[str] = []
    # Some USB docks become unreliable when two UVC devices are initialized
    # at exactly the same time. Serialize only the short open/configure phase.
    with _CAMERA_ENUMERATION_LOCK, _CAMERA_OPEN_LOCK:
        for endpoint in endpoints:
            index = int(getattr(endpoint, "index"))
            backend = int(getattr(endpoint, "backend"))
            capture = cv2.VideoCapture(index, backend)
            if capture.isOpened():
                configure_capture_1080p(capture)
                return capture, endpoint
            capture.release()
            errors.append(f"backend={backend}, index={index}")
    raise RuntimeError(
        "摄像头已被 Windows 枚举，但无法打开视频流（"
        + "; ".join(errors)
        + "）；请确认未被其他程序占用"
    )


def configure_capture_1080p(cap: cv2.VideoCapture) -> None:
    """Request the camera's native 1080p MJPEG stream on Windows."""
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*CAPTURE_CODEC))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, CAPTURE_FPS)


def is_1080p_resolution(width: int, height: int) -> bool:
    return width == CAPTURE_WIDTH and height == CAPTURE_HEIGHT


def open_camera(target: CameraTarget, logger: logging.Logger) -> cv2.VideoCapture:
    camera_info = resolve_target_camera(target)
    cap, opened_info = open_camera_info(camera_info)
    camera_index = int(getattr(opened_info, "index"))
    camera_backend = int(getattr(opened_info, "backend"))
    logger.info(
        "目标摄像头已打开: camera-id=%s | name=%s | backend=%s | index=%s | path=%s | 请求=%sx%s %s %sfps",
        target.camera_id,
        getattr(opened_info, "name", getattr(camera_info, "name", "未知")),
        camera_backend,
        camera_index,
        getattr(opened_info, "path", getattr(camera_info, "path", "未知")),
        CAPTURE_WIDTH,
        CAPTURE_HEIGHT,
        CAPTURE_CODEC,
        CAPTURE_FPS,
    )
    return cap


def ensure_output_dir(base_dir: Path) -> Path:
    day_dir = base_dir / datetime.now().strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir


def capture_one(
    cap: cv2.VideoCapture,
    output_dir: Path,
    image_format: str,
    trigger_source: str,
    logger: logging.Logger,
) -> bool:
    day_dir = ensure_output_dir(output_dir)
    now = datetime.now()
    filename = (
        now.strftime("plant_%Y%m%d_%H%M%S_")
        + f"{int(now.microsecond / 1000):03d}"
        + f"_{trigger_source}.{image_format}"
    )
    target = day_dir / filename

    ret, frame = cap.read()
    if not ret or frame is None:
        logger.error("抓帧失败（%s）", now.strftime("%Y-%m-%d %H:%M:%S"))
        return False

    height, width = frame.shape[:2]
    if not is_1080p_resolution(width, height):
        logger.error(
            "拒绝保存低分辨率画面：实际 %sx%s，要求 %sx%s。"
            "请改用主板 USB 3.x 接口并避免两个摄像头共用低带宽 USB Hub。",
            width,
            height,
            CAPTURE_WIDTH,
            CAPTURE_HEIGHT,
        )
        return False

    if not cv2.imwrite(str(target), frame):
        logger.error("保存图片失败：%s", target)
        return False

    logger.info("保存成功：%s", target)
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="WHEELTEC C100/C70 定时拍照工具（Windows 优先）"
    )
    parser.add_argument(
        "--list-cameras",
        action="store_true",
        help="列出 Windows 摄像头的名称、VID:PID 和设备路径后退出",
    )
    parser.add_argument(
        "--camera-id",
        type=str,
        default=None,
        help="指定 WHEELTEC 摄像头的 USB VID:PID，例如 0c45:6366",
    )
    parser.add_argument(
        "--camera-name",
        type=str,
        default=None,
        help="可选的摄像头名称校验，必须与 Windows 中的名称完全一致",
    )
    parser.add_argument(
        "--camera-path",
        type=str,
        default=None,
        help="可选的设备路径校验；连接多个同型号摄像头时用于精确区分",
    )
    parser.add_argument(
        "-i",
        "--id",
        "--camera-index",
        type=int,
        default=None,
        help="已弃用；请使用 --camera-id，避免误开电脑内置摄像头",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=None,
        help="间隔触发秒数，默认 60；设置为 0 表示关闭间隔触发",
    )
    parser.add_argument("--timepoints", type=str, default=None, help="固定时间点，逗号分隔，例如 08:00,12:30,18:00")
    parser.add_argument("--timepoint-file", type=str, default=None, help="可选 JSON 配置文件，包含 timepoints 字段")
    parser.add_argument("--output-dir", type=str, default="output", help="图片输出目录，默认 output")
    parser.add_argument("--format", type=str, default="jpg", help="图片格式，支持 jpg/png/jpeg")
    parser.add_argument("--log-file", type=str, default="camera_timer.log", help="日志文件路径")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.list_cameras:
        print_camera_list()
        return

    output_dir = Path(args.output_dir)
    log_file = Path(args.log_file)
    logger = setup_logger(log_file)

    file_timepoints: List[dt_time] = []
    file_interval: Optional[int] = None
    file_camera_id: Optional[str] = None
    file_camera_name: Optional[str] = None
    file_camera_path: Optional[str] = None
    if args.timepoint_file:
        config_text = Path(args.timepoint_file).read_text(encoding="utf-8")
        config = json.loads(config_text)
        if not isinstance(config, dict):
            raise ValueError("时间点配置文件必须是对象，例如：{\"timepoints\":[...],\"interval_seconds\":60}")
        file_timepoints = load_timepoints_from_file(args.timepoint_file)
        interval_cfg = config.get("interval_seconds")
        if interval_cfg is not None:
            if not isinstance(interval_cfg, int) or interval_cfg < 0:
                raise ValueError("配置文件 interval_seconds 必须是 0 或正整数")
            file_interval = interval_cfg
        camera_config = config.get("camera", {})
        if not isinstance(camera_config, dict):
            raise ValueError("配置文件 camera 必须是对象")
        file_camera_id = camera_config.get("id", config.get("camera_id"))
        file_camera_name = camera_config.get("name", config.get("camera_name"))
        file_camera_path = camera_config.get("path", config.get("camera_path"))
        for key, value in (
            ("camera.id", file_camera_id),
            ("camera.name", file_camera_name),
            ("camera.path", file_camera_path),
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"配置文件 {key} 必须是字符串")
        if "camera_index" in config:
            raise ValueError(
                "配置文件 camera_index 已停用。请运行 --list-cameras，并改用 camera.id"
            )

    cli_timepoints = parse_timepoints(args.timepoints)
    file_timepoints = [tp for tp in file_timepoints if tp]
    merged_timepoints = sorted(set(cli_timepoints + file_timepoints))

    interval_seconds = (
        args.interval_seconds
        if args.interval_seconds is not None
        else (file_interval if file_interval is not None else 60)
    )
    if args.id is not None:
        raise ValueError("--camera-index 已停用。请运行 --list-cameras，并改用 --camera-id")
    camera_id = args.camera_id or file_camera_id
    if not camera_id:
        program_name = Path(sys.argv[0]).name
        raise ValueError(
            "必须配置 WHEELTEC 摄像头的 camera-id。"
            f"请先运行 python {program_name} --list-cameras 查看 VID:PID"
        )
    camera_vid, camera_pid = parse_camera_id(camera_id)
    camera_target = CameraTarget(
        vid=camera_vid,
        pid=camera_pid,
        name=args.camera_name or file_camera_name,
        path=args.camera_path or file_camera_path,
    )
    image_format = args.format.lower()
    if image_format not in SUPPORTED_FORMATS:
        raise ValueError(f"不支持的图片格式: {args.format}，支持：{', '.join(sorted(SUPPORTED_FORMATS))}")

    if interval_seconds < 0:
        raise ValueError("interval-seconds 必须为 0 或正整数，0 表示关闭间隔触发")

    if args.timepoints is not None and not cli_timepoints:
        raise ValueError("timepoints 参数不能为空或格式不正确")

    merged_timepoints = sorted(set(merged_timepoints))

    if merged_timepoints:
        logger.info("时间点列表: %s", ", ".join(tp.strftime("%H:%M") for tp in merged_timepoints))
    logger.info("间隔秒数: %s", interval_seconds if interval_seconds != 0 else "(未启用)")

    if args.timepoint_file:
        logger.info("已加载配置文件: %s", args.timepoint_file)
    logger.info("锁定目标摄像头: camera-id=%s", camera_target.camera_id)

    try:
        cap = open_camera(camera_target, logger)
    except Exception as exc:
        logger.exception("摄像头打开失败")
        raise SystemExit(str(exc)) from exc

    now = datetime.now()
    interval_enabled = interval_seconds > 0
    next_interval = next_interval_after(now, interval_seconds) if interval_enabled else None
    next_timepoint = next_timepoint_after(now, merged_timepoints)

    total_captures = 0
    failed_captures = 0
    last_capture_time: Optional[datetime] = None
    last_capture_sources = {"interval": None, "timepoint": None}
    last_message = datetime.now()

    try:
        while True:
            now = datetime.now()
            due_interval = (
                interval_enabled
                and next_interval is not None
                and now >= next_interval - timedelta(seconds=TRIGGER_TOLERANCE_SECONDS)
            )
            due_timepoint = next_timepoint is not None and now >= next_timepoint - timedelta(seconds=TRIGGER_TOLERANCE_SECONDS)

            if due_interval or due_timepoint:
                now = datetime.now()
                if last_capture_time is not None:
                    if (now - last_capture_time).total_seconds() < TRIGGER_TOLERANCE_SECONDS:
                        # 避免同一时刻连续触发重复写盘
                        time.sleep(0.1)
                        continue

                if due_interval and last_capture_sources["interval"] is not None:
                    if (now - last_capture_sources["interval"]).total_seconds() < TRIGGER_TOLERANCE_SECONDS:
                        due_interval = False
                if due_timepoint and last_capture_sources["timepoint"] is not None:
                    if (now - last_capture_sources["timepoint"]).total_seconds() < TRIGGER_TOLERANCE_SECONDS:
                        due_timepoint = False

                trigger_sources = []
                if due_interval:
                    trigger_sources.append("interval")
                if due_timepoint:
                    trigger_sources.append("timepoint")
                if not trigger_sources:
                    time.sleep(0.1)
                    continue
                source_label = "both" if len(trigger_sources) > 1 else trigger_sources[0]

                captured = False
                for _ in range(1):
                    if not cap.isOpened():
                        logger.warning("摄像头不可用，尝试重连中...")
                        try:
                            cap.release()
                        except Exception:
                            pass
                        try:
                            cap = open_camera(camera_target, logger)
                        except Exception as exc:
                            logger.error("重连失败: %s", exc)
                            break
                    captured = capture_one(cap, output_dir, image_format, source_label, logger)
                    if captured:
                        break
                    cap.release()
                    logger.warning("本次拍照失败，等待下次触发")

                if captured:
                    last_capture_time = now
                    total_captures += 1
                    for source in trigger_sources:
                        last_capture_sources[source] = now
                    logger.info("本次触发: %s | 累计: %s | 失败: %s", source_label, total_captures, failed_captures)
                else:
                    failed_captures += 1
                    logger.warning("本次触发失败 | 累计失败: %s", failed_captures)

                if due_interval and interval_enabled and next_interval is not None:
                    next_interval = advance_interval_due(next_interval, now, interval_seconds)
                if due_timepoint and next_timepoint is not None:
                    next_timepoint = next_timepoint_after(now + timedelta(seconds=1), merged_timepoints)
            else:
                now = datetime.now()
                next_events = [t for t in (next_interval, next_timepoint) if t is not None]
                if not next_events:
                    logger.warning("未配置任何触发方式，程序退出")
                    return
                next_event = min(next_events)
                wait_seconds = (next_event - now).total_seconds() - TRIGGER_TOLERANCE_SECONDS
                if wait_seconds <= 0:
                    time.sleep(0.1)
                else:
                    time.sleep(min(1.0, wait_seconds))

            if (datetime.now() - last_message).total_seconds() >= 30:
                last_message = datetime.now()
                if due_interval or due_timepoint:
                    logger.info(
                        "下一次间隔: %s | 下一次时间点: %s | 累计: %s | 失败: %s",
                        next_interval.strftime("%Y-%m-%d %H:%M:%S") if next_interval else "未启用",
                        next_timepoint.strftime("%Y-%m-%d %H:%M:%S") if next_timepoint else "未启用",
                        total_captures,
                        failed_captures,
                    )
    except KeyboardInterrupt:
        logger.info("接收到退出信号，正在关闭...")
    finally:
        try:
            cap.release()
        except Exception:
            pass
        logger.info("结束运行。总计: %s 张, 失败: %s 张", total_captures, failed_captures)


if __name__ == "__main__":
    main()
