# 参与开发

支持环境为 Windows x64 / Python 3.12、3.13。按 README 建立虚拟环境后，运行 `python -m pip install -e ".[dev,agent]"`。

提交前运行 `python tools/check_repository.py` 和 `python tools/run_tests.py`。完整验收要求零跳过；需要聚焦单个测试时仍可使用 unittest。改变安装、资源或入口时，还需构建 wheel、在仓库外安装并执行 `python -m pagewriter --smoke`；改变打包配置时也需检查 EXE 启动。CI 使用 Windows 原生 Qt，不要强制 offscreen 插件。

测试必须使用假传输、仓库内的公开资源或源码生成的原创几何。不得读取开发者安装目录、私人照片目录或研究证据目录，也不得因这类素材缺失而跳过。缺少随仓库提供的字体等必需资源应直接失败；发布审计会检查外部素材路径。不要在 CI 中连接真实串口；连接和运动属于显式用户操作。涉及坐标、净空、队列、完成判定或停止行为时，补充能识别回归的测试，并在 PR 中区分自动测试与实际设备观察。

保留 `.pagewriter` 文件兼容性。模块名 `pagewriter` 是现有实现名；展示名称统一为「马良 · Maliang」。版本号以 `pagewriter/__init__.py` 为准，更新时同步 CHANGELOG。

请通过分支和 PR 提交，描述具体行为、验证方法与剩余限制。不要上传厂商字库、来自字库的字形缓存、个人书页照片、真实串口运行日志、凭据或本机快捷方式。公开示例应使用合成页面和原创几何。

## 发布

在 main 上确认 CI 通过并更新版本及 CHANGELOG，然后创建与代码版本一致的 `vX.Y.Z` 标签并推送。Release 工作流会重新测试、构建和检查 wheel/sdist/Windows ZIP，执行 EXE 冒烟，最后生成 GitHub Release 及 SHA256SUMS。实验版会标为 prerelease；当前流程不发布 PyPI。
