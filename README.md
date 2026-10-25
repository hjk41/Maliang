<p align="center"><img src="pagewriter/resources/maliang.png" width="112" alt="Maliang icon"></p>
<h1 align="center">马良 · Maliang</h1>
<p align="center">Your words, on paper. · 让你的文字，落在纸上。</p>

[![CI](https://github.com/hjk41/Maliang/actions/workflows/ci.yml/badge.svg)](https://github.com/hjk41/Maliang/actions/workflows/ci.yml)
[![Security](https://github.com/hjk41/Maliang/actions/workflows/security.yml/badge.svg)](https://github.com/hjk41/Maliang/actions/workflows/security.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

马良是一款 Windows 本地写字机排版与直连工具。在书页照片上直接编辑批注，指定哪里写、哪里避让，再由 USB 写字机落到纸上。无需账户或云服务。

## 能做什么

- **所见即所得编辑**：直接在页面输入，按字设置字体、字号和下划线；支持拖动、缩放文字框、旋转、撤销和中文输入法。
- **书页照片参考**：EXIF 转正、90°旋转、四角透视校正、曲线辅助拉平，以及点击放大镜。
- **灵活排版**：单栏、双栏、三栏、栏间分割线，文字自动避让保护区。
- **图画与线稿**：图片骨架描线、灰度排线填充、SVG 图形导入、局部擦除。
- **USB 直连**：连接、页面方向点动、手动升降笔、参考点对准、三点标定、笔高度校准、小方框试写、暂停和停止。
- **便携项目**：`.pagewriter` 保存底图、样式及使用过的笔迹；导出毫米 SVG。底图不进入书写输出。
- **九款离线内置单线字体**：四款中文转换字库、Hanzi Writer 笔顺楷备选和四款 EMS 英文字体，无需安装厂商软件。支持中英混排和局部换字体。

目前为 **0.6.0 实验版**，主要支持 Windows。已在颂博士 2024 款 / BtWriter 控制器上做过废纸方框和文字试写。新版向导、曲面处理与真实书页定位尚需更多实机验收；其他 Grbl 设备没有完成适配验证。详细边界见 [验证说明](docs/verification.md)。

## 安装与启动

需要 **64 位 Windows、Python 3.12 或 3.13**。在仓库目录执行：

```powershell
git clone https://github.com/hjk41/Maliang.git
cd Maliang
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install .
.venv\Scripts\maliang.exe
```

也可以双击 `start_maliang.cmd`，或用 `.venv\Scripts\python.exe main.py` 启动源码。程序从空白页面开始；「更多 → 打开分栏示例」提供离线排版示例。

**已内置九款可离线书写的单线字体。** 中文来源为猫啃硬笔楷书、霞鹜文楷 GB、小赖、悠哉，另有 Hanzi Writer 备选；英文为 EMS Readability / Tech / Neato / Allure。转换字体使用马良衍生名称，原作者和字体名保留在来源说明中。完整字符覆盖、许可与构建方法见[内置字体](docs/builtin-fonts.md)。系统字体仍可用于轮廓预览，也可导入自己有权使用的单线 `.gfont`；已有厂商字体仅从本机读取。

Windows 可能需要设备厂商提供的 CH340 USB 串口驱动。先断开其他程序对串口的占用，再在马良中连接。连接不会自动运动或回零。

## 使用顺序

1. **照片**：量好纸页尺寸，导入、转正和校正照片，圈出已有内容与实体障碍。
2. **批注**：在页面上编辑文字、分栏、下划线与图画，处理所有预检冲突。
3. **书写**：按向导连接、确认书本摆放、对准笔尖、校准高度，再在废纸/空白处试写小方框，核对后开始书写。

手推笔头后控制器不能感知真实位移，必须点击「已经手推笔尖」并重新对准。项目文件不会恢复现场运动授权。书倒放 180°时选择实体倒置预设，编辑画面仍保持正常阅读方向。

更完整的操作和停止行为见 [使用指南](docs/usage.md)，实现与协议见 [架构说明](docs/architecture.md) 和 [设备协议](docs/device-protocol.md)。

## 开发、测试与打包

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe tools\check_repository.py
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m build
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean maliang.spec
```

Windows 测试使用原生 Qt，**不要设置 `QT_QPA_PLATFORM=offscreen`**。自动测试用假串口，不驱动真实设备；本机厂商字体/个人照片的可选测试会在文件缺失时跳过。`--smoke` 会打开窗口并在约 0.8 秒后退出。

EXE 输出在 `dist/Maliang/Maliang.exe`；分发时需要整个 `Maliang` 文件夹。也可运行 `build_exe.cmd`。CI 测试 Python 3.12/3.13、构建并实际安装 wheel、打包 EXE、检查资源和执行启动冒烟测试。主分支 CI 的 EXE 在 Actions artifacts 下载；版本标签 `v*` 可触发独立的测试、打包和 GitHub Release 流程。

源码模块暂保留 `pagewriter` 名称，项目格式仍为 `.pagewriter`，以兼容现有文件。贡献方法见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

项目源码、原创示例和图标采用 [MIT](LICENSE)。第三方运行库、系统字体与设备厂商字体各自适用其原许可证，见 [第三方说明](docs/third-party.md)。
