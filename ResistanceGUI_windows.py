import csv
import math
import os
import queue
import random
import re
import shutil
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import filedialog, messagebox, ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


class ResistanceParser:
    UNIT_PATTERN = r"(MΩ|MOhm|Mohm|兆欧|mΩ|mohm|毫欧|[kK]Ω|[kK][oO][hH][mM]|千欧|Ω|[oO][hH][mM]|欧姆|欧)?"
    NUMBER_PATTERN = r"([-+]?(?:\d[\d,]*\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
    LABEL_PATTERN = r"(?:R_s|R_S|r_s|r_S|R[sS]|r[sS]|R|r|resistance|Resistance|RESISTANCE|res|Res|RES|电阻)"
    MARKED_PATTERN = re.compile(
        rf"(?:^|[\s,;]){LABEL_PATTERN}\s*[:=]\s*{NUMBER_PATTERN}\s*{UNIT_PATTERN}"
    )
    FIRST_PATTERN = re.compile(rf"{NUMBER_PATTERN}\s*{UNIT_PATTERN}")

    @classmethod
    def parse(cls, raw):
        line = raw.strip()
        if not line:
            return None

        match = cls.MARKED_PATTERN.search(line) or cls.FIRST_PATTERN.search(line)
        if not match:
            return None

        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            return None

        unit = match.group(2) or ""
        if unit in ("MΩ", "MOhm", "Mohm", "兆欧"):
            return value * 1_000_000
        if unit in ("mΩ", "mohm", "毫欧"):
            return value / 1_000
        if unit.lower() in ("kω", "kohm") or unit == "千欧":
            return value * 1_000
        return value


def trim_number(value):
    absolute = abs(value)
    if absolute == 0:
        return "0"
    if absolute >= 100:
        return f"{value:.1f}".removesuffix(".0")
    if absolute >= 10:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.3f}".rstrip("0").rstrip(".")


def format_time_axis(seconds):
    absolute = abs(seconds)
    if absolute >= 3600:
        return f"{trim_number(seconds / 3600)}h"
    if absolute >= 60:
        return f"{trim_number(seconds / 60)}m"
    return f"{trim_number(seconds)}s"


def axis_label(value, step=None):
    if not step or step <= 0:
        return trim_number(value)
    step = abs(step)
    if step >= 1:
        decimals = 1
    elif step >= 0.1:
        decimals = 2
    elif step >= 0.01:
        decimals = 3
    elif step >= 0.001:
        decimals = 4
    else:
        decimals = 5
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".")


def custom_y_range(values, step):
    data_min = min(values) if values else 0
    data_max = max(values) if values else data_min
    minimum_span = step * 5
    data_span = max(data_max - data_min, step)
    interval_count = max(5, math.ceil(data_span / step))
    span = max(minimum_span, interval_count * step)
    center = (data_min + data_max) / 2
    min_y = math.floor((center - span / 2) / step) * step
    max_y = min_y + span

    while data_min < min_y:
        min_y -= step
        max_y -= step
    while data_max > max_y:
        min_y += step
        max_y += step
    return min_y, max_y


def y_tick_values(min_y, max_y, step=None):
    if not step or step <= 0:
        return [max_y - (index / 5) * (max_y - min_y) for index in range(6)]
    interval_count = max(1, round((max_y - min_y) / step))
    draw_every = max(1, math.ceil(interval_count / 12))
    displayed_step = step * draw_every
    value = math.ceil(min_y / displayed_step) * displayed_step
    values = []
    while value <= max_y + displayed_step * 0.001:
        values.append(value)
        value += displayed_step
    return values or [min_y, max_y]


def resolve_unit(values, preferred="auto"):
    if preferred != "auto":
        return preferred
    max_abs = max([abs(value) for value in values], default=0)
    if max_abs >= 1_000_000:
        return "mohm"
    if max_abs >= 1_000:
        return "kohm"
    return "ohm"


def unit_scale(unit):
    if unit == "mohm":
        return 1_000_000, "MΩ"
    if unit == "kohm":
        return 1_000, "kΩ"
    return 1, "Ω"


def format_resistance(ohms, preferred="auto", resolved=None):
    if ohms is None or not math.isfinite(ohms):
        return "--"
    unit = resolved or resolve_unit([ohms], preferred)
    factor, label = unit_scale(unit)
    return f"{trim_number(ohms / factor)} {label}"


def value_or_dash(value):
    cleaned = str(value).strip()
    return cleaned if cleaned else "--"


def show_control_characters(text):
    return text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")


def safe_filename_part(value, fallback):
    cleaned = re.sub(r'[<>:"/\\|?*\s]+', "_", str(value).strip())
    cleaned = cleaned.strip("._")
    return (cleaned[:40] or fallback)


class SerialWorker:
    def __init__(self, event_queue):
        self.event_queue = event_queue
        self.thread = None
        self.stop_event = threading.Event()
        self.serial_port = None

    def start(self, port, baudrate):
        self.stop()
        if serial is None:
            raise RuntimeError("未安装 pyserial。请运行：python -m pip install pyserial")

        self.stop_event.clear()
        self.serial_port = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.01,
        )
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.serial_port is not None:
            try:
                self.serial_port.close()
            except serial.SerialException:
                pass
            self.serial_port = None

    def _read_loop(self):
        pending = ""
        last_byte_time = time.monotonic()
        flush_after = 0.08

        while not self.stop_event.is_set():
            try:
                chunk = self.serial_port.read(512)
            except Exception as exc:
                self.event_queue.put(("error", str(exc)))
                break

            if chunk:
                last_byte_time = time.monotonic()
                self.event_queue.put(("bytes", len(chunk)))
                text = chunk.decode("utf-8", errors="replace")
                for char in text:
                    if char in ("\r", "\n"):
                        line = pending
                        pending = ""
                        if line.strip():
                            self.event_queue.put(("line", line))
                    else:
                        pending += char
            elif pending.strip() and time.monotonic() - last_byte_time >= flush_after:
                self.event_queue.put(("line", pending))
                pending = ""


class SimplePDFReport:
    PAGE_WIDTH = 595
    PAGE_HEIGHT = 842
    MARGIN = 42

    def __init__(self, path, data_points, visible_points, stats):
        self.path = path
        self.data_points = data_points
        self.visible_points = visible_points
        self.stats = stats
        self.pages = []

    def write(self):
        content = []
        self._draw_page(content)
        self.pages.append("\n".join(content).encode("utf-8"))

        objects = []
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        page_refs = " ".join(f"{index} 0 R" for index in range(5, 5 + len(self.pages) * 2, 2))
        objects.append(f"<< /Type /Pages /Kids [{page_refs}] /Count {len(self.pages)} >>".encode("utf-8"))
        objects.append(
            b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /UniGB-UCS2-H /DescendantFonts [4 0 R] >>"
        )
        objects.append(
            b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> "
            b"/FontDescriptor << /Type /FontDescriptor /FontName /STSong-Light /Flags 4 "
            b"/FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 880 /Descent -120 "
            b"/CapHeight 700 /StemV 80 >> >>"
        )

        object_id = 5
        for page_content in self.pages:
            content_id = object_id + 1
            objects.append(
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.PAGE_WIDTH} {self.PAGE_HEIGHT}] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode("utf-8")
            )
            objects.append(b"<< /Length " + str(len(page_content)).encode("ascii") + b" >>\nstream\n" + page_content + b"\nendstream")
            object_id += 2

        self._write_objects(objects)

    def _write_objects(self, objects):
        with open(self.path, "wb") as handle:
            handle.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
            offsets = [0]
            for index, obj in enumerate(objects, start=1):
                offsets.append(handle.tell())
                handle.write(f"{index} 0 obj\n".encode("ascii"))
                handle.write(obj)
                handle.write(b"\nendobj\n")
            xref = handle.tell()
            handle.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
            handle.write(b"0000000000 65535 f \n")
            for offset in offsets[1:]:
                handle.write(f"{offset:010d} 00000 n \n".encode("ascii"))
            handle.write(
                f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
            )

    def _draw_page(self, content):
        y = 806
        self._text(content, "植物阻抗测量报告", 42, y, 22)
        y -= 24
        self._line(content, 42, y, 553, y, 0.08, 0.48, 0.45, 1.2)
        y -= 22

        self._text(content, "实验变量", 42, y, 15, 0.08, 0.48, 0.45)
        y -= 24
        for line in self._metadata_lines():
            self._text(content, line, 42, y, 11)
            y -= 18

        y -= 12
        self._text(content, "统计摘要", 42, y, 15, 0.08, 0.48, 0.45)
        y -= 24
        for line in self._summary_lines():
            self._text(content, line, 42, y, 11)
            y -= 18

        y -= 14
        self._text(content, "曲线图", 42, y, 15, 0.08, 0.48, 0.45)
        self._chart(content, 42, 145, 511, 360)

    def _metadata_lines(self):
        metadata = self.stats.get("metadata", {})
        return [
            f"通道：{metadata.get('channel', '--')}    串口：{metadata.get('serial_port', '--')}    波特率：{metadata.get('baudrate', '--')}",
            f"实验时间：{metadata.get('experiment_time', '--')}    测试时长：{metadata.get('duration', '--')}",
            f"植物编号：{metadata.get('plant_id', '--')}    电极位置：{metadata.get('electrode_position', '--')}    土壤状态：{metadata.get('soil_state', '--')}",
            f"温度：{metadata.get('temperature', '--')} °C    湿度：{metadata.get('humidity', '--')} %    Y格差：{metadata.get('y_grid_step', '--')}",
        ]

    def _summary_lines(self):
        values = [point[1] for point in self.visible_points]
        resolved = "kohm"
        average = sum(values) / len(values) if values else None
        return [
            f"统计样本数：{len(values)}",
            f"平均值：{format_resistance(average, resolved=resolved) if average is not None else '--'}",
            f"最小值：{format_resistance(min(values), resolved=resolved) if values else '--'}    最大值：{format_resistance(max(values), resolved=resolved) if values else '--'}",
        ]

    def _chart(self, content, x, y, width, height):
        self._rect(content, x, y, width, height, 0.98, 0.99, 1.0, 0.82, 0.86, 0.92)
        plot_x = x + 46
        plot_y = y + 30
        plot_w = width - 62
        plot_h = height - 58

        points = self.visible_points
        values = [point[1] for point in points]
        resolved = "kohm"
        factor, unit_label = unit_scale(resolved)
        grid_step = self.stats.get("y_grid_step_value")
        latest = points[-1][0] if points else 120
        min_x = 0
        max_x = max(self.stats["window_seconds"], latest)

        if values and grid_step:
            min_y, max_y = custom_y_range(values, grid_step)
        elif values:
            min_y = min(values)
            max_y = max(values)
            if min_y == max_y:
                pad = max(abs(min_y) * 0.05, 1)
                min_y -= pad
                max_y += pad
            else:
                pad = (max_y - min_y) * 0.12
                min_y -= pad
                max_y += pad
        else:
            min_y = 0
            max_y = 1

        for index in range(7):
            ratio = index / 6
            gx = plot_x + ratio * plot_w
            self._line(content, gx, plot_y, gx, plot_y + plot_h, 0.82, 0.86, 0.92, 0.5)
            seconds = min_x + ratio * (max_x - min_x)
            self._text(content, format_time_axis(seconds), gx - 10, plot_y - 18, 8, 0.38, 0.44, 0.54)

        for value in y_tick_values(min_y, max_y, grid_step):
            ratio = (value - min_y) / max(max_y - min_y, 0.001)
            gy = plot_y + ratio * plot_h
            self._line(content, plot_x, gy, plot_x + plot_w, gy, 0.82, 0.86, 0.92, 0.5)
            self._text(content, axis_label(value / factor, grid_step / factor if grid_step else None), x + 5, gy - 3, 8, 0.38, 0.44, 0.54)

        self._text(content, f"电阻 {unit_label}", plot_x, y + height - 16, 10)
        self._text(content, "时间", x + width - 42, y + 12, 10)

        if len(points) < 2:
            self._text(content, "暂无曲线数据", x + width / 2 - 35, y + height / 2, 12, 0.38, 0.44, 0.54)
            return

        coords = []
        for point_time, resistance, _raw in points:
            px = plot_x + ((point_time - min_x) / max(max_x - min_x, 0.001)) * plot_w
            ratio = (resistance - min_y) / max(max_y - min_y, 0.001)
            py = plot_y + ratio * plot_h
            coords.append((px, py))
        if coords:
            commands = [f"{coords[0][0]:.2f} {coords[0][1]:.2f} m"]
            commands.extend(f"{px:.2f} {py:.2f} l" for px, py in coords[1:])
            content.append("0.08 0.48 0.45 RG 2 w " + " ".join(commands) + " S")
            px, py = coords[-1]
            self._rect(content, px - 3, py - 3, 6, 6, 0.9, 0.45, 0.25, 0.9, 0.45, 0.25)

    def _table_header(self, content, y):
        self._text(content, "序号", 42, y, 9)
        self._text(content, "时间(s)", 88, y, 9)
        self._text(content, "电阻(Ω)", 150, y, 9)
        self._text(content, "原始数据", 238, y, 9)
        self._line(content, 42, y - 6, 553, y - 6, 0.82, 0.86, 0.92, 0.7)

    def _text(self, content, text, x, y, size, r=0.09, g=0.13, b=0.2):
        encoded = text.encode("utf-16-be", errors="replace").hex().upper()
        content.append(f"BT {r:.3f} {g:.3f} {b:.3f} rg /F1 {size} Tf {x:.2f} {y:.2f} Td <{encoded}> Tj ET")

    def _line(self, content, x1, y1, x2, y2, r, g, b, width):
        content.append(f"{r:.3f} {g:.3f} {b:.3f} RG {width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def _rect(self, content, x, y, width, height, fr, fg, fb, sr, sg, sb):
        content.append(
            f"{fr:.3f} {fg:.3f} {fb:.3f} rg {x:.2f} {y:.2f} {width:.2f} {height:.2f} re f "
            f"{sr:.3f} {sg:.3f} {sb:.3f} RG 0.7 w {x:.2f} {y:.2f} {width:.2f} {height:.2f} re S"
        )


class MeasurementChannel(ttk.Frame):
    def __init__(self, master, app, channel_index):
        super().__init__(master)
        self.app = app
        self.channel_index = channel_index

        self.data_points = deque(maxlen=20_000)
        self.raw_samples = deque(maxlen=20_000)
        self.start_time = None
        self.raw_start_time = None
        self.last_recorded_time = None
        self.raw_bytes = 0
        self.raw_records = 0
        self.connected_port = None
        self.measurement_source = None
        self.autosave_path = None
        self.autosave_handle = None
        self.autosave_writer = None
        self.event_queue = queue.Queue()
        self.serial_worker = SerialWorker(self.event_queue)
        self.simulation_job = None
        self.auto_stop_job = None
        self.auto_stopped = False
        self.stats_dirty = True
        self.chart_dirty = True
        self.last_stats_update = 0.0
        self.last_chart_redraw = 0.0
        self.last_autosave_flush = 0.0
        self.autosave_flush_interval = 1.0
        self.log_line_count = 0
        self.unparsed_lines_since_log = 0
        self.last_unparsed_log_time = 0.0
        self.experiment_time_var = tk.StringVar(value=time.strftime("%Y-%m-%d %H:%M"))
        self.sample_interval_var = tk.StringVar(value="60")
        self.x_axis_hours_var = tk.StringVar(value="6")
        self.plant_id_var = tk.StringVar(value=f"绿萝{channel_index:02d}")
        self.temperature_var = tk.StringVar()
        self.humidity_var = tk.StringVar()
        self.electrode_var = tk.StringVar(value="叶片两点")
        self.soil_var = tk.StringVar(value="正常")
        self.y_grid_step_var = tk.StringVar()
        self.zoom_factor = 1.0
        self.pan_x_offset = 0.0
        self.pan_y_offset = 0.0
        self.drag_start = None
        self.current_chart_view = None
        self.plotted_points = []

        self._build_ui()

    def _build_ui(self):
        header = ttk.Frame(self, padding=(16, 14, 16, 8))
        header.pack(fill="x")

        title = ttk.Label(header, text=f"实时电阻曲线 - 通道{self.channel_index}", font=("Microsoft YaHei UI", 22, "bold"))
        title.pack(anchor="w")
        subtitle = ttk.Label(header, text="Windows 本地 GUI，每个通道独立读取一个硬件串口并记录一株植物。")
        subtitle.pack(anchor="w", pady=(4, 0))

        controls = ttk.Frame(self, padding=(16, 8))
        controls.pack(fill="x")

        ttk.Label(controls, text="串口").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_var.trace_add("write", lambda *_args: self.update_tab_label())
        self.plant_id_var.trace_add("write", lambda *_args: self.update_tab_label())
        self.port_combo = ttk.Combobox(controls, textvariable=self.port_var, width=18, state="readonly")
        self.port_combo.pack(side="left", padx=(6, 8))

        ttk.Button(controls, text="刷新串口", command=self.app.refresh_all_ports).pack(side="left", padx=(0, 12))

        ttk.Label(controls, text="波特率").pack(side="left")
        self.baud_var = tk.StringVar(value="115200")
        self.baud_combo = ttk.Combobox(
            controls,
            textvariable=self.baud_var,
            values=("9600", "19200", "38400", "57600", "115200", "230400"),
            width=10,
            state="readonly",
        )
        self.baud_combo.pack(side="left", padx=(6, 12))

        self.connect_button = ttk.Button(controls, text="连接", command=self.connect_serial)
        self.connect_button.pack(side="left")
        self.disconnect_button = ttk.Button(controls, text="断开", command=self.disconnect_serial, state="disabled")
        self.disconnect_button.pack(side="left", padx=(6, 12))

        self.auto_scale_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="y 轴自动缩放", variable=self.auto_scale_var, command=self.redraw_chart).pack(side="left")

        ttk.Button(controls, text="模拟数据", command=self.toggle_simulation).pack(side="right", padx=(8, 0))
        ttk.Button(controls, text="清空", command=self.clear_data).pack(side="right")
        ttk.Button(controls, text="导出记录CSV", command=self.export_raw_csv).pack(side="right", padx=(0, 8))
        ttk.Button(controls, text="导出PDF", command=self.export_pdf).pack(side="right", padx=(0, 8))
        ttk.Button(controls, text="重置缩放", command=self.reset_zoom).pack(side="right", padx=(0, 8))
        ttk.Button(controls, text="放大", command=self.zoom_in).pack(side="right", padx=(0, 6))
        ttk.Button(controls, text="缩小", command=self.zoom_out).pack(side="right", padx=(0, 6))

        experiment = ttk.Frame(self, padding=(16, 2, 16, 8))
        experiment.pack(fill="x")

        ttk.Label(experiment, text="实验时间").pack(side="left")
        ttk.Entry(experiment, textvariable=self.experiment_time_var, width=18).pack(side="left", padx=(6, 12))

        ttk.Label(experiment, text="测试时长(h)").pack(side="left")
        self.window_var = tk.StringVar(value="24")
        window_entry = ttk.Entry(experiment, textvariable=self.window_var, width=8)
        window_entry.pack(side="left", padx=(6, 12))
        window_entry.bind("<Return>", lambda _event: self.update_duration_setting())

        ttk.Label(experiment, text="记录间隔(s)").pack(side="left")
        interval_entry = ttk.Entry(experiment, textvariable=self.sample_interval_var, width=7)
        interval_entry.pack(side="left", padx=(6, 12))

        ttk.Label(experiment, text="横轴范围(h)").pack(side="left")
        x_axis_entry = ttk.Entry(experiment, textvariable=self.x_axis_hours_var, width=7)
        x_axis_entry.pack(side="left", padx=(6, 12))
        x_axis_entry.bind("<Return>", lambda _event: self.reset_zoom())

        ttk.Label(experiment, text="植物编号").pack(side="left")
        ttk.Entry(experiment, textvariable=self.plant_id_var, width=10).pack(side="left", padx=(6, 12))

        ttk.Label(experiment, text="温度°C").pack(side="left")
        ttk.Entry(experiment, textvariable=self.temperature_var, width=7).pack(side="left", padx=(6, 12))

        ttk.Label(experiment, text="湿度%").pack(side="left")
        ttk.Entry(experiment, textvariable=self.humidity_var, width=7).pack(side="left", padx=(6, 12))

        ttk.Label(experiment, text="电极位置").pack(side="left")
        ttk.Combobox(
            experiment,
            textvariable=self.electrode_var,
            values=("叶片两点", "叶柄-叶片", "茎段两点", "自定义"),
            width=10,
            state="readonly",
        ).pack(side="left", padx=(6, 12))

        ttk.Label(experiment, text="土壤状态").pack(side="left")
        ttk.Combobox(
            experiment,
            textvariable=self.soil_var,
            values=("正常", "偏干", "干燥", "湿润", "浇水后"),
            width=8,
            state="readonly",
        ).pack(side="left", padx=(6, 12))

        ttk.Label(experiment, text="Y格差(Ω)").pack(side="left")
        y_grid_entry = ttk.Entry(experiment, textvariable=self.y_grid_step_var, width=8)
        y_grid_entry.pack(side="left", padx=(6, 12))
        y_grid_entry.bind("<Return>", lambda _event: self.redraw_chart())

        status = ttk.Frame(self, padding=(16, 2, 16, 8))
        status.pack(fill="x")
        self.status_var = tk.StringVar(value="未连接")
        self.current_var = tk.StringVar(value="当前：--")
        self.average_var = tk.StringVar(value="平均：--")
        self.min_var = tk.StringVar(value="最小：--")
        self.max_var = tk.StringVar(value="最大：--")
        self.bytes_var = tk.StringVar(value="收到字节：0")
        self.parsed_var = tk.StringVar(value="解析成功：0")
        self.raw_var = tk.StringVar(value="保存记录：0")
        self.autosave_var = tk.StringVar(value="自动CSV：未开始")

        for variable in (
            self.status_var,
            self.current_var,
            self.average_var,
            self.min_var,
            self.max_var,
            self.bytes_var,
            self.parsed_var,
            self.raw_var,
            self.autosave_var,
        ):
            ttk.Label(status, textvariable=variable).pack(side="left", padx=(0, 16))

        self.canvas = tk.Canvas(self, bg="#fbfcff", highlightthickness=1, highlightbackground="#d9e1ed")
        self.canvas.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.canvas.bind("<Configure>", lambda _event: self.redraw_chart())
        self.canvas.bind("<Motion>", self.show_point_tooltip)
        self.canvas.bind("<Leave>", lambda _event: self.hide_point_tooltip())
        self.canvas.bind("<ButtonPress-1>", self.start_pan)
        self.canvas.bind("<B1-Motion>", self.pan_chart)
        self.canvas.bind("<ButtonRelease-1>", self.end_pan)
        self.canvas.bind("<MouseWheel>", self.zoom_with_wheel)
        self.canvas.bind("<Button-4>", lambda event: self.zoom_with_wheel(event, direction=1))
        self.canvas.bind("<Button-5>", lambda event: self.zoom_with_wheel(event, direction=-1))

        log_frame = ttk.Frame(self, padding=(16, 0, 16, 16))
        log_frame.pack(fill="both")
        self.log = tk.Text(log_frame, height=7, bg="#111827", fg="#d8dee9", insertbackground="#d8dee9")
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scrollbar.set)

    def tab_label(self):
        port = self.connected_port or self.port_var.get()
        if not port or port.startswith("未"):
            port = "未选择"
        plant = value_or_dash(self.plant_id_var.get())
        return f"通道{self.channel_index} · {port} · {plant}"

    def update_tab_label(self):
        self.app.update_tab_label(self)

    def refresh_ports(self, ports=None):
        if list_ports is None:
            self.port_combo["values"] = ("未安装 pyserial",)
            self.port_var.set("未安装 pyserial")
            self.connect_button.configure(state="disabled")
            self.status_var.set("请先安装 pyserial：python -m pip install pyserial")
            self.update_tab_label()
            return

        if ports is None:
            ports = [port.device for port in list_ports.comports()]

        if self.connected_port:
            values = list(ports)
            if self.connected_port not in values:
                values.insert(0, self.connected_port)
            self.port_combo["values"] = values
            self.port_var.set(self.connected_port)
            self.connect_button.configure(state="disabled")
            self.disconnect_button.configure(state="normal")
            self.port_combo.configure(state="disabled")
            self.baud_combo.configure(state="disabled")
            self.update_tab_label()
            return

        if not ports:
            self.port_combo["values"] = ("未发现串口",)
            self.port_var.set("未发现串口")
            self.connect_button.configure(state="disabled")
        else:
            self.port_combo["values"] = ports
            if self.port_var.get() not in ports:
                self.port_var.set(ports[0])
            self.connect_button.configure(state="normal")
        self.port_combo.configure(state="readonly")
        self.baud_combo.configure(state="readonly")
        self.update_tab_label()

    def connect_serial(self):
        port = self.port_var.get()
        if not port or port.startswith("未"):
            self.status_var.set("没有可用串口")
            return
        if self.app.is_port_in_use(port, self):
            self.status_var.set(f"{port} 已被其他通道连接")
            messagebox.showwarning("串口已占用", f"{port} 已被其他通道连接，请选择另一个串口。")
            return

        self.stop_simulation()
        self.auto_stopped = False
        try:
            self.serial_worker.start(port, int(self.baud_var.get()))
        except Exception as exc:
            self.status_var.set(f"连接失败：{exc}")
            messagebox.showerror("连接失败", str(exc))
            return

        self.connected_port = port
        self.measurement_source = port
        self.status_var.set(f"已连接：{port}")
        self.connect_button.configure(state="disabled")
        self.disconnect_button.configure(state="normal")
        self.port_combo.configure(state="disabled")
        self.baud_combo.configure(state="disabled")
        self.app.refresh_all_ports()
        self.update_tab_label()

    def disconnect_serial(self):
        self.serial_worker.stop()
        self.cancel_auto_stop()
        saved_path = self.autosave_path
        self.close_autosave_file()
        self.connected_port = None
        if saved_path:
            self.status_var.set(f"未连接，CSV 已保存：{os.path.basename(saved_path)}")
        else:
            self.status_var.set("未连接")
        self.connect_button.configure(state="normal")
        self.disconnect_button.configure(state="disabled")
        self.port_combo.configure(state="readonly")
        self.baud_combo.configure(state="readonly")
        self.app.refresh_all_ports()
        self.update_tab_label()

    def process_events(self):
        processed = 0
        deadline = time.monotonic() + 0.01

        while processed < 500 and time.monotonic() < deadline:
            try:
                event, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break

            processed += 1
            if event == "bytes":
                self.raw_bytes += payload
                self.request_ui_update()
            elif event == "line":
                self.handle_line(payload)
            elif event == "error":
                self.status_var.set(f"读取失败：{payload}")
                self.disconnect_serial()

        self.flush_pending_ui_updates()
        return not self.event_queue.empty()

    def handle_line(self, line):
        if self.auto_stopped:
            return
        now = time.monotonic()
        if not self.should_record_sample(now):
            return
        resistance = ResistanceParser.parse(line)
        if resistance is None:
            self.append_unparsed_log(line, now)
            self.request_ui_update()
            return

        if self.start_time is not None and now - self.start_time > self.measurement_duration():
            self.auto_stop_measurement()
            return
        self.record_raw_sample(line, resistance, now)
        self.append_data_point(resistance, line, now)

    def request_ui_update(self, redraw=False):
        self.stats_dirty = True
        if redraw:
            self.chart_dirty = True

    def flush_pending_ui_updates(self, force=False):
        now = time.monotonic()
        if self.stats_dirty and (force or now - self.last_stats_update >= 0.2):
            self.update_stats()
            self.stats_dirty = False
            self.last_stats_update = now
        if self.chart_dirty and (force or now - self.last_chart_redraw >= 0.25):
            self.redraw_chart()
            self.chart_dirty = False
            self.last_chart_redraw = now

    def append_unparsed_log(self, line, now):
        self.unparsed_lines_since_log += 1
        if self.last_unparsed_log_time and now - self.last_unparsed_log_time < 1.0:
            return

        count = self.unparsed_lines_since_log
        prefix = "未解析" if count == 1 else f"未解析 {count} 条，最近"
        self.append_log(f"{prefix} <= {show_control_characters(line)}")
        self.unparsed_lines_since_log = 0
        self.last_unparsed_log_time = now

    def should_record_sample(self, now_monotonic):
        interval = self.sample_interval()
        if interval <= 0:
            return True
        if self.last_recorded_time is None:
            return True
        return now_monotonic - self.last_recorded_time >= interval

    def record_raw_sample(self, raw_line, parsed_resistance, now_monotonic=None):
        if now_monotonic is None:
            now_monotonic = time.monotonic()
        if self.raw_start_time is None:
            self.raw_start_time = now_monotonic
        self.last_recorded_time = now_monotonic
        self.raw_records += 1
        sample = (
            self.raw_records,
            time.time(),
            now_monotonic - self.raw_start_time,
            raw_line,
            parsed_resistance,
        )
        self.raw_samples.append(sample)
        self.write_autosave_sample(sample)

    def append_data_point(self, resistance, raw_line, now=None):
        if self.auto_stopped:
            return
        if now is None:
            now = time.monotonic()
        if self.start_time is None:
            self.start_time = now
            self.schedule_auto_stop()
        elapsed = now - self.start_time
        duration = self.measurement_duration()
        if elapsed > duration:
            self.auto_stop_measurement()
            return
        self.data_points.append((elapsed, resistance, raw_line))

        self.append_log(f"{format_resistance(resistance, preferred='kohm')} <= {show_control_characters(raw_line)}")
        self.request_ui_update(redraw=True)
        if elapsed >= duration:
            self.auto_stop_measurement()

    def visible_points(self):
        if not self.data_points:
            return []
        window_seconds = self.measurement_duration()
        latest = self.data_points[-1][0]
        max_x = max(window_seconds, latest)
        return [point for point in self.data_points if point[0] <= max_x]

    def update_stats(self):
        visible = self.visible_points()
        self.bytes_var.set(f"收到字节：{self.raw_bytes}")
        self.parsed_var.set(f"解析成功：{len(self.data_points)}")
        self.raw_var.set(f"保存记录：{self.raw_records}")

        if not visible:
            self.current_var.set("当前：--")
            self.average_var.set("平均：--")
            self.min_var.set("最小：--")
            self.max_var.set("最大：--")
            return

        values = [point[1] for point in visible]
        resolved = "kohm"
        self.current_var.set(f"当前：{format_resistance(values[-1], resolved=resolved)}")
        self.average_var.set(f"平均：{format_resistance(sum(values) / len(values), resolved=resolved)}")
        self.min_var.set(f"最小：{format_resistance(min(values), resolved=resolved)}")
        self.max_var.set(f"最大：{format_resistance(max(values), resolved=resolved)}")

    def redraw_chart(self):
        canvas = self.canvas
        width = max(canvas.winfo_width(), 320)
        height = max(canvas.winfo_height(), 280)
        canvas.delete("all")

        margin_left = 72
        margin_top = 28
        margin_right = 30
        margin_bottom = 44
        plot_x = margin_left
        plot_y = margin_top
        plot_w = width - margin_left - margin_right
        plot_h = height - margin_top - margin_bottom

        visible = self.visible_points()
        latest = visible[-1][0] if visible else 0
        x_window = self.x_axis_window_seconds()
        if x_window is None:
            full_max_x = max(self.measurement_duration(), latest, 1)
            base_span = full_max_x
        else:
            full_max_x = max(x_window, latest, 1)
            base_span = min(x_window, full_max_x)

        x_span = max(1, min(base_span / self.zoom_factor, full_max_x))
        if x_span >= full_max_x:
            self.pan_x_offset = 0.0
        if self.zoom_factor <= 1:
            self.pan_y_offset = 0.0
        self.pan_x_offset = max(0, min(self.pan_x_offset, max(0, full_max_x - x_span)))
        max_x = full_max_x - self.pan_x_offset
        min_x = max(0, max_x - x_span)

        display_points = [point for point in visible if min_x <= point[0] <= max_x]
        range_points = display_points or visible
        values = [point[1] for point in range_points]
        resolved = "kohm"
        factor, unit_label = unit_scale(resolved)

        if values and self.auto_scale_var.get():
            min_y = min(values)
            max_y = max(values)
            if min_y == max_y:
                pad = max(abs(min_y) * 0.05, 1)
                min_y -= pad
                max_y += pad
            else:
                pad = (max_y - min_y) * 0.12
                min_y -= pad
                max_y += pad
        else:
            min_y = 0
            max_y = 1_000

        grid_step = self.y_grid_step()
        if values and grid_step:
            min_y, max_y = custom_y_range(values, grid_step)

        if values and self.zoom_factor > 1 and max_y > min_y:
            center = (min_y + max_y) / 2
            span = max((max_y - min_y) / self.zoom_factor, 0.000001)
            min_y = center - span / 2 + self.pan_y_offset
            max_y = center + span / 2 + self.pan_y_offset

        self.current_chart_view = {
            "plot_x": plot_x,
            "plot_y": plot_y,
            "plot_w": plot_w,
            "plot_h": plot_h,
            "min_x": min_x,
            "max_x": max_x,
            "min_y": min_y,
            "max_y": max_y,
            "full_max_x": full_max_x,
        }

        for index in range(7):
            ratio = index / 6
            x = plot_x + ratio * plot_w
            canvas.create_line(x, plot_y, x, plot_y + plot_h, fill="#d9e1ed")
            seconds = min_x + ratio * (max_x - min_x)
            canvas.create_text(x, plot_y + plot_h + 22, text=format_time_axis(seconds), fill="#617089", font=("Segoe UI", 9))

        for value in y_tick_values(min_y, max_y, grid_step):
            ratio = (max_y - value) / max(max_y - min_y, 0.001)
            y = plot_y + ratio * plot_h
            canvas.create_line(plot_x, y, plot_x + plot_w, y, fill="#d9e1ed")
            canvas.create_text(34, y, text=axis_label(value / factor, grid_step / factor if grid_step else None), fill="#617089", font=("Segoe UI", 9))

        canvas.create_line(plot_x, plot_y, plot_x, plot_y + plot_h, plot_x + plot_w, plot_y + plot_h, fill="#69758a")
        canvas.create_text(plot_x + 36, 14, text=f"电阻 {unit_label}", fill="#162033", font=("Segoe UI", 10, "bold"))
        canvas.create_text(width - 44, height - 17, text="时间", fill="#162033", font=("Segoe UI", 10, "bold"))

        self.plotted_points = []
        if len(display_points) < 2:
            message = "连接串口或启动模拟数据后，曲线会显示在这里。" if not visible else "当前缩放范围内数据点不足。"
            canvas.create_text(width / 2, height / 2, text=message, fill="#617089")
            return

        plot_points = self.downsample_points(display_points, max(400, int(plot_w * 2)))
        coords = []
        for point_time, resistance, raw_line in plot_points:
            x = plot_x + ((point_time - min_x) / max(max_x - min_x, 0.001)) * plot_w
            y_ratio = (resistance - min_y) / max(max_y - min_y, 0.001)
            y = plot_y + (1 - y_ratio) * plot_h
            coords.extend([x, y])
            self.plotted_points.append((x, y, point_time, resistance, raw_line))

        canvas.create_line(*coords, fill="#147a72", width=3, smooth=True)
        canvas.create_oval(coords[-2] - 4, coords[-1] - 4, coords[-2] + 4, coords[-1] + 4, fill="#e5743f", outline="")

    def downsample_points(self, points, max_points):
        count = len(points)
        if count <= max_points:
            return points
        if max_points < 3:
            return [points[0], points[-1]]

        bucket_count = max(1, (max_points - 2) // 2)
        bucket_size = (count - 2) / bucket_count
        sampled = [points[0]]
        for bucket_index in range(bucket_count):
            start = 1 + int(bucket_index * bucket_size)
            end = 1 + int((bucket_index + 1) * bucket_size)
            if bucket_index == bucket_count - 1:
                end = count - 1
            bucket = points[start:end]
            if not bucket:
                continue
            min_point = min(bucket, key=lambda point: point[1])
            max_point = max(bucket, key=lambda point: point[1])
            if min_point is max_point:
                sampled.append(min_point)
            elif min_point[0] <= max_point[0]:
                sampled.extend([min_point, max_point])
            else:
                sampled.extend([max_point, min_point])
        sampled.append(points[-1])
        return sampled

    def y_grid_step(self):
        try:
            value = float(self.y_grid_step_var.get().strip())
        except ValueError:
            return None
        return value if value > 0 else None

    def zoom_in(self):
        self.zoom_factor = min(self.zoom_factor * 1.5, 16)
        self.clamp_pan()
        self.redraw_chart()

    def zoom_out(self):
        self.zoom_factor = max(self.zoom_factor / 1.5, 1)
        self.clamp_pan()
        self.redraw_chart()

    def reset_zoom(self):
        self.zoom_factor = 1.0
        self.pan_x_offset = 0.0
        self.pan_y_offset = 0.0
        self.redraw_chart()

    def zoom_with_wheel(self, event, direction=None):
        if direction is None:
            direction = 1 if event.delta > 0 else -1
        if direction > 0:
            self.zoom_factor = min(self.zoom_factor * 1.12, 16)
        else:
            self.zoom_factor = max(self.zoom_factor / 1.12, 1)
        self.clamp_pan()
        self.redraw_chart()

    def start_pan(self, event):
        view = self.current_chart_view
        if not view:
            self.drag_start = None
            return
        in_plot = (
            view["plot_x"] <= event.x <= view["plot_x"] + view["plot_w"]
            and view["plot_y"] <= event.y <= view["plot_y"] + view["plot_h"]
        )
        self.drag_start = (event.x, event.y) if in_plot else None

    def pan_chart(self, event):
        if self.drag_start is None or not self.current_chart_view:
            return
        last_x, last_y = self.drag_start
        dx = event.x - last_x
        dy = event.y - last_y
        view = self.current_chart_view
        can_pan_x = (view["max_x"] - view["min_x"]) < view["full_max_x"] - 0.001
        if not can_pan_x and self.zoom_factor <= 1:
            return
        x_span = max(view["max_x"] - view["min_x"], 0.001)
        y_span = max(view["max_y"] - view["min_y"], 0.001)
        if can_pan_x:
            self.pan_x_offset += (dx / max(view["plot_w"], 1)) * x_span
        if self.zoom_factor > 1:
            self.pan_y_offset += (dy / max(view["plot_h"], 1)) * y_span
        self.clamp_pan()
        self.drag_start = (event.x, event.y)
        self.redraw_chart()

    def end_pan(self, _event):
        self.drag_start = None

    def clamp_pan(self):
        latest = self.data_points[-1][0] if self.data_points else 0
        x_window = self.x_axis_window_seconds()
        if x_window is None:
            full_max_x = max(self.measurement_duration(), latest, 1)
            base_span = full_max_x
        else:
            full_max_x = max(x_window, latest, 1)
            base_span = min(x_window, full_max_x)
        x_span = max(1, min(base_span / self.zoom_factor, full_max_x))
        if x_span >= full_max_x:
            self.pan_x_offset = 0.0
            if self.zoom_factor <= 1:
                self.pan_y_offset = 0.0
            return
        self.pan_x_offset = max(0, min(self.pan_x_offset, max(0, full_max_x - x_span)))

    def show_point_tooltip(self, event):
        self.canvas.delete("tooltip")
        if not self.plotted_points:
            return

        nearest = None
        nearest_distance = float("inf")
        for x, y, point_time, resistance, raw_line in self.plotted_points:
            distance = math.hypot(x - event.x, y - event.y)
            if distance < nearest_distance:
                nearest_distance = distance
                nearest = (x, y, point_time, resistance, raw_line)

        if nearest is None or nearest_distance > 14:
            return

        x, y, point_time, resistance, _raw_line = nearest
        text = f"时间：{format_time_axis(point_time)}\n电阻：{format_resistance(resistance, preferred='kohm')}"
        box_width = max(112, len(max(text.splitlines(), key=len)) * 8 + 18)
        box_height = 48
        box_x = event.x + 14
        box_y = event.y - box_height - 10
        if box_x + box_width > self.canvas.winfo_width() - 8:
            box_x = event.x - box_width - 14
        if box_y < 8:
            box_y = event.y + 14

        self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#e5743f", outline="", tags="tooltip")
        self.canvas.create_rectangle(
            box_x,
            box_y,
            box_x + box_width,
            box_y + box_height,
            fill="#ffffff",
            outline="#c7d0df",
            tags="tooltip",
        )
        self.canvas.create_text(
            box_x + 9,
            box_y + 9,
            anchor="nw",
            text=text,
            fill="#162033",
            font=("Segoe UI", 10),
            tags="tooltip",
        )

    def hide_point_tooltip(self):
        self.canvas.delete("tooltip")

    def toggle_simulation(self):
        if self.simulation_job is not None:
            self.stop_simulation()
            self.status_var.set("模拟数据已停止")
            return
        if self.connected_port:
            self.disconnect_serial()
        self.auto_stopped = False
        self.measurement_source = "模拟"
        self.status_var.set("模拟数据中")
        self._simulate_tick(0.0)

    def _simulate_tick(self, phase):
        phase += 0.16
        baseline = 820 + 180 * math.sin(phase / 2.5)
        ripple = 38 * math.sin(phase * 2.3)
        noise = random.uniform(-12, 12)
        value = max(0.1, baseline + ripple + noise)
        raw_line = f"R={trim_number(value)} ohm"
        now = time.monotonic()
        if not self.should_record_sample(now):
            self.simulation_job = self.after(350, lambda: self._simulate_tick(phase))
            return
        if self.start_time is not None and now - self.start_time > self.measurement_duration():
            self.auto_stop_measurement()
            return
        self.record_raw_sample(raw_line, value, now)
        self.append_data_point(value, raw_line, now)
        if self.auto_stopped:
            return
        self.simulation_job = self.after(350, lambda: self._simulate_tick(phase))

    def stop_simulation(self):
        if self.simulation_job is not None:
            self.after_cancel(self.simulation_job)
            self.simulation_job = None

    def clear_data(self):
        self.data_points.clear()
        self.raw_samples.clear()
        self.start_time = None
        self.raw_start_time = None
        self.last_recorded_time = None
        self.auto_stopped = False
        self.cancel_auto_stop()
        self.close_autosave_file()
        self.autosave_path = None
        self.measurement_source = None
        self.autosave_var.set("自动CSV：未开始")
        self.raw_bytes = 0
        self.raw_records = 0
        self.log.delete("1.0", "end")
        self.log_line_count = 0
        self.update_stats()
        self.redraw_chart()

    def update_duration_setting(self):
        self.redraw_chart()
        self.schedule_auto_stop()

    def measurement_duration(self):
        try:
            hours = float(self.window_var.get().strip())
        except ValueError:
            hours = 24
        seconds = hours * 3600
        return max(5, min(86400, seconds))

    def sample_interval(self):
        try:
            seconds = float(self.sample_interval_var.get().strip())
        except ValueError:
            seconds = 60
        return max(0, seconds)

    def x_axis_window_seconds(self):
        cleaned = self.x_axis_hours_var.get().strip()
        if not cleaned:
            return None
        try:
            hours = float(cleaned)
        except ValueError:
            return None
        if hours <= 0:
            return None
        return min(self.measurement_duration(), max(5, hours * 3600))

    def schedule_auto_stop(self):
        self.cancel_auto_stop()
        if self.start_time is None or self.auto_stopped:
            return
        remaining = self.measurement_duration() - (time.monotonic() - self.start_time)
        if remaining <= 0:
            self.auto_stop_measurement()
            return
        self.auto_stop_job = self.after(int(remaining * 1000), self.auto_stop_measurement)

    def cancel_auto_stop(self):
        if self.auto_stop_job is not None:
            try:
                self.after_cancel(self.auto_stop_job)
            except tk.TclError:
                pass
            self.auto_stop_job = None

    def auto_stop_measurement(self):
        if self.auto_stopped:
            return
        self.auto_stopped = True
        self.cancel_auto_stop()
        self.stop_simulation()
        self.serial_worker.stop()
        saved_path = self.autosave_path
        self.close_autosave_file()
        self.connected_port = None
        self.connect_button.configure(state="normal")
        self.disconnect_button.configure(state="disabled")
        self.port_combo.configure(state="readonly")
        self.baud_combo.configure(state="readonly")
        if saved_path:
            self.status_var.set(f"已到测试时长，CSV 已保存：{os.path.basename(saved_path)}")
        else:
            self.status_var.set("已到测试时长，自动停止")
        self.append_log("已到测试时长，自动停止。")
        self.update_stats()
        self.redraw_chart()
        self.app.refresh_all_ports()
        self.update_tab_label()

    def append_log(self, line):
        stamp = time.strftime("%H:%M:%S")
        self.log.insert("end", f"[{stamp}] {line}\n")
        self.log_line_count += 1
        if self.log_line_count > 260:
            remove_count = self.log_line_count - 220
            self.log.delete("1.0", f"{remove_count + 1}.0")
            self.log_line_count = 220
        self.log.see("end")

    def ensure_autosave_file(self):
        if self.autosave_handle is not None:
            return

        output_dir = os.path.join(os.getcwd(), "output")
        os.makedirs(output_dir, exist_ok=True)
        channel_part = safe_filename_part(f"通道{self.channel_index}", f"channel{self.channel_index}")
        port_label = self.measurement_source or self.connected_port or self.port_var.get()
        port_part = safe_filename_part(port_label, "unknown_port")
        plant_part = safe_filename_part(self.plant_id_var.get(), "unknown_plant")
        self.autosave_path = os.path.join(
            output_dir,
            f"植物阻抗自动记录_{channel_part}_{port_part}_{plant_part}_{time.strftime('%Y%m%d_%H%M%S')}.csv",
        )
        self.autosave_handle = open(self.autosave_path, "w", encoding="utf-8-sig", newline="")
        self.autosave_writer = csv.writer(self.autosave_handle)
        self.autosave_writer.writerow(
            [
                "index",
                "received_at",
                "elapsed_s",
                "parsed_resistance_ohm",
                "parsed_resistance_kohm",
                "raw_line",
            ]
        )
        self.autosave_handle.flush()
        self.last_autosave_flush = time.monotonic()
        self.autosave_var.set(f"自动CSV：{os.path.basename(self.autosave_path)}")

    def write_autosave_sample(self, sample):
        try:
            self.ensure_autosave_file()
            index, received_at, elapsed, raw_line, parsed_resistance = sample
            self.autosave_writer.writerow(
                [
                    index,
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(received_at)),
                    f"{elapsed:.6f}",
                    "" if parsed_resistance is None else f"{parsed_resistance:.6f}",
                    "" if parsed_resistance is None else f"{parsed_resistance / 1000:.6f}",
                    raw_line,
                ]
            )
            now = time.monotonic()
            if now - self.last_autosave_flush >= self.autosave_flush_interval:
                self.autosave_handle.flush()
                self.last_autosave_flush = now
        except Exception as exc:
            self.status_var.set(f"自动CSV写入失败：{exc}")

    def close_autosave_file(self):
        if self.autosave_handle is None:
            return
        try:
            self.autosave_handle.flush()
            self.autosave_handle.close()
        finally:
            self.autosave_handle = None
            self.autosave_writer = None

    def export_pdf(self):
        if not self.data_points:
            messagebox.showinfo("没有可导出的数据", "请先连接串口并采集到电阻数据。")
            return

        channel_part = safe_filename_part(f"通道{self.channel_index}", f"channel{self.channel_index}")
        plant_part = safe_filename_part(self.plant_id_var.get(), "unknown_plant")
        default_name = f"植物阻抗测量报告_{channel_part}_{plant_part}_{time.strftime('%Y%m%d_%H%M%S')}.pdf"
        path = filedialog.asksaveasfilename(
            title="导出实验 PDF",
            defaultextension=".pdf",
            initialfile=default_name,
            filetypes=(("PDF 文件", "*.pdf"), ("所有文件", "*.*")),
        )
        if not path:
            return

        try:
            window_seconds = self.measurement_duration()
            stats = {
                "raw_records": self.raw_records,
                "raw_bytes": self.raw_bytes,
                "window_seconds": window_seconds,
                "y_grid_step_value": self.y_grid_step(),
                "metadata": {
                    "channel": f"通道{self.channel_index}",
                    "serial_port": value_or_dash(self.measurement_source or self.connected_port or self.port_var.get()),
                    "baudrate": value_or_dash(self.baud_var.get()),
                    "experiment_time": value_or_dash(self.experiment_time_var.get()),
                    "duration": f"{trim_number(window_seconds / 3600)} 小时    记录间隔：{trim_number(self.sample_interval())} 秒",
                    "plant_id": value_or_dash(self.plant_id_var.get()),
                    "temperature": value_or_dash(self.temperature_var.get()),
                    "humidity": value_or_dash(self.humidity_var.get()),
                    "electrode_position": value_or_dash(self.electrode_var.get()),
                    "soil_state": value_or_dash(self.soil_var.get()),
                    "y_grid_step": f"{trim_number(self.y_grid_step())} Ω" if self.y_grid_step() else "自动",
                },
            }
            report = SimplePDFReport(path, self.data_points, self.visible_points(), stats)
            report.write()
            self.status_var.set(f"PDF 已导出：{path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def export_raw_csv(self):
        if not self.raw_samples and not self.autosave_path:
            messagebox.showinfo("没有可导出的记录数据", "请先连接串口并记录到电阻数据。")
            return

        channel_part = safe_filename_part(f"通道{self.channel_index}", f"channel{self.channel_index}")
        plant_part = safe_filename_part(self.plant_id_var.get(), "unknown_plant")
        default_name = f"植物阻抗记录数据_{channel_part}_{plant_part}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        path = filedialog.asksaveasfilename(
            title="导出记录数据 CSV",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=(("CSV 文件", "*.csv"), ("所有文件", "*.*")),
        )
        if not path:
            return

        try:
            if self.autosave_path and os.path.exists(self.autosave_path):
                if self.autosave_handle is not None:
                    self.autosave_handle.flush()
                if os.path.abspath(self.autosave_path) != os.path.abspath(path):
                    shutil.copyfile(self.autosave_path, path)
            else:
                with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(
                        [
                            "index",
                            "received_at",
                            "elapsed_s",
                            "parsed_resistance_ohm",
                            "parsed_resistance_kohm",
                            "raw_line",
                        ]
                    )
                    for index, received_at, elapsed, raw_line, parsed_resistance in self.raw_samples:
                        writer.writerow(
                            [
                                index,
                                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(received_at)),
                                f"{elapsed:.6f}",
                                "" if parsed_resistance is None else f"{parsed_resistance:.6f}",
                                "" if parsed_resistance is None else f"{parsed_resistance / 1000:.6f}",
                                raw_line,
                            ]
                        )
            self.status_var.set(f"记录数据已导出：{path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def close(self):
        self.stop_simulation()
        self.cancel_auto_stop()
        self.serial_worker.stop()
        self.close_autosave_file()


class ResistanceApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("四路实时电阻曲线 GUI - Windows")
        self.geometry("1240x860")
        self.minsize(980, 700)
        self.configure(bg="#f5f7fb")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)

        self.channels = []
        for index in range(1, 5):
            channel = MeasurementChannel(self.notebook, self, index)
            self.channels.append(channel)
            self.notebook.add(channel, text=channel.tab_label())

        self.refresh_all_ports()
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        self.after(16, self.process_events)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def refresh_all_ports(self):
        ports = None
        if list_ports is not None:
            ports = [port.device for port in list_ports.comports()]
        for channel in self.channels:
            channel.refresh_ports(ports)

    def is_port_in_use(self, port, requester):
        return any(channel is not requester and channel.connected_port == port for channel in self.channels)

    def update_tab_label(self, channel):
        if not hasattr(self, "notebook"):
            return
        try:
            self.notebook.index(channel)
        except tk.TclError:
            return
        self.notebook.tab(channel, text=channel.tab_label())

    def on_tab_changed(self, _event):
        channel = self.current_channel()
        if channel is not None:
            channel.flush_pending_ui_updates(force=True)

    def current_channel(self):
        selected = self.notebook.select()
        if not selected:
            return None
        widget = self.nametowidget(selected)
        return widget if isinstance(widget, MeasurementChannel) else None

    def process_events(self):
        has_pending = False
        for channel in self.channels:
            has_pending = channel.process_events() or has_pending
        self.after(1 if has_pending else 16, self.process_events)

    def close(self):
        for channel in self.channels:
            channel.close()
        self.destroy()


if __name__ == "__main__":
    app = ResistanceApp()
    app.mainloop()
