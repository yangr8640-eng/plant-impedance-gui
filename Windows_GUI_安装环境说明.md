# 实时电阻曲线 GUI：Windows 安装环境说明

本文档用于说明把现有电脑上的“实时电阻曲线 GUI”复制到另一台 Windows 电脑时，需要安装和检查哪些环境。  
如果复制后 GUI 能打开但不能正常显示曲线，通常不是界面文件本身丢失，而是 Python 运行环境、串口依赖、USB 转串口驱动、串口参数或硬件输出格式没有配置好。

## 一、需要复制哪些文件

建议直接复制整个项目文件夹，不要只复制桌面快捷方式。

Windows 运行 GUI 至少需要这些文件放在同一个文件夹里：

- `ResistanceGUI_windows.py`
- `启动电阻曲线GUI_Windows.bat`
- 本说明文档或 `README_Windows_GUI.txt`

注意：项目里的 `ResistanceGUI` 是 macOS 可执行文件，不是 Windows 程序，复制到 Windows 上不能直接当作 Windows GUI 使用。

## 二、Windows 电脑必须安装的内容

### 1. Python 3

必须安装 Python 3.9 或更高版本，推荐使用 python.org 的 Windows 安装包。

安装时请务必勾选：

```text
Add python.exe to PATH
```

安装完成后，在 Windows 的命令提示符或 PowerShell 中检查：

```bat
py -3 --version
```

如果提示找不到 `py`，再检查：

```bat
python --version
```

只要能看到 Python 3 的版本号即可。

### 2. pyserial 串口库

这个 GUI 读取硬件串口数据需要 `pyserial`。  
双击 `启动电阻曲线GUI_Windows.bat` 时会自动检查并安装 `pyserial`。

如果自动安装失败，可手动运行：

```bat
py -3 -m pip install pyserial
```

如果电脑上没有 `py` 命令，可改用：

```bat
python -m pip install pyserial
```

安装后可检查：

```bat
py -3 -c "import serial; print(serial.__version__)"
```

能输出版本号，说明 `pyserial` 已安装成功。

### 3. tkinter 图形界面组件

GUI 使用 Python 自带的 `tkinter` 绘制窗口和曲线。  
从 python.org 安装的标准 Windows Python 通常已经包含 `tkinter`，不需要额外安装 `matplotlib`、`numpy` 或浏览器插件。

如果双击后窗口完全打不开，并提示类似 `No module named tkinter`，说明当前 Python 不是完整安装版。请卸载后重新安装 python.org 的 Windows 版 Python，并保留默认组件。

### 4. USB 转串口驱动

另一台 Windows 电脑必须能识别硬件串口，否则 GUI 无法收到数据。  
请根据硬件使用的 USB 转串口芯片安装对应驱动，常见类型包括：

- CH340 / CH341
- CP210x
- FTDI
- STM32 Virtual COM Port
- Arduino 或开发板自带的串口驱动

安装或插入硬件后，在 Windows 设备管理器中检查：

```text
端口 (COM 和 LPT) -> USB-SERIAL CH340 (COM3)
```

只要能看到类似 `COM3`、`COM4`、`COM5` 的端口，GUI 才能选择并连接。

## 三、启动方法

1. 把整个项目文件夹复制到 Windows 电脑。
2. 双击 `启动电阻曲线GUI_Windows.bat`。
3. 如果第一次运行，它会自动安装 `pyserial`。
4. 打开 GUI 后点击“刷新串口”。
5. 选择硬件对应的 `COM3`、`COM4`、`COM5` 等串口。
6. 选择和 SSCOM 或硬件程序一致的波特率，常用为 `115200`。
7. 填写“测试时长(h)”，例如 `24` 表示 24 小时，`0.5` 表示 30 分钟。
8. 填写“横轴范围(h)”，例如 `6` 表示曲线显示最近 6 小时，`24` 表示显示完整 24 小时。
9. 点击“连接”。

也可以手动启动：

```bat
cd /d GUI所在文件夹
py -3 ResistanceGUI_windows.py
```

## 四、硬件输出数据格式要求

GUI 会从串口收到的每一行文本中提取电阻值。  
建议硬件每次发送一行，并以 `\n` 或 `\r\n` 结尾。

推荐格式：

```text
R=1023.5 ohm
R=1.024 kΩ
resistance: 998.2 Ω
电阻=1020 欧姆
```

支持的单位：

- `Ω` / `ohm` / `欧` / `欧姆`
- `kΩ` / `kohm` / `千欧`
- `MΩ` / `Mohm` / `兆欧`
- `mΩ` / `mohm` / `毫欧`

如果没有单位，GUI 默认按欧姆处理。

## 五、复制后曲线不显示的排查顺序

### 1. 先点“模拟数据”

如果模拟数据能显示曲线，说明 GUI 的绘图功能正常，问题大概率在串口、驱动、波特率或硬件输出格式。  
如果模拟数据也没有曲线，请重新安装完整 Python，并确认不是误运行了错误文件。

### 2. 看串口下拉框

如果只显示“未安装 pyserial”，说明没有安装串口库。运行：

```bat
py -3 -m pip install pyserial
```

如果显示“未发现串口”，请检查：

- USB 线是否支持数据传输，不是只充电线
- 硬件是否供电
- USB 转串口驱动是否安装
- 设备管理器里是否出现 `COM` 端口

### 3. 确认串口没有被其他软件占用

同一个串口通常只能被一个软件打开。  
如果 SSCOM、Arduino Serial Monitor 或其他串口工具正在连接同一个 `COM` 口，请先关闭它们的串口连接，再回到 GUI 里点击“连接”。

### 4. 确认波特率一致

GUI 里的波特率必须和硬件程序一致，也要和 SSCOM 能正常显示数据时的波特率一致。  
例如硬件是 `115200`，GUI 也必须选择 `115200`。

波特率不一致时，可能会出现：

- 没有有效数据
- 接收日志是乱码
- 收到字节增加但解析成功一直是 0

### 5. 看“收到字节”和“解析成功”

GUI 中有两个重要状态：

- `收到字节`
- `解析成功`

判断方法：

- `收到字节` 一直是 0：GUI 没收到硬件数据。优先检查串口号、驱动、USB 线、硬件供电、硬件是否主动发送。
- `收到字节` 增加，但 `解析成功` 是 0：GUI 收到了文本，但没有识别出电阻值。请把接收日志中的原始数据格式发给开发者。
- `解析成功` 增加但看不到曲线：点击“重置缩放”，并检查测试时长、Y 格差设置是否合理。

### 6. 确认测试时长没有结束

GUI 的计时从第一条有效电阻数据开始。  
如果测试时长设置为 `24` 小时，到达 24 小时后 GUI 会自动停止继续追加曲线。

## 六、不需要安装的内容

Windows GUI 当前不依赖这些库：

- `matplotlib`
- `numpy`
- `pandas`
- Chrome / Edge 浏览器
- Web Serial API

这些是网页版本或其他数据处理场景可能用到的内容，不是 Windows 本地 GUI 显示曲线的必要条件。

## 七、给新电脑的最小安装清单

交给别人安装时，可以按下面清单确认：

```text
1. Windows 10/11
2. Python 3.9 或更高版本
3. 安装 Python 时勾选 Add python.exe to PATH
4. pyserial 串口库
5. 对应硬件的 USB 转串口驱动
6. 设备管理器中能看到 COM 端口
7. GUI 串口号和波特率与硬件一致
8. 硬件每条数据按 R=1023.5 ohm 这类格式发送，并带换行
```

## 八、建议的交付方式

如果经常要给别人使用，建议把 Windows 版单独整理成一个文件夹，例如：

```text
实时电阻曲线GUI_Windows/
  ResistanceGUI_windows.py
  启动电阻曲线GUI_Windows.bat
  Windows_GUI_安装环境说明.md
  README_Windows_GUI.txt
```

对方只需要先按本文档安装 Python、pyserial 和 USB 转串口驱动，再双击 `启动电阻曲线GUI_Windows.bat` 即可。
