WHEELTEC C100/C70 双摄像头定时拍照示例（Windows 优先）

推荐使用图形界面：
1) 双击 start_camera_gui_windows.bat。
2) 点击“刷新全部设备”，分别打开“相机 1”和“相机 2”标签页。
3) 每个标签页选择一个 WHEELTEC USB 摄像头，点击“连接并预览”并确认实时画面。
4) 为两个通道设置不同的“照片标识”，例如“左侧植株”和“右侧植株”。
5) 分别设置固定间隔和/或固定时间点，再启动拍照计划；也可使用顶部的批量按钮。

重要说明：
WHEELTEC C100/C70 是 UVC USB 摄像头，不是串口设备，不会对应 COM3/COM4。
GUI 会像植物阻抗程序管理多个串口一样提供两个独立相机标签页。两个相同型号摄像头会使用设备路径区分并绑定，不会只依赖相同的 VID:PID。

双摄像头保存规则：
1) 图片按日期、相机通道和照片标识分别建目录。
2) 示例：
   output/2026-07-14/camera1_左侧植株/plant_camera1_左侧植株_20260714_120000_123_interval.png
   output/2026-07-14/camera2_右侧植株/plant_camera2_右侧植株_20260714_120000_127_interval.png
3) camera1/camera2 同时写入目录和文件名，两个相机同一秒拍照也不会互相覆盖。
4) 切换标签页不会停止另一个相机的后台预览和拍照计划。

1080p 画质规则：
1) 每个摄像头连接时请求 MJPG 1920 x 1080 @ 10fps，在保持 1080p 的同时降低双路 USB 带宽压力。
2) 保存图片直接使用摄像头原始 1080p 帧，不做低分辨率插值放大。
3) 如果实际视频流仍是 640 x 480，GUI 会显示实际尺寸并阻止拍照计划启动。
4) 双路 1080p 建议使用主板 USB 3.x 接口，避免两个摄像头共用低带宽 USB Hub。

GUI 图片保存规则：
1) GUI 固定保存为 PNG，不再提供 JPG/JPEG 格式选择。
2) PNG 使用无损编码，压缩级别设为 0，不对 OpenCV 收到的画面帧进行有损压缩。
3) PNG 文件会明显大于原来的 JPG，这是预期行为。

双摄像头识别优化：
1) GUI 每 2 秒在后台扫描摄像头，同时检查 DirectShow 和 Media Foundation；扩展坞刚接入时的一次短暂漏报不会立即清空设备列表。
2) “连接全部”会依次连接两个摄像头，等待第一个视频流建立后再初始化第二个。
3) 建议两个摄像头分别接入主板 USB 3.x 接口，不要共用无源 USB Hub 或同一组前置面板接口。
4) 如果 GUI 始终只显示一个设备，请先在 Windows 设备管理器中确认系统是否同时显示两个摄像头。

依赖：
1) OpenCV >= 4.10
2) Python 3
3) cv2-enumerate-cameras（用于按 USB 硬件身份识别 Windows 摄像头）
4) Pillow（用于 GUI 实时预览）

安装依赖：
python -m pip install opencv-python cv2-enumerate-cameras

首次设置目标摄像头：
1) 将 WHEELTEC C100/C70 连接到 Windows 电脑。
2) 运行：
   python camera.py --list-cameras
3) 找到 WHEELTEC 摄像头对应的 camera-id（格式为 VID:PID）。
4) 将这个 camera-id 写入配置文件；以后即使摄像头序号变化，程序也会重新查找它。

安全规则：
1) 程序不再默认打开序号 0。
2) 找不到指定的 WHEELTEC 摄像头时会停止，不会改用电脑内置摄像头。
3) 相机断开后重新连接时，也会按同一个 camera-id 重新识别。
4) GUI 的两个通道不能选择同一个物理摄像头。
5) 如果更换 USB 接口导致设备路径变化，请在对应标签页点击“更换绑定”并重新确认画面。

启动方式（Windows）：
python camera.py --timepoint-file camera_config.json

参数说明：
--list-cameras
  列出摄像头的名称、camera-id 和设备路径。

--camera-id
  WHEELTEC 摄像头的 USB VID:PID，例如 0c45:6366。

--camera-name
  可选名称校验，必须与 --list-cameras 显示的名称完全一致。

--camera-path
  可选设备路径校验。只有连接多个相同型号摄像头时才需要。

-i / --id / --camera-index
  已停用，避免 Windows 摄像头序号变化后误开内置摄像头。

--interval-seconds
  间隔触发秒数，默认 60。设置为 0 可关闭间隔触发，只保留时间点触发。

--timepoints
  固定时间点（可选），逗号分隔，按 HH:MM 格式，例如：
  --timepoints 08:00,12:30,18:00

--timepoint-file
  可选 JSON 配置文件。示例：
  {
    "interval_seconds": 60,
    "camera": {
      "id": "请替换为--list-cameras显示的VID:PID"
    },
    "timepoints": ["08:00", "12:00", "18:00"]
  }

--output-dir
  默认 output，按天自动创建子目录 output/YYYY-MM-DD

--format
  图片格式，支持 jpg/jpeg/png，默认 jpg

--log-file
  日志文件路径，默认 camera_timer.log

运行示例：
1) 只做固定间隔（默认 60 秒）：
python camera.py --camera-id 0c45:6366 --interval-seconds 60

2) 只做固定时间点：
python camera.py --camera-id 0c45:6366 --interval-seconds 0 --timepoints 12:00

3) 同时启用：
python camera.py --camera-id 0c45:6366 --interval-seconds 60 --timepoints 08:00,12:00,18:00

注意：以上 0c45:6366 仅为格式示例，必须替换成你的 WHEELTEC 摄像头实际显示的 camera-id。

触发规则：
1) 触发方式为“间隔 + 时间点”并集。
2) 时间点触发使用本地时间（24 小时制 HH:MM）。
3) 同一时刻双触发时会去重并记录来源为 both，避免重复拍一秒内重复写盘。
