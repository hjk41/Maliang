# 架构与开发接续

Maliang 是本地 Python / PySide6 应用，展示名称「马良」，兼容性模块名 `pagewriter`。

| 模块 | 职责 |
|---|---|
| `app.py` / `__main__.py` | 源码、安装包与 EXE 共用入口、图标与应用元数据 |
| `ui.py` / `inline_edit.py` | 页面场景、输入法、原位编辑、工具与阶段切换 |
| `core.py` | 毫米文档、富文本/分栏、保护区绕排、共享笔迹、预检和项目持久化 |
| `rectify.py` / `dewarp.py` | 照片四点透视、曲线引导与 2D 拉平 |
| `lineart.py` / `svgimport.py` | 图片骨架/填充线和 SVG 图形的笔迹转换 |
| `gfont.py` | 用户显式导入的 v5 ZIP 字形读取；不解释加密元数据 |
| `stroke_fonts.py` | 内置字库目录、SQLite 字形懒读、来源版本与 fallback、Qt 私有度量代理 |
| `protocol.py` / `controller.py` | 旧导入位置的兼容层；实现已迁入设备适配包 |
| `devices/ir.py` | 毫米 XY 和抬落笔的通用有序动作，不要求 Z 轴或 G-code |
| `devices/profiles.py` / `transport.py` | 不可变机型与能力、配置校验、传输所有权 |
| `devices/grbl_*` / `simulator.py` | Grbl 协议/控制器和独立 XY 舵机模拟器；ACK 与完成分开 |
| `document_service.py` / `rendering.py` | 原生 Qt 文档命令、原子批量编辑、版本/撤销、文件边界和图片预览 |
| `application.py` / `job_service.py` | 共享应用入口、会话、独立预检、持久作业意图、幂等与运行时授权 |
| `interfaces/` / `agent_ui.py` | 本机 HTTP、CLI、官方 SDK MCP、私有连接文件及桌面操作员授权 |
| `device_ui.py` | 连接到书写的向导、高级标定、用户确认与错误反馈 |
| `resources/` | 可随 wheel / EXE 分发的图标、原创合成示例和带许可的内置单线字体 |

页面单位是毫米，x 向右、y 向下。画布、碰撞预检、SVG 和真实发送共享笔迹路径。照片只作为参考，不进入输出。系统字体是轮廓；真实文字作业要求完整单线字形。

内置字体由构建工具预先生成，应用仅使用 SQLite/zlib 和 Qt，不依赖 Pillow、fontTools 或 OpenCV 进行运行时转换。字符路径按 Unicode scalar 懒读并冻结到项目；私有代理 TTF 只提供字符字宽和度量，使英文比例、UTF-16 光标命中、分栏和下划线与实际输出一致。代理字体不安装到系统；字库及代理的版本不一致时提示重新选择，避免静默换字。来源和构建流程见[内置字体](builtin-fonts.md)。

`.pagewriter` 是 JSON，保存嵌入底图、文字样式、布局、保护区和已用笔迹。未修改文字可用冻结路径保持跨机形状；编辑新字符可能需要原字库。导入项目不会恢复会话授权。新字段需兼容历史文件并验证有限值和尺寸边界。

串口只由控制器线程持有。UI 提交意图并显示快照，不直接写串口。不要让加载文件、连接设备或打开示例自动产生运动。发送前核对当前会话的映射、笔高度、工作边界和全路径净空。详见 [设备协议](device-protocol.md)。

应用服务运行在 Qt 所属线程，HTTP 工作线程只提交有期限的队列请求。GUI 的提交、原位编辑与 Agent 共用同一个 Project；代理写入进入相同撤销历史，expected_revision 防止覆盖新改动。布局缓存不计入编辑版本，也不在高频状态轮询中深拷贝字形数据。

服务从当前模型独立生成不可变作业，客户端没有 preflight_passed 或 authorize 布尔开关。作业意图先写 SQLite，再提交唯一控制器；相同 request_id 返回原作业，不在异常/重启后自动重放。来源不明的结束状态保留为 unknown，由操作员核实。物理授权只在内存中，绑定会话、范围、定位和期限；工作线程再次检查可撤销租约。暂停/停止的实际语义由驱动能力和完成证据明确返回。

接口和机型扩展方法分别见[Agent 接口](agent-interface.md)及[设备适配](device-adapters.md)。JSON 型号配置不会载入可执行插件或恢复标定。普通 CLI 使用同一服务；MCP 为可选安装项，便携双入口包包含该依赖。

当前验证最需要补齐的是：新版笔高度向导、实拍页面现场锚点与物理尺寸、倒置 180°书页、整页持续质量和中途暂停继续。已有离线几何及废纸小样不能替代这些项目。参见 [验证范围](verification.md)。
