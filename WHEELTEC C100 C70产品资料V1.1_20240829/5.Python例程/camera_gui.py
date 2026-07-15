from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

import cv2
from PIL import Image, ImageTk

try:
    import camera_timer as camera_core
except ImportError:
    import camera as camera_core


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "camera_gui_config.json"
LOG_PATH = SCRIPT_DIR / "camera_gui.log"
TRIGGER_TOLERANCE_SECONDS = 1.0
CAMERA_CHANNEL_COUNT = 2
AUTO_REFRESH_INTERVAL_MS = 2000
DISCOVERY_POLL_INTERVAL_MS = 100
INCOMPLETE_SCAN_CONFIRMATIONS = 2
CONNECT_POLL_INTERVAL_MS = 250
CONNECT_SETTLE_INTERVAL_MS = 750
CONNECT_TIMEOUT_SECONDS = 8.0
FRAME_READ_FAILURE_LIMIT = 5
FRAME_READ_RETRY_SECONDS = 0.12


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("wheeltec_camera_gui")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
    return logger


def fit_preview_size(
    source_width: int,
    source_height: int,
    box_width: int,
    box_height: int,
) -> Tuple[Tuple[int, int], float]:
    scale = min(box_width / source_width, box_height / source_height)
    display_size = (
        max(1, round(source_width * scale)),
        max(1, round(source_height * scale)),
    )
    return display_size, scale


def safe_filename_part(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\s]+', "_", str(value).strip())
    cleaned = cleaned.strip("._")
    return cleaned[:40] or fallback


def target_key(target: Optional[camera_core.CameraTarget]) -> Optional[str]:
    if target is None:
        return None
    if target.path:
        return f"path:{target.path.casefold()}"
    return f"id:{target.camera_id}"


class CameraWorker:
    def __init__(self, event_queue: queue.Queue):
        self.event_queue = event_queue
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.generation = 0
        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.frame_sequence = 0
        self.capture_lock = threading.Lock()
        self.capture = None

    def start(self, target: camera_core.CameraTarget) -> None:
        self.stop()
        self.generation += 1
        generation = self.generation
        self.stop_event = threading.Event()
        with self.frame_lock:
            self.latest_frame = None
            self.frame_sequence = 0
        self.thread = threading.Thread(
            target=self._run,
            args=(target, self.stop_event, generation),
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.generation += 1
        self.stop_event.set()
        with self.capture_lock:
            if self.capture is not None:
                try:
                    self.capture.release()
                except Exception:
                    pass
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.thread = None
        with self.capture_lock:
            self.capture = None

    def get_latest_frame(self) -> Tuple[int, Any]:
        with self.frame_lock:
            if self.latest_frame is None:
                return self.frame_sequence, None
            return self.frame_sequence, self.latest_frame.copy()

    def _emit(self, generation: int, event: str, payload: Any = None) -> None:
        self.event_queue.put((generation, event, payload))

    def _run(
        self,
        target: camera_core.CameraTarget,
        stop_event: threading.Event,
        generation: int,
    ) -> None:
        while not stop_event.is_set():
            try:
                camera_info = camera_core.resolve_target_camera(target)
                capture, opened_info = camera_core.open_camera_info(camera_info)
            except Exception as exc:
                self._emit(generation, "retrying", str(exc))
                if stop_event.wait(2.0):
                    break
                continue

            with self.capture_lock:
                self.capture = capture
            connection_announced = False
            consecutive_read_failures = 0
            while not stop_event.is_set():
                ok, frame = capture.read()
                if not ok or frame is None:
                    consecutive_read_failures += 1
                    if consecutive_read_failures < FRAME_READ_FAILURE_LIMIT:
                        if stop_event.wait(FRAME_READ_RETRY_SECONDS):
                            break
                        continue
                    self._emit(
                        generation,
                        "retrying",
                        f"视频连续读取失败 {consecutive_read_failures} 次，"
                        "正在重新枚举并连接设备",
                    )
                    break
                consecutive_read_failures = 0
                if not connection_announced:
                    height, width = frame.shape[:2]
                    self._emit(
                        generation,
                        "connected",
                        {
                            "name": getattr(camera_info, "name", "未知摄像头"),
                            "camera_id": camera_core.camera_info_id(camera_info) or "未知",
                            "path": getattr(camera_info, "path", ""),
                            "backend": int(getattr(opened_info, "backend", -1)),
                            "width": width,
                            "height": height,
                            "resolution_ok": camera_core.is_1080p_resolution(width, height),
                        },
                    )
                    connection_announced = True
                with self.frame_lock:
                    self.latest_frame = frame
                    self.frame_sequence += 1

            try:
                capture.release()
            except Exception:
                pass
            with self.capture_lock:
                if self.capture is capture:
                    self.capture = None

            if not stop_event.is_set():
                stop_event.wait(1.0)

        self._emit(generation, "stopped")


class CameraChannel(ttk.Frame):
    def __init__(
        self,
        master: ttk.Notebook,
        app: "MultiCameraTimerApp",
        channel_index: int,
        config: Dict[str, Any],
    ) -> None:
        super().__init__(master)
        self.app = app
        self.channel_index = channel_index
        self.logger = app.logger
        self.event_queue: queue.Queue = queue.Queue()
        self.worker = CameraWorker(self.event_queue)
        self.camera_choices: Dict[str, camera_core.CameraTarget] = {}
        self.bound_target: Optional[camera_core.CameraTarget] = None
        self.bound_name = ""
        self.active_target: Optional[camera_core.CameraTarget] = None
        self.connected_device_name = ""
        self.camera_connected = False
        self.camera_resolution_ok = False
        self.capture_resolution = (0, 0)
        self.last_preview_sequence = -1
        self.last_preview_size = (0, 0)
        self.preview_photo = None
        self.schedule_running = False
        self.next_interval: Optional[datetime] = None
        self.next_timepoint: Optional[datetime] = None
        self.schedule_timepoints = []
        self.schedule_interval_seconds = 0
        self.last_capture_time: Optional[datetime] = None
        self.total_captures = 0
        self.failed_captures = 0
        self.last_refresh_error = ""
        self.closing = False

        self._load_bound_target(config)
        self._create_variables(config)
        self._build_ui()
        self.channel_label_var.trace_add("write", lambda *_args: self.app.update_tab_label(self))
        self.camera_var.trace_add("write", lambda *_args: self.app.update_tab_label(self))

        self.after(30, self._update_preview)
        self.after(100, self._process_worker_events)
        self.after(100, self._schedule_tick)

    def _load_bound_target(self, config: Dict[str, Any]) -> None:
        camera_config = config.get("camera")
        if not isinstance(camera_config, dict) or not camera_config.get("id"):
            return
        try:
            vid, pid = camera_core.parse_camera_id(str(camera_config["id"]))
        except ValueError:
            return
        path = camera_config.get("path")
        self.bound_target = camera_core.CameraTarget(
            vid=vid,
            pid=pid,
            path=str(path) if path else None,
        )
        self.bound_name = str(camera_config.get("name", ""))

    def _create_variables(self, config: Dict[str, Any]) -> None:
        self.channel_label_var = tk.StringVar(
            value=str(config.get("label", f"相机{self.channel_index}"))
        )
        self.camera_var = tk.StringVar()
        self.camera_status_var = tk.StringVar(value="未连接")
        self.preview_status_var = tk.StringVar(value="等待连接摄像头")
        self.interval_enabled_var = tk.BooleanVar(
            value=bool(config.get("interval_enabled", True))
        )
        self.interval_seconds_var = tk.StringVar(
            value=str(config.get("interval_seconds", 60))
        )
        saved_timepoints = config.get("timepoints", "")
        if isinstance(saved_timepoints, list):
            saved_timepoints = ",".join(str(item) for item in saved_timepoints)
        self.timepoint_enabled_var = tk.BooleanVar(
            value=bool(config.get("timepoint_enabled", bool(saved_timepoints)))
        )
        self.timepoints_var = tk.StringVar(value=str(saved_timepoints))
        self.output_dir_var = tk.StringVar(value=str(config.get("output_dir", "output")))
        self.image_format_var = tk.StringVar(value="png")
        self.schedule_status_var = tk.StringVar(value="拍照计划未启动")
        self.next_trigger_var = tk.StringVar(value="下一次触发：--")
        self.capture_count_var = tk.StringVar(value="成功：0    失败：0")
        self.last_saved_var = tk.StringVar(value="最近保存：--")

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        device = ttk.Frame(self, padding=(16, 10, 16, 8))
        device.grid(row=0, column=0, sticky="ew")
        ttk.Label(device, text=f"相机通道 {self.channel_index}", style="Section.TLabel").pack(side="left")
        self.camera_combo = ttk.Combobox(
            device,
            textvariable=self.camera_var,
            width=46,
            state="readonly",
        )
        self.camera_combo.pack(side="left", padx=(12, 8))
        self.refresh_button = ttk.Button(
            device,
            text="刷新设备",
            command=self.app.refresh_all_cameras,
        )
        self.refresh_button.pack(side="left")
        self.connect_button = ttk.Button(
            device,
            text="连接并预览",
            command=self.connect_camera,
            style="Accent.TButton",
        )
        self.connect_button.pack(side="left", padx=(8, 0))
        self.disconnect_button = ttk.Button(
            device,
            text="断开",
            command=self.disconnect_camera,
            state="disabled",
        )
        self.disconnect_button.pack(side="left", padx=(8, 0))
        self.rebind_button = ttk.Button(
            device,
            text="更换绑定",
            command=self.clear_camera_binding,
        )
        self.rebind_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            device,
            textvariable=self.camera_status_var,
            style="Subtle.TLabel",
        ).pack(side="right")

        ttk.Separator(self, orient="horizontal").grid(row=1, column=0, sticky="ew", padx=16)

        content = ttk.Frame(self, padding=(16, 10, 16, 8))
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=0)
        content.rowconfigure(1, weight=1)

        preview_header = ttk.Frame(content)
        preview_header.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        ttk.Label(preview_header, text="实时画面", style="Section.TLabel").pack(side="left")
        ttk.Label(
            preview_header,
            textvariable=self.preview_status_var,
            style="Subtle.TLabel",
        ).pack(side="right")

        self.preview_label = tk.Label(
            content,
            text="连接摄像头后显示实时画面",
            bg="#111827",
            fg="#cbd5e1",
            font=("Microsoft YaHei UI", 13),
            anchor="center",
        )
        self.preview_label.grid(row=1, column=0, sticky="nsew", padx=(0, 16))

        settings = ttk.Frame(content, width=390)
        settings.grid(row=0, column=1, rowspan=2, sticky="ns")
        settings.grid_propagate(False)
        settings.columnconfigure(0, minsize=105)
        settings.columnconfigure(1, weight=1, minsize=125)
        settings.columnconfigure(2, minsize=42)

        ttk.Label(settings, text="拍照计划", style="Section.TLabel").grid(
            row=0,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(0, 10),
        )
        ttk.Checkbutton(
            settings,
            text="固定间隔",
            variable=self.interval_enabled_var,
        ).grid(row=1, column=0, sticky="w")
        self.interval_entry = ttk.Entry(
            settings,
            textvariable=self.interval_seconds_var,
            width=12,
        )
        self.interval_entry.grid(row=1, column=1, sticky="ew", padx=(10, 8))
        ttk.Label(settings, text="秒").grid(row=1, column=2, sticky="w")

        ttk.Checkbutton(
            settings,
            text="固定时间点",
            variable=self.timepoint_enabled_var,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(11, 0))
        ttk.Entry(settings, textvariable=self.timepoints_var).grid(
            row=3,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(6, 0),
        )
        ttk.Label(
            settings,
            text="格式：08:00,12:30,18:00",
            style="Subtle.TLabel",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(3, 10))

        ttk.Label(settings, text="保存设置", style="Section.TLabel").grid(
            row=5,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(5, 9),
        )
        ttk.Label(settings, text="照片标识").grid(row=6, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.channel_label_var).grid(
            row=6,
            column=1,
            columnspan=2,
            sticky="ew",
            padx=(10, 0),
        )
        ttk.Label(settings, text="目录").grid(row=7, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(settings, textvariable=self.output_dir_var).grid(
            row=7,
            column=1,
            sticky="ew",
            padx=(10, 8),
            pady=(8, 0),
        )
        ttk.Button(settings, text="选择", command=self.choose_output_dir).grid(
            row=7,
            column=2,
            sticky="ew",
            pady=(8, 0),
        )
        ttk.Label(settings, text="格式").grid(row=8, column=0, sticky="w", pady=(8, 0))
        ttk.Label(
            settings,
            text="PNG（无损）",
            style="Subtle.TLabel",
        ).grid(row=8, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(8, 0))

        action_row = ttk.Frame(settings)
        action_row.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(16, 8))
        self.start_schedule_button = ttk.Button(
            action_row,
            text="启动拍照计划",
            command=self.start_schedule,
            style="Accent.TButton",
        )
        self.start_schedule_button.pack(side="left")
        self.stop_schedule_button = ttk.Button(
            action_row,
            text="停止计划",
            command=self.stop_schedule,
            state="disabled",
        )
        self.stop_schedule_button.pack(side="left", padx=(8, 0))

        manual_row = ttk.Frame(settings)
        manual_row.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        ttk.Button(
            manual_row,
            text="立即拍照",
            command=lambda: self.capture_image("manual"),
        ).pack(side="left")
        ttk.Button(
            manual_row,
            text="打开保存目录",
            command=self.open_output_dir,
        ).pack(side="left", padx=(8, 0))

        ttk.Separator(settings, orient="horizontal").grid(
            row=11,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(0, 10),
        )
        ttk.Label(settings, textvariable=self.schedule_status_var).grid(
            row=12,
            column=0,
            columnspan=3,
            sticky="w",
        )
        ttk.Label(
            settings,
            textvariable=self.next_trigger_var,
            style="Subtle.TLabel",
        ).grid(row=13, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(
            settings,
            textvariable=self.capture_count_var,
            style="Subtle.TLabel",
        ).grid(row=14, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(
            settings,
            textvariable=self.last_saved_var,
            style="Subtle.TLabel",
            wraplength=380,
            justify="left",
        ).grid(row=15, column=0, columnspan=3, sticky="w", pady=(4, 0))

        log_frame = ttk.Frame(self, padding=(16, 0, 16, 12))
        log_frame.grid(row=3, column=0, sticky="ew")
        self.log = tk.Text(
            log_frame,
            height=5,
            bg="#111827",
            fg="#d8dee9",
            insertbackground="#d8dee9",
            font=("Consolas", 9),
            state="disabled",
        )
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scrollbar.set)

    def config_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "label": self.channel_label_var.get().strip() or f"相机{self.channel_index}",
            "interval_enabled": self.interval_enabled_var.get(),
            "interval_seconds": self.interval_seconds_var.get().strip(),
            "timepoint_enabled": self.timepoint_enabled_var.get(),
            "timepoints": self.timepoints_var.get().strip(),
            "output_dir": self.output_dir_var.get().strip(),
            "image_format": self.image_format_var.get().strip(),
        }
        if self.bound_target is not None:
            payload["camera"] = {
                "id": self.bound_target.camera_id,
                "name": self.bound_name,
            }
            if self.bound_target.path:
                payload["camera"]["path"] = self.bound_target.path
        return payload

    def append_log(self, message: str, level: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] {message}\n")
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 220:
            self.log.delete("1.0", "41.0")
        self.log.see("end")
        self.log.configure(state="disabled")
        getattr(self.logger, level, self.logger.info)(
            "相机通道 %s | %s",
            self.channel_index,
            message,
        )

    def tab_label(self) -> str:
        label = self.channel_label_var.get().strip() or f"相机{self.channel_index}"
        device = self.connected_device_name or self.bound_name
        if not device:
            selected = self.camera_var.get()
            device = selected.split("  [", 1)[0] if "  [" in selected else "未选择"
        return f"{label} · {device}"

    def refresh_cameras(
        self,
        cameras: List[Any],
        error: Optional[str] = None,
    ) -> None:
        if self.worker.thread is not None:
            return
        if error:
            self.camera_choices = {}
            self.camera_combo["values"] = ("无法读取摄像头设备",)
            self.camera_var.set("无法读取摄像头设备")
            self.connect_button.configure(state="disabled")
            self.camera_status_var.set(error)
            if error != self.last_refresh_error:
                self.append_log(f"刷新设备失败：{error}", "error")
            self.last_refresh_error = error
            return

        self.last_refresh_error = ""

        counts: Dict[str, int] = {}
        for camera in cameras:
            camera_id = camera_core.camera_info_id(camera)
            if camera_id:
                counts[camera_id] = counts.get(camera_id, 0) + 1

        if (
            self.bound_target is not None
            and not self.bound_target.path
            and counts.get(self.bound_target.camera_id, 0) > 1
        ):
            self.camera_choices = {}
            self.camera_combo["values"] = ("原绑定无法区分两个同型号摄像头",)
            self.camera_var.set("原绑定无法区分两个同型号摄像头")
            self.connect_button.configure(state="disabled")
            self.camera_status_var.set("请点击“更换绑定”并重新选择")
            self.append_log("检测到两个同型号摄像头，旧绑定缺少设备路径", "warning")
            return

        seen: Dict[str, int] = {}
        choices: Dict[str, camera_core.CameraTarget] = {}
        for camera in cameras:
            camera_id = camera_core.camera_info_id(camera)
            if not camera_id:
                continue
            seen[camera_id] = seen.get(camera_id, 0) + 1
            vid, pid = camera_core.parse_camera_id(camera_id)
            raw_path = str(getattr(camera, "path", ""))
            target = camera_core.CameraTarget(
                vid=vid,
                pid=pid,
                path=raw_path or None,
            )

            if self.bound_target is not None:
                if target.camera_id != self.bound_target.camera_id:
                    continue
                if (
                    self.bound_target.path
                    and raw_path.casefold() != self.bound_target.path.casefold()
                ):
                    continue
                target = self.bound_target
            elif self.app.is_camera_in_use(target, self):
                continue

            name = str(getattr(camera, "name", "未知摄像头"))
            label = f"{name}  [{camera_id}]"
            if counts.get(camera_id, 0) > 1:
                label += f"  · 同型号设备 {seen[camera_id]}"
            if raw_path:
                label += f"  · {raw_path[-28:]}"
            choices[label] = target

        previous_selection = self.camera_var.get()
        self.camera_choices = choices
        values = tuple(choices.keys())
        self.camera_combo["values"] = values
        if values:
            if previous_selection in values:
                selected = previous_selection
            elif self.bound_target is not None:
                selected = values[0]
            else:
                selected = values[min(self.channel_index - 1, len(values) - 1)]
            self.camera_var.set(selected)
            self.connect_button.configure(state="normal")
            if self.bound_target is not None:
                self.camera_status_var.set(
                    f"已绑定 {self.bound_target.camera_id}，等待连接"
                )
            else:
                self.camera_status_var.set(f"发现 {len(values)} 个可选摄像头")
        else:
            text = "绑定的摄像头未连接" if self.bound_target else "没有可用摄像头"
            self.camera_combo["values"] = (text,)
            self.camera_var.set(text)
            self.connect_button.configure(state="disabled")
            self.camera_status_var.set(text)
        self.camera_combo.configure(state="readonly")
        self.app.update_tab_label(self)

    def connect_camera(self, show_dialog: bool = True) -> bool:
        target = self.camera_choices.get(self.camera_var.get())
        if target is None:
            self._connection_error("请刷新并选择一个 USB 摄像头。", show_dialog)
            return False
        if self.app.is_camera_in_use(target, self):
            self._connection_error("该摄像头已被另一个相机通道绑定或连接。", show_dialog)
            return False

        if self.bound_target is None:
            self.bound_target = target
            self.bound_name = self.camera_var.get().split("  [", 1)[0]
            self.app.save_config()
            self.append_log(
                f"已绑定设备：{target.camera_id} | path={target.path or '无'}"
            )

        self.active_target = self.bound_target
        self.camera_connected = False
        self.camera_resolution_ok = False
        self.capture_resolution = (0, 0)
        self.camera_status_var.set("正在连接...")
        self.preview_status_var.set("正在打开视频流")
        self.connect_button.configure(state="disabled")
        self.disconnect_button.configure(state="normal")
        self.refresh_button.configure(state="disabled")
        self.rebind_button.configure(state="disabled")
        self.camera_combo.configure(state="disabled")
        self.worker.start(self.active_target)
        self.app.refresh_all_cameras()
        return True

    def _connection_error(self, text: str, show_dialog: bool) -> None:
        self.camera_status_var.set(text)
        self.append_log(text, "warning")
        if show_dialog:
            messagebox.showwarning("无法连接", text)

    def disconnect_camera(self) -> None:
        self.stop_schedule()
        self.worker.stop()
        self.active_target = None
        self.camera_connected = False
        self.camera_resolution_ok = False
        self.capture_resolution = (0, 0)
        self.connected_device_name = ""
        self.preview_photo = None
        self.preview_label.configure(image="", text="连接摄像头后显示实时画面")
        self.camera_status_var.set("已断开")
        self.preview_status_var.set("等待连接摄像头")
        self.disconnect_button.configure(state="disabled")
        self.refresh_button.configure(state="normal")
        self.rebind_button.configure(state="normal")
        self.camera_combo.configure(state="readonly")
        self.append_log("摄像头已断开")
        self.app.refresh_all_cameras()
        self.app.update_tab_label(self)

    def clear_camera_binding(self) -> None:
        if self.worker.thread is not None:
            self.disconnect_camera()
        if self.bound_target is None:
            self.app.refresh_all_cameras()
            return
        confirmed = messagebox.askyesno(
            "更换绑定",
            f"相机通道 {self.channel_index} 当前绑定为 {self.bound_target.camera_id}。"
            "确定要重新选择吗？",
        )
        if not confirmed:
            return
        self.bound_target = None
        self.bound_name = ""
        self.connected_device_name = ""
        self.app.save_config()
        self.app.refresh_all_cameras()
        self.append_log("已解除摄像头绑定，请重新选择设备")

    def _process_worker_events(self) -> None:
        if self.closing:
            return
        while True:
            try:
                generation, event, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break
            if generation != self.worker.generation:
                continue
            if event == "connected":
                first_connection = not self.camera_connected
                self.camera_connected = True
                self.camera_resolution_ok = bool(payload["resolution_ok"])
                self.capture_resolution = (int(payload["width"]), int(payload["height"]))
                self.connected_device_name = str(payload["name"])
                resolution_text = f"{payload['width']} × {payload['height']}"
                if self.camera_resolution_ok:
                    self.camera_status_var.set(
                        f"已连接：{payload['name']} [{payload['camera_id']}] · 1080p"
                    )
                    self.preview_status_var.set(f"视频流正常 · {resolution_text}")
                else:
                    self.camera_status_var.set(
                        f"分辨率不足：{resolution_text}（要求 1920 × 1080）"
                    )
                    self.preview_status_var.set("已连接，但未获得 1080p 视频流")
                if first_connection:
                    if self.camera_resolution_ok:
                        self.append_log(
                            f"1080p 视频流已连接：{payload['name']} "
                            f"[{payload['camera_id']}] · {resolution_text}"
                        )
                    else:
                        self.append_log(
                            f"未获得 1080p：实际 {resolution_text}，要求 1920 × 1080。"
                            "请使用主板 USB 3.x 接口，并避免两个摄像头共用低带宽 USB Hub。",
                            "error",
                        )
                self.app.update_tab_label(self)
            elif event == "retrying":
                self.camera_connected = False
                self.camera_resolution_ok = False
                self.capture_resolution = (0, 0)
                self.camera_status_var.set("连接中断，自动重连中")
                self.preview_status_var.set("等待目标摄像头恢复")
                self.append_log(str(payload), "warning")
            elif event == "stopped" and not self.closing:
                self.camera_connected = False
                self.camera_resolution_ok = False
                self.capture_resolution = (0, 0)
        self.after(100, self._process_worker_events)

    def _update_preview(self) -> None:
        if self.closing:
            return
        if self.winfo_ismapped():
            sequence, frame = self.worker.get_latest_frame()
            box_width = max(320, self.preview_label.winfo_width())
            box_height = max(240, self.preview_label.winfo_height())
            preview_size = (box_width, box_height)
            if frame is not None and (
                sequence != self.last_preview_sequence
                or preview_size != self.last_preview_size
            ):
                self.last_preview_sequence = sequence
                self.last_preview_size = preview_size
                height, width = frame.shape[:2]
                if camera_core.is_1080p_resolution(width, height):
                    self.preview_status_var.set(f"视频流正常 · {width} × {height}")
                else:
                    self.preview_status_var.set(
                        f"分辨率不足 · {width} × {height}（要求 1920 × 1080）"
                    )
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = Image.fromarray(rgb)
                display_size, scale = fit_preview_size(
                    width,
                    height,
                    box_width,
                    box_height,
                )
                resample = (
                    Image.Resampling.LANCZOS
                    if scale < 1
                    else Image.Resampling.BILINEAR
                )
                image = image.resize(display_size, resample)
                background = Image.new(
                    "RGB",
                    (box_width, box_height),
                    (17, 24, 39),
                )
                offset = (
                    (box_width - image.width) // 2,
                    (box_height - image.height) // 2,
                )
                background.paste(image, offset)
                self.preview_photo = ImageTk.PhotoImage(background)
                self.preview_label.configure(image=self.preview_photo, text="")
        self.after(30, self._update_preview)

    def choose_output_dir(self) -> None:
        initial = self._output_path()
        path = filedialog.askdirectory(
            title=f"选择相机通道 {self.channel_index} 的图片保存目录",
            initialdir=str(initial),
        )
        if path:
            self.output_dir_var.set(path)
            self.app.save_config()

    def _output_path(self) -> Path:
        raw = self.output_dir_var.get().strip() or "output"
        path = Path(raw).expanduser()
        return path if path.is_absolute() else SCRIPT_DIR / path

    def open_output_dir(self) -> None:
        path = self._output_path()
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))
        except AttributeError:
            messagebox.showinfo("保存目录", str(path))
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc))

    def start_schedule(self, show_dialog: bool = True) -> bool:
        if not self.camera_connected:
            return self._schedule_error(
                "请先连接摄像头并确认实时画面正常。",
                show_dialog,
            )
        if not self.camera_resolution_ok:
            width, height = self.capture_resolution
            return self._schedule_error(
                f"当前视频流为 {width} × {height}，未达到 1920 × 1080。"
                "请检查 USB 接口或带宽后重新连接。",
                show_dialog,
            )

        interval_seconds = 0
        if self.interval_enabled_var.get():
            try:
                interval_seconds = int(self.interval_seconds_var.get().strip())
            except ValueError:
                return self._schedule_error(
                    "拍照间隔必须是大于 0 的整数秒。",
                    show_dialog,
                )
            if interval_seconds <= 0:
                return self._schedule_error(
                    "拍照间隔必须是大于 0 的整数秒。",
                    show_dialog,
                )

        timepoints = []
        if self.timepoint_enabled_var.get():
            try:
                timepoints = sorted(
                    set(camera_core.parse_timepoints(self.timepoints_var.get()))
                )
            except ValueError as exc:
                return self._schedule_error(str(exc), show_dialog)
            if not timepoints:
                return self._schedule_error(
                    "请输入至少一个 HH:MM 时间点。",
                    show_dialog,
                )

        if interval_seconds == 0 and not timepoints:
            return self._schedule_error(
                "请至少启用固定间隔或固定时间点。",
                show_dialog,
            )

        try:
            self._output_path().mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return self._schedule_error(f"保存目录不可用：{exc}", show_dialog)

        now = datetime.now()
        self.schedule_running = True
        self.schedule_interval_seconds = interval_seconds
        self.schedule_timepoints = timepoints
        self.next_interval = (
            now + timedelta(seconds=interval_seconds)
            if interval_seconds
            else None
        )
        self.next_timepoint = camera_core.next_timepoint_after(now, timepoints)
        self.last_capture_time = None
        self.start_schedule_button.configure(state="disabled")
        self.stop_schedule_button.configure(state="normal")
        self.schedule_status_var.set("拍照计划运行中")
        self.app.save_config()
        sources = []
        if interval_seconds:
            sources.append(f"每 {interval_seconds} 秒")
        if timepoints:
            sources.append(
                "每日 " + ", ".join(point.strftime("%H:%M") for point in timepoints)
            )
        self.append_log("拍照计划已启动：" + " + ".join(sources))
        self._update_next_trigger_label()
        return True

    def _schedule_error(self, text: str, show_dialog: bool) -> bool:
        self.schedule_status_var.set(text)
        self.append_log(text, "warning")
        if show_dialog:
            messagebox.showerror("无法启动拍照计划", text)
        return False

    def stop_schedule(self) -> None:
        was_running = self.schedule_running
        self.schedule_running = False
        self.schedule_interval_seconds = 0
        self.next_interval = None
        self.next_timepoint = None
        self.start_schedule_button.configure(state="normal")
        self.stop_schedule_button.configure(state="disabled")
        self.schedule_status_var.set("拍照计划未启动")
        self.next_trigger_var.set("下一次触发：--")
        if was_running:
            self.append_log("拍照计划已停止")

    def _schedule_tick(self) -> None:
        if self.closing:
            return
        if self.schedule_running:
            now = datetime.now()
            tolerance = timedelta(seconds=TRIGGER_TOLERANCE_SECONDS)
            due_interval = (
                self.next_interval is not None
                and now >= self.next_interval - tolerance
            )
            due_timepoint = (
                self.next_timepoint is not None
                and now >= self.next_timepoint - tolerance
            )

            if due_interval or due_timepoint:
                sources = []
                if due_interval:
                    sources.append("interval")
                if due_timepoint:
                    sources.append("timepoint")
                source = "both" if len(sources) == 2 else sources[0]
                if (
                    self.last_capture_time is None
                    or (now - self.last_capture_time).total_seconds() >= 1
                ):
                    if self.capture_image(source):
                        self.last_capture_time = now
                else:
                    self.append_log("触发时间相差不足 1 秒，已去重")

                if due_interval and self.next_interval is not None:
                    self.next_interval = camera_core.advance_interval_due(
                        self.next_interval,
                        now,
                        self.schedule_interval_seconds,
                    )
                if due_timepoint and self.next_timepoint is not None:
                    self.next_timepoint = camera_core.next_timepoint_after(
                        now + timedelta(seconds=1),
                        self.schedule_timepoints,
                    )
                self._update_next_trigger_label()
        self.after(100, self._schedule_tick)

    def _update_next_trigger_label(self) -> None:
        events = [
            event
            for event in (self.next_interval, self.next_timepoint)
            if event is not None
        ]
        if not events:
            self.next_trigger_var.set("下一次触发：--")
            return
        next_event = min(events)
        self.next_trigger_var.set(
            f"下一次触发：{next_event.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def capture_image(self, trigger_source: str) -> bool:
        if not self.camera_connected:
            self.failed_captures += 1
            self._update_capture_counts()
            self.append_log(f"拍照失败 ({trigger_source})：摄像头未连接", "error")
            return False

        _sequence, frame = self.worker.get_latest_frame()
        if frame is None:
            self.failed_captures += 1
            self._update_capture_counts()
            self.append_log(f"拍照失败 ({trigger_source})：尚未收到视频帧", "error")
            return False

        height, width = frame.shape[:2]
        if not camera_core.is_1080p_resolution(width, height):
            self.failed_captures += 1
            self._update_capture_counts()
            self.append_log(
                f"拒绝保存低分辨率画面 ({trigger_source})：实际 {width} × {height}，"
                "要求 1920 × 1080",
                "error",
            )
            return False

        now = datetime.now()
        image_format = "png"
        self.image_format_var.set(image_format)

        label = safe_filename_part(
            self.channel_label_var.get(),
            f"camera{self.channel_index}",
        )
        channel_token = f"camera{self.channel_index}_{label}"
        day_dir = (
            self._output_path()
            / now.strftime("%Y-%m-%d")
            / channel_token
        )
        filename = (
            f"plant_{channel_token}_"
            + now.strftime("%Y%m%d_%H%M%S_")
            + f"{now.microsecond // 1000:03d}_{trigger_source}.{image_format}"
        )
        target = day_dir / filename

        try:
            day_dir.mkdir(parents=True, exist_ok=True)
            ok, encoded = cv2.imencode(
                ".png",
                frame,
                [cv2.IMWRITE_PNG_COMPRESSION, 0],
            )
            if not ok:
                raise RuntimeError("OpenCV 图片编码失败")
            encoded.tofile(str(target))
        except Exception as exc:
            self.failed_captures += 1
            self._update_capture_counts()
            self.append_log(f"拍照保存失败 ({trigger_source})：{exc}", "error")
            return False

        self.total_captures += 1
        self._update_capture_counts()
        self.last_saved_var.set(f"最近保存：{target}")
        self.append_log(f"保存成功 ({trigger_source})：{target}")
        return True

    def _update_capture_counts(self) -> None:
        self.capture_count_var.set(
            f"成功：{self.total_captures}    失败：{self.failed_captures}"
        )

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.schedule_running = False
        self.worker.stop()


class MultiCameraTimerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("WHEELTEC 双路植物定时拍照 - Windows")
        self.geometry("1220x860")
        self.minsize(980, 720)
        self.configure(bg="#f4f7f6")

        self.logger = setup_logger()
        self.closing = False
        self.camera_infos: List[Any] = []
        self.discovery_queue: queue.Queue = queue.Queue()
        self.discovery_thread: Optional[threading.Thread] = None
        self.incomplete_scan_count = 0
        self.device_summary_var = tk.StringVar(value="正在检测摄像头...")
        self.connect_all_queue: List[CameraChannel] = []
        self.connect_all_current: Optional[CameraChannel] = None
        self.connect_all_waiting_for_scan = False
        self.connect_all_deadline = 0.0
        config = self._load_config()

        self._configure_style()
        self._build_header()
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        channel_configs = self._channel_configs(config)
        self.channels: List[CameraChannel] = []
        for index in range(1, CAMERA_CHANNEL_COUNT + 1):
            channel = CameraChannel(
                self.notebook,
                self,
                index,
                channel_configs[index - 1],
            )
            self.channels.append(channel)
            self.notebook.add(channel, text=channel.tab_label())

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.refresh_all_cameras()
        self.after(DISCOVERY_POLL_INTERVAL_MS, self._process_discovery_results)
        self.after(AUTO_REFRESH_INTERVAL_MS, self._auto_refresh_cameras)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        available = style.theme_names()
        if "vista" in available:
            style.theme_use("vista")
        elif "clam" in available:
            style.theme_use("clam")
        style.configure("TFrame", background="#f4f7f6")
        style.configure(
            "TLabel",
            background="#f4f7f6",
            foreground="#18211f",
            font=("Microsoft YaHei UI", 10),
        )
        style.configure(
            "Title.TLabel",
            font=("Microsoft YaHei UI", 22, "bold"),
            foreground="#173f35",
        )
        style.configure("Subtle.TLabel", foreground="#5f6f6b")
        style.configure(
            "Section.TLabel",
            font=("Microsoft YaHei UI", 12, "bold"),
            foreground="#173f35",
        )
        style.configure(
            "TButton",
            padding=(10, 6),
            font=("Microsoft YaHei UI", 10),
        )
        style.configure(
            "Accent.TButton",
            padding=(12, 7),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "TCheckbutton",
            background="#f4f7f6",
            font=("Microsoft YaHei UI", 10),
        )

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(18, 12, 18, 6))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            header,
            text="WHEELTEC 双路植物定时拍照",
            style="Title.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="两个相机通道独立预览、独立计划、后台同时拍照",
            style="Subtle.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        toolbar = ttk.Frame(self, padding=(18, 4, 18, 8))
        toolbar.grid(row=1, column=0, sticky="ew")
        ttk.Button(
            toolbar,
            text="刷新全部设备",
            command=self.refresh_all_cameras,
        ).pack(side="left")
        ttk.Button(
            toolbar,
            text="连接全部",
            command=self.connect_all,
            style="Accent.TButton",
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            toolbar,
            text="启动全部计划",
            command=self.start_all_schedules,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            toolbar,
            text="停止全部计划",
            command=self.stop_all_schedules,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            toolbar,
            text="断开全部",
            command=self.disconnect_all,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(
            toolbar,
            textvariable=self.device_summary_var,
            style="Subtle.TLabel",
        ).pack(side="right")

    def _load_config(self) -> Dict[str, Any]:
        if not CONFIG_PATH.exists():
            return {}
        try:
            payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            self.logger.warning("配置文件读取失败: %s", exc)
            return {}

    def _channel_configs(self, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        channels = config.get("channels")
        if isinstance(channels, list):
            result = [item if isinstance(item, dict) else {} for item in channels]
        else:
            first = dict(config)
            first.setdefault("label", "相机1")
            second = {
                key: value
                for key, value in config.items()
                if key not in ("camera", "channels")
            }
            second["label"] = "相机2"
            result = [first, second]
        while len(result) < CAMERA_CHANNEL_COUNT:
            result.append({"label": f"相机{len(result) + 1}"})
        return result[:CAMERA_CHANNEL_COUNT]

    def save_config(self) -> None:
        if not hasattr(self, "channels"):
            return
        payload = {
            "version": 2,
            "channels": [channel.config_payload() for channel in self.channels],
        }
        CONFIG_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def refresh_all_cameras(self) -> None:
        if self.closing:
            return
        if self.discovery_thread is not None:
            return
        self.device_summary_var.set("正在扫描 Windows 摄像头...")
        self.discovery_thread = threading.Thread(
            target=self._discover_cameras,
            daemon=True,
        )
        self.discovery_thread.start()

    def _discover_cameras(self) -> None:
        error: Optional[str] = None
        try:
            cameras = list(camera_core.enumerate_windows_cameras())
            counts: Dict[str, int] = {}
            for camera in cameras:
                camera_id = camera_core.camera_info_id(camera) or ""
                counts[camera_id] = counts.get(camera_id, 0) + 1
            cameras.sort(
                key=lambda camera: (
                    0
                    if counts.get(camera_core.camera_info_id(camera) or "", 0) > 1
                    else 1,
                    str(getattr(camera, "name", "")).casefold(),
                    str(getattr(camera, "path", "")).casefold(),
                )
            )
        except Exception as exc:
            cameras = []
            error = str(exc)
        self.discovery_queue.put((cameras, error))

    def _process_discovery_results(self) -> None:
        if self.closing:
            return
        latest: Optional[Tuple[List[Any], Optional[str]]] = None
        while True:
            try:
                latest = self.discovery_queue.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            cameras, error = latest
            self.discovery_thread = None
            self._apply_discovery_result(cameras, error)
        self.after(DISCOVERY_POLL_INTERVAL_MS, self._process_discovery_results)

    def _apply_discovery_result(
        self,
        cameras: List[Any],
        error: Optional[str],
    ) -> None:
        if error:
            # A transient COM/backend failure must not make a camera disappear
            # from the GUI. Connection still re-enumerates before opening it.
            self.device_summary_var.set("设备扫描暂时失败，2 秒后重试")
            if self.camera_infos:
                for channel in getattr(self, "channels", []):
                    channel.refresh_cameras(self.camera_infos)
            else:
                for channel in getattr(self, "channels", []):
                    channel.refresh_cameras([], error)
            return

        if len(cameras) < len(self.camera_infos):
            self.incomplete_scan_count += 1
            if self.incomplete_scan_count < INCOMPLETE_SCAN_CONFIRMATIONS:
                self.device_summary_var.set(
                    f"本次仅扫到 {len(cameras)} 个设备，正在复核..."
                )
                return
        else:
            self.incomplete_scan_count = 0

        self.camera_infos = cameras
        self.incomplete_scan_count = 0
        self.device_summary_var.set(
            f"Windows 检测到 {len(self.camera_infos)} 个摄像头"
        )
        for channel in getattr(self, "channels", []):
            channel.refresh_cameras(self.camera_infos)

    def _auto_refresh_cameras(self) -> None:
        if self.closing:
            return
        if any(channel.worker.thread is None for channel in self.channels):
            self.refresh_all_cameras()
        self.after(AUTO_REFRESH_INTERVAL_MS, self._auto_refresh_cameras)

    def is_camera_in_use(
        self,
        target: camera_core.CameraTarget,
        requester: CameraChannel,
    ) -> bool:
        key = target_key(target)
        return any(
            channel is not requester
            and target_key(channel.bound_target or channel.active_target) == key
            for channel in getattr(self, "channels", [])
        )

    def update_tab_label(self, channel: CameraChannel) -> None:
        if not hasattr(self, "notebook"):
            return
        try:
            self.notebook.index(channel)
        except tk.TclError:
            return
        self.notebook.tab(channel, text=channel.tab_label())

    def _on_tab_changed(self, _event: Any) -> None:
        selected = self.notebook.select()
        if not selected:
            return
        channel = self.nametowidget(selected)
        if isinstance(channel, CameraChannel):
            channel.last_preview_size = (0, 0)

    def connect_all(self) -> None:
        if (
            self.connect_all_current is not None
            or self.connect_all_queue
            or self.connect_all_waiting_for_scan
        ):
            return
        self.connect_all_waiting_for_scan = True
        self.refresh_all_cameras()
        self.after(DISCOVERY_POLL_INTERVAL_MS, self._connect_all_after_scan)

    def _connect_all_after_scan(self) -> None:
        if self.closing:
            self.connect_all_waiting_for_scan = False
            return
        if self.discovery_thread is not None:
            self.after(DISCOVERY_POLL_INTERVAL_MS, self._connect_all_after_scan)
            return
        self.connect_all_waiting_for_scan = False
        self.connect_all_queue = [
            channel for channel in self.channels if channel.worker.thread is None
        ]
        self._connect_next_channel()

    def _connect_next_channel(self) -> None:
        if self.closing or not self.connect_all_queue:
            self.connect_all_current = None
            return
        channel = self.connect_all_queue.pop(0)
        if not channel.connect_camera(show_dialog=False):
            self.after(CONNECT_SETTLE_INTERVAL_MS, self._connect_next_channel)
            return
        self.connect_all_current = channel
        self.connect_all_deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
        self.after(
            CONNECT_POLL_INTERVAL_MS,
            lambda: self._wait_for_channel_connection(channel),
        )

    def _wait_for_channel_connection(self, channel: CameraChannel) -> None:
        if self.closing or self.connect_all_current is not channel:
            return
        timed_out = time.monotonic() >= self.connect_all_deadline
        if channel.camera_connected or channel.worker.thread is None or timed_out:
            self.connect_all_current = None
            self.refresh_all_cameras()
            self.after(CONNECT_SETTLE_INTERVAL_MS, self._connect_next_channel)
            return
        self.after(
            CONNECT_POLL_INTERVAL_MS,
            lambda: self._wait_for_channel_connection(channel),
        )

    def start_all_schedules(self) -> None:
        for channel in self.channels:
            if not channel.schedule_running:
                channel.start_schedule(show_dialog=False)

    def stop_all_schedules(self) -> None:
        for channel in self.channels:
            channel.stop_schedule()

    def disconnect_all(self) -> None:
        self.connect_all_queue = []
        self.connect_all_current = None
        self.connect_all_waiting_for_scan = False
        for channel in self.channels:
            if channel.worker.thread is not None:
                channel.disconnect_camera()

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.connect_all_queue = []
        self.connect_all_current = None
        try:
            self.save_config()
        except Exception:
            pass
        for channel in self.channels:
            channel.close()
        self.destroy()


def enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


if __name__ == "__main__":
    enable_windows_dpi_awareness()
    app = MultiCameraTimerApp()
    app.mainloop()
