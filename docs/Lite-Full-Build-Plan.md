# 单主线打包 Lite / Full：按目录拆分

> 目录拆分已在集成分支实现，合入 `main` 和发布仍须通过下文的双平台验收。构建两版无需切换分支或给 Python 文件加版本后缀。

## 一、怎么合并：保留历史，不覆盖 `main`

**不复制两套完整仓库，也不归档 `main` 后用新目录覆盖它。** 从现有 `main` 创建临时集成分支，在该分支上合入 `wipOCR`，再用普通 PR 合并回 `main`。独立工作树只是可选的隔离目录，与现有仓库共用 Git 历史。

1. **确认两条构建基线。** 记录当前 `main`（Lite）与 `wipOCR`（Full）的提交。若想长期保留合并前的构建基线，可以另打 tag；不必为此保留分支。
2. **在临时集成分支开始合并，先不提交。** 例如，从仓库根目录执行以下命令可在独立工作树中操作，不影响现有两个检出目录：

   ```bash
   git worktree add -b integrate/lite-full ../WWEcho-integrate main
   cd ../WWEcho-integrate
   git merge --no-ff --no-commit wipOCR
   ```

3. **按原始分支取文件，而不是从自动合并后的同名文件取。** 将 `main` 的界面和打包文件放进 `lite/`，将 `wipOCR` 的对应文件和 OCR 模块放进 `full/`。两边都迁到新路径后，再移除被替代的旧路径并标记冲突已解决。Git 不报冲突的脚本和依赖也要按这个规则拆分：自动合并后的文件可能只是 Full 的版本。
4. **双版通过验收后，再合入 `main`。** 在集成分支记录完整合并提交，从同一提交打出两版并启动验证；验收后提交 PR，使用保留历史的 merge commit，而不是覆盖目录、强推或 squash。确认 Full 的基线提交已成为 `main` 的祖先，从同一个发布 tag 构建两版。发布成功并确认没有遗漏改动后，再退役 `wipOCR` 和临时集成分支。`main` 始终保留。

## 二、目标目录与文件来源

```text
WWEcho/
├── src/                     # 共用评分、开孔、频整器、权重管理
├── config/                  # 共用配置
├── icon.png
├── weight-presets.json      # 共用构建模板
├── requirements.txt        # 共用基础依赖
├── tests/                   # 共用测试
├── lite/                    # 来自 main
│   ├── echo_gui.py
│   ├── WWEcho.spec
│   ├── requirements-build.txt
│   ├── build-macos.sh
│   ├── build-windows.bat
│   └── tests/
└── full/                    # 来自 wipOCR
    ├── echo_gui.py
    ├── echo_capture.py
    ├── echo_ocr.py
    ├── echo_ocr_gui.py
    ├── WWEcho.spec
    ├── requirements-build.txt
    ├── requirements-ocr.txt
    ├── build-macos.sh
    ├── build-windows.bat
    ├── licenses/
    └── tests/
```

- **Lite** 保留 `main` 的手动录入、透明度、置顶和窗口设置逻辑；不导入 `full` 的模块。
- **Full** 保留 `wipOCR` 的界面、截图识别、回填和关闭流程。把原 `src.echo_ocr*`、`src.echo_capture` 的引用改为 `full` 下的模块；共用算法继续从 `src` 导入。首次合并不要求统一两份界面或迁移设置格式。
- 版别专用界面测试随入口移动，保留同名 `test_echo_gui.py`；修改测试里的导入、mock 路径及 Full 的截图样本路径。根目录中引用旧界面的共用测试也要改为调用新版模块。
- 从仓库根目录可用 `python -m lite.echo_gui`、`python -m full.echo_gui` 启动源码，分别使用对应环境。保留根目录 `run.sh`、`run.bat` 作为 Lite 的兼容入口时，要改掉对旧 `src.echo_gui` 的引用。README 改为说明两个版本；`.gitignore` 保留两版设置文件，并忽略各版的虚拟环境、构建目录和产物。

## 三、打包脚本怎么改

用**脚本所在目录**选择版本，不新增 `lite/full` 参数。每个脚本从自身位置解析版本目录和仓库根目录，不依赖调用者的当前目录。

| 项目 | `lite/` | `full/` |
|---|---|---|
| PyInstaller 入口 | `lite/echo_gui.py` | `full/echo_gui.py` |
| 构建依赖 | `requirements-build.txt` 引用 `../requirements.txt`，另装 PyInstaller、Pillow | `requirements-build.txt` 引用同目录的 `requirements-ocr.txt`（后者引用 `../requirements.txt`），另装 PyInstaller、Pillow |
| 资源 | 图标、配置、初始权重模板；不收集 OCR 模块、运行库或模型 | 共有资源加 OCR 配置、模型和许可；缺必需资源时构建失败 |
| 环境 | `lite/.venv/`；保留原有 Python ≥3.9 检查 | `full/.venv/`；保留 Python 3.9～3.12、macOS arm64 / Windows x64 限制 |
| 工作和输出目录 | `lite/build/`、`lite/dist/` | `full/build/`、`full/dist/`；Windows 再按 `onefile`、`onedir` 分开 |

两份 spec 都需更新路径：`SPECPATH` 现在是版本目录，项目根目录是其父级。`Analysis` 指向该版入口，并将项目根目录加入模块搜索路径；共用资源仍从根目录读取，生成的图标和 PyInstaller 中间文件写入各版自己的 `build/`。Full spec 还要改 OCR 模块导入和许可文件路径，保留现有的打包内资源布局，确保运行时能找到模型。

两版文件名保留原样，构建目录彼此独立。打包使用版本库中干净的 `weight-presets.json` 作初始模板，不收集运行时的设置文件。运行时权重文件和设置保存在应用旁，Lite 使用 `window-settings.json`，Full 使用 `ocr-settings.json`。发布时给压缩包名加 `Lite`／`Full`、平台和产物形式，建议解压到不同目录，避免同名应用覆盖或共用权重文件。

### 合并后从仓库根目录构建

macOS：

```bash
./lite/build-macos.sh
./full/build-macos.sh
```

Windows（命令提示符）：

```bat
lite\build-windows.bat
full\build-windows.bat all
```

Lite 保留单文件 EXE；Full 默认 `all`，也可单独传 `onefile` 或 `onedir`。macOS 两版分别在各自 `dist/` 下生成 `.app`，Windows 的目录版须完整分发。

## 四、合入 `main` 前的验收

1. 在 Windows、macOS 各自的原生环境，从**同一提交、同一检出目录**构建 Lite 和 Full。Windows 验收 Full 的 `onefile` 和 `onedir`；两版输出互不覆盖，且不依赖旧工作树的虚拟环境或产物。
2. 启动 Lite，验证无截图入口、无 OCR 依赖和模型；手动录入、评分、透明度、置顶和设置保存与原 `main` 一致。检查打包产物，而不是只看界面。
3. 启动 Full，验证手动功能不变，OCR 模型和许可齐全，实机截图、回填、识别中关闭与原 `wipOCR` 一致。两平台分别验证窗口行为和截图权限。
4. 检查新目录下的源代码启动、权重模板和旧设置读取；测试分别覆盖共用核心、Lite 界面和 Full OCR。验收通过才合入 `main`，不给任何一个只能打 Full 的中间状态打发布 tag。
