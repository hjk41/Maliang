# 内置单线字体

马良 0.6 内置九款离线单线字库。它们在页面预览、碰撞预检、SVG 和设备作业中使用相同笔迹，不需要安装系统字体或厂商软件。

| 界面名称 | 字形来源 | 许可 |
|---|---|---|
| 马良·硬笔 | 猫啃硬笔楷书 | OFL 1.1 |
| 马良·清楷GB | 霞鹜文楷 GB | OFL 1.1 |
| 马良·圆笔 | 小赖 | OFL 1.1 |
| 马良·悠笔 | 悠哉 | OFL 1.1 |
| 马良·笔顺楷（备选） | Hanzi Writer / Make Me a Hanzi | Arphic Public License |
| EMS Readability | 原作者 SVG 单线轨迹 | OFL 1.1 |
| EMS Tech | 原作者 SVG 单线轨迹 | OFL 1.1 |
| EMS Neato | 原作者 SVG 单线轨迹 | OFL 1.1 |
| EMS Allure | 原作者 SVG 单线轨迹 | OFL 1.1 |

前四款是原字体的全字符映射转换版，采用马良衍生名称，来源名称仅作署名。特别是文楷 GB 和悠哉的保留名称不能直接用于转换后的字体主名称。Hanzi Writer 和 EMS 直接使用原有中心线，不再经过轮廓骨架化。用户已排除的字体和授权未核实的厂商字体不随应用分发。

## 使用

从页面上方的单线字体列表选择字体，可先选字体再添加文字，也可选中一段文字单独换字体。字号、下划线、双栏/三栏仍然适用。英文按每个字母的真实 advance 排版，`i` 与 `W` 不会占相同宽度。

同一文本可以混用多款字体。英文字体中的中文、笔顺楷中的英文，按已声明的内置回退顺序寻找真实笔迹；所有回退来源均属于以上已批准字库。各来源都缺字时会明确提示并阻止导出，不能用系统轮廓冒充可书写字形。

复制文本带入的零宽空格等不可见格式字符仅参与排版，不产生笔迹。软连字符（U+00AD）若在换行处变为可见连字符，当前版本会提示改为普通连字符或加宽文字框，避免漏写；不支持的变体选择符仍按缺字处理。

字库只按需读取当前字符；项目文件只保存用过的字形与资源版本，不嵌入整套字库或个人安装路径。保存后未编辑的笔迹保持精确；更新了字体资源后若继续编辑，按版本检查提示重新选择字体。外部 `.gfont` 仍可作为本地字体导入，其原有使用权和分享限制不变。

## 构建与复现

源码仓库、wheel 和 Windows EXE 都直接携带已生成资源；普通安装和运行不需要字体转换库或网络。

只有重新生成字体资源时，才需要额外构建环境：

```powershell
py -3.12 -m venv .venv-fontbuild
.venv-fontbuild\Scripts\python.exe -m pip install ".[fontbuild]"
.venv-fontbuild\Scripts\python.exe tools\build_builtin_fonts.py --workers 6
```

输入固定版本、源文件 SHA256 和许可 SHA256 在 `tools/font_sources.json`。首次构建下载到被 Git 忽略的 `.font-build/`，之后复用缓存。范围为源字体 Unicode 字符映射中的完整字符集，不宣称支持没有独立字符映射的 OpenType 替换字形或字形变体。中文转换每 250 字提交一次缓存，可运行同一命令续接。`--only` 可只重建指定 ID；改变源版本或算法参数应使用新的 `--cache`，避免混用结果。`--limit` 用于开发批次，不会发布未完成字库。

中文输入逐字以 512 px/em 渲染，经 scikit-image Zhang 细化、连通骨架内不重复经过边的路径整理和 OpenCV 0.85 px 简化。全量构建采用加速实现，像素位置和路径数可能与早期预览脚本有轻微差异；已做同文视觉对照。原字宽、字符映射与基线位置均保留，不跨空白添加连笔。单点笔画以极短点线保留，空白字保留字宽，无可绘轮廓的非空白映射不宣称可书写覆盖。

四款 EMS 以真实小写 `a` 的字身高度统一到 0.5em，所有坐标和 advance 使用同一比例缩放，保留每款的字形比例与字距。花体的大写花饰仍会自然高于普通印刷体，不使用不准确的源字体 cap-height 元数据控制行距。

资源目录 `pagewriter/resources/fonts/` 含逐字压缩路径数据库、目录与来源元数据、Qt 布局度量代理字体、每款对应的许可全文。布局代理字体仅提供 advance/cmap/行度量，不是实际绘制的轮廓，也不安装到系统。其字符表与实际笔迹覆盖对应，以便预览、光标命中和导出保持一致。

## 质量边界

完整转换不等于人工校对每个字。自动骨架仍可能产生短支、局部折返和笔顺差异；悠哉等原字体也有地区字形差异。需要精确字形时应先在页面上放大检查，再做废纸试写。字库名称和路径数量不能替代实际耗时测量。

新内置字体尚未完成整页实机书写验收。历史厂商字体的废纸试写不能作为这些新字库的硬件验收结果。

## 来源与分发

源项目：[猫啃硬笔楷书](https://github.com/maoken-fonts/MaokenYingBiKaiShuJ)、[文楷 GB](https://github.com/lxgw/LxgwWenKaiGB)、[小赖](https://github.com/lxgw/kose-font)、[悠哉](https://github.com/lxgw/yozai-font)、[Hanzi Writer Data](https://github.com/chanind/hanzi-writer-data)、[EMS SVG Fonts](https://gitlab.com/oskay/svg-fonts)。

字体及衍生资源保留对应 OFL/APL，独立于马良程序的 MIT 许可。每款 `*-LICENSE.txt` 保存原声明、署名、来源及转换说明；Windows 分发包的 `THIRD_PARTY_LICENSES/Fonts/` 另有一份。改动并再分发字体时，继续遵守各自的署名、保留名称和同许可分发要求。

Hanzi Writer 的 `ARPHICPL.TXT` 是 Big5 原文，另以原字节完整附带；许可查看入口展示可读的 UTF-8 转码副本，不修改原文条款。
