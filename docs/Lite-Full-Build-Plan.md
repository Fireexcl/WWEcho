# Lite / Full 源码整理与打包方案

> 这是后续实施方案，不代表双版本构建已经完成。当前 `main` 与 `wipOCR` 的迁移改动应分别保存后再合并；本文中的构建命令是拟议接口。

## 目标与现状

- Lite 保留手动录入、评分、开孔建议、频整器计算、权重管理，以及透明度和置顶。
- Full 在相同功能上增加截图识别；两版共享业务逻辑，不长期维护两套源码。
- `wipOCR` 的截图、OCR 解析和界面分别在 `src/echo_capture.py`、`src/echo_ocr.py`、`src/echo_ocr_gui.py`；`src/echo_gui.py` 中还有控制器接入和回填。
- `main` 的窗口设置保存为 `window-settings.json`；现有 Full 的透明度、置顶和识别区域保存在 `ocr-settings.json`。合并时不能丢失已有设置或识别区域。

## 一、源码整理

1. 以 `main` 和 `wipOCR` 合并后的源码为基础，解决 `echo_gui.py`、测试、README 等冲突，保留两边有效修改。不要复制整套目录，也不要用某一边直接覆盖另一边。
2. 评分、策略、频整器、权重及手动录入控件保持共同实现。Lite 沿用手动录入布局；Full 保留手动和截图识别标签。提取共同的控件创建代码，避免两份手动录入逻辑。
3. 透明度滑块和置顶按钮均由主界面创建，两个版本都可用。把窗口外观状态、设置保存及窗口状态恢复从 OCR 控制器独立出来；Full 截图前后的最小化和恢复仍须保持原行为。`WheelSafeComboBox` 等共用控件不能从 OCR 界面模块导入。
4. OCR 控制器、截图界面、识别回填和任务关闭流程只在 Full 启用。Lite 不初始化 OCR、不显示识别入口，也不依赖 OCR 模块。Full 在识别进行中关闭仍须安全等待后台任务；手动输入变更继续使旧预览失效。
5. 建议增加 Lite、Full 两个小入口，向共用窗口显式传入版本或可选功能；打包产物的版本由入口固定，不能仅依赖构建时环境变量。源码启动默认保持 Full 当前行为：缺少可选 OCR 引擎时仍能手动使用，识别时显示明确错误。
6. 将现有 OCR 类型的运行时导入放到 Full 路径，避免 Lite 因缺少模块而启动失败。只用于类型检查的引用可以使用 `TYPE_CHECKING`；OCR 回填与界面之间保留明确接口。

### 设置兼容

- 合并后以 `window-settings.json` 管理透明度和置顶，`ocr-settings.json` 管理识别区域。都位于应用旁；源码版位于项目根目录。
- 老 Full 用户首次运行新版本时，若 `window-settings.json` 不存在，从有效的旧 `ocr-settings.json` 读取透明度和置顶并迁移；保留原文件和识别区域。已有 `window-settings.json` 优先，不能被旧文件覆盖。
- 旧 OCR 设置的解析应兼容其现有 schema；迁移失败、文件损坏或写入失败时展示错误，不覆盖原文件，不静默宣称成功。设置写入沿用原子写入，并在关闭时处理尚未保存的滑块修改。
- 若 Lite 与 Full 放在同一目录，共用窗口设置和权重方案；不同目录则各自独立。避免把可写设置或 OCR 模型放到只读打包资源目录。

## 二、打包脚本

### 依赖与环境

- `requirements-build.txt` 改为基础运行依赖、PyInstaller 和 Pillow；新增 Full 构建依赖文件，引用基础构建依赖和 `requirements-ocr.txt`。
- 分别使用 `.venv-build-lite` 和 `.venv-build-full`，防止 Full 的依赖被 Lite 意外打包；这些环境应加入忽略规则。脚本检查解释器版本、平台与架构，不满足时明确退出。
- Lite 继续支持项目原有的 Python 范围，以实际基础依赖兼容性验证；Full 保留当前 Python 3.9～3.12、Windows 11 x64 和 macOS Apple Silicon 约束。

### PyInstaller

- 修改 `WWEcho.spec`，校验 `lite` / `full` 参数并选择固定版本入口；版本与 Windows 的 `onefile` / `onedir` 是两个独立维度。
- 两版均收集图标、配置、权重模板和各自依赖所需许可。Full 继续收集 OCR 引擎、模型、配置和第三方许可，并在资源缺失时失败。
- Lite 不收集 OCR 模块、引擎、ONNX Runtime、OpenCV 或模型。PyInstaller 会静态分析条件导入，因此 Lite 需显式排除 OCR 模块及其专属依赖，并验证启动；不能只隐藏入口。注意 NumPy 是基础依赖，不应排除。
- 保持 macOS `.app` 和 Windows 单文件、目录版各自的现有打包逻辑；不要覆盖目标平台的版本或许可要求。

### 命令与输出

建议要求显式版本，避免 `main` 和 `wipOCR` 过去的默认构建含义冲突：

```bash
./build-macos.sh lite
./build-macos.sh full
```

```bat
build-windows.bat lite all
build-windows.bat full onefile
build-windows.bat full onedir
```

Windows 第二参数沿用 `all`、`onefile`、`onedir`，可默认 `all`；缺失版本或无效参数应显示用法并返回非零状态。脚本应根据版本选择依赖、入口和环境，校验方案库，并将工作目录与产物目录分别隔离：

```text
dist/lite/macos/...
dist/full/macos/...
dist/lite/windows-onefile/...
dist/lite/windows-onedir/...
dist/full/windows-onefile/...
dist/full/windows-onedir/...
build/lite/...
build/full/...
```

更新 README 中的安装、源码运行、构建命令、产物位置、设置路径和平台限制；注明这会改变以前无参数构建的用法。

## 三、实施顺序与验收

1. 分别保存两个分支当前改动，合并并解决冲突；先验证两版共用的透明度、置顶与手动功能。
2. 抽取 OCR 边界和窗口设置迁移；覆盖无设置、有效旧设置、两份设置同时存在、损坏文件和不可写目录，确认识别区域未丢失。
3. 调整依赖、spec 和脚本，验证错误参数、环境选择以及不同版本与形式之间不覆盖。
4. 运行相关界面、OCR 和业务回归测试；验证 Lite 在未安装 OCR 依赖时可启动、无识别入口，Full 的识别、预览回填、取消和后台关闭正常。
5. 在 Windows 和 macOS 原生环境分别构建并启动 Lite / Full；Windows 分别验收 onefile 和 onedir。检查 Lite 产物无 OCR 库及模型，Full 的模型和许可齐全；测试设置与权重在应用旁持久化。离屏测试不能替代两平台实机的置顶、透明度和截图权限验收。
