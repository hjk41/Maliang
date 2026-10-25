# 第三方与素材

Maliang 自有源码、合成示例和所选图标按 MIT 分发。图标为本项目通过图像生成工具制作并选定；公开示例页面及文案为项目原创合成材料。

运行依赖保留各自许可证：PySide6_Essentials / Qt 使用其适用的 LGPL/GPL/商业许可，pyserial 使用 BSD 许可证，NumPy 使用 BSD 及其所附第三方许可。只安装应用所用的 Qt 基础模块，不要求 PySide6_Addons。开发工具 Pillow、PyInstaller 和 build 也保留原许可，PyInstaller 的软件分发例外不改变 Qt 的许可要求。

0.7 的共享接口使用 jsonschema（MIT）及其运行依赖。可选 `agent` extra 使用官方 Python MCP SDK（MIT）及其依赖；Windows 便携版包含该 extra。`tools/collect_licenses.py --agent` 按实际安装版本和平台条件递归收集运行依赖的元数据与许可全文，不能用本项目 MIT 替代这些许可。

Windows EXE 采用 onedir 方式保留动态链接库及依赖携带的许可元数据；发布包附带 THIRD_PARTY_LICENSES 目录与本项目 LICENSE。修改并分发依赖时，还须履行对应源码/修改提供义务。

`licenses/qt/` 保留 [Qt for Python v6.11.2 的许可证原文](https://github.com/pyside/pyside-setup/tree/v6.11.2/LICENSES)，`licenses/pyserial-LICENSE.txt` 来自 [pyserial v3.5](https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt)。Qt/PySide 的开源 LGPL 使用需要保留可替换的动态库；上游对应源码可从上述版本及 [Qt 源码仓库](https://code.qt.io/cgit/) 获取。本项目没有修改这些库。

内置九款字体及转换资源按各自的 OFL 1.1 / Arphic Public License 分发，独立于 MIT 代码许可。具体来源、派生改名和构建方式见[内置字体](builtin-fonts.md)。每款原署名、许可全文、来源与修改说明随 `pagewriter/resources/fonts/*-LICENSE.txt` 提供，EXE 分发包另收集到 `THIRD_PARTY_LICENSES/Fonts/`。布局代理 TTF 与相应轨迹数据遵循同一源字体许可。

仓库不包含厂商软件、固件、厂商字库、厂商字形子集或个人页面照片。用户导入的额外字体和照片由用户自行确认使用权。便携项目可能嵌入用过的字形及图像，分享自己的项目时需要检查其中的素材授权与隐私。

现有 `.pagewriter` 格式和内部模块名保留兼容，不表示与设备或字体厂商存在合作关系。
