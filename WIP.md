# 项目文件结构

```text
WWEcho/
├── README.md                     # 功能介绍、安装启动与使用说明
├── WIP.md                        # U R Here --> 项目目录结构
├── requirements.txt              # 共用运行依赖
├── bin/                          # Lite / Full 的 macOS、Linux 和 Windows 启动脚本
│   ├── run-lite.sh
│   ├── run-lite.bat
│   ├── run-full.sh
│   └── run-full.bat
├── .gitignore                    # Git 忽略规则
├── weight-presets.json           # 权重方案库，运行时创建在EXE(win11)或APP(mac) 同目录
├── config/
│   ├── activation-costs.json     ## 培养成本：打孔消耗、弃置回收与升级成本
│   ├── echo-stats.json           ## 基本参数：声骸词条定义、主词条满级值、副词条档位与概率
│   └── score-weights.json        ## 评分权重：主/副词条默认评分权重（占位值，默认为爱弥斯）
├── docs/
│   ├── Algorithm-Tuning.md      ## 调频/开孔算法
│   ├── General-Definition.md    ## 总体定义、术语、调谐规则与概率数据
│   └── Algorithm-Reroll.md      ## 频整器概率、洗炼/洗词条算法和收益
├── implementation/               ## 开发和实现文档
│   ├── Lite-Full-Build-Plan.md
│   ├── OCR-Implementation-Plan.md
│   ├── OCR-Plan.md
│   ├── desktop-app-plan.md     ## 桌面程序技术方案、功能规划与实施顺序
│   └── old-readme.md             # 旧版详细项目说明与算法口径
├── src/
│   ├── lite/                     # Lite 界面和构建配置
│   │   ├── echo_gui.py             # PySide6 图形界面：评分与后台策略分析
│   │   ├── WWEcho.spec
│   │   ├── requirements-build.txt
│   │   ├── build-macos.sh          # macOS .app 构建脚本
│   │   └── build-windows.bat       # Windows EXE 构建脚本     
│   ├── full/                     # Full 界面、OCR 和构建配置
│   │   ├── echo_gui.py             # PySide6 图形界面：评分与后台策略分析
│   │   ├── echo_capture.py
│   │   ├── echo_ocr.py
│   │   ├── echo_ocr_gui.py
│   │   ├── WWEcho.spec
│   │   ├── requirements-build.txt
│   │   ├── requirements-ocr.txt
│   │   ├── build-macos.sh          # macOS .app 构建脚本
│   │   ├── build-windows.bat       # Windows EXE 构建脚本
│   │   └── licenses/
│   ├── echo_costs.py             ## 轻量成本配置：GUI 与策略共用的默认值和校验
│   ├── echo_weight_presets.py    ## 权重方案：UUID、整库导出、按ID合并及原子保存
│   ├── echo_score.py             ## 评分核心与输入校验
│   ├── echo_strategy.py          ## 激活策略：概率上下界与材料权重累计成本最优停止
│   ├── echo_reconstruction.py    ## 频整器：离散单次分析与动态预算入口
│   └── echo_reconstruction_budget.py ## 动态换锁：条件积分、花费拆分与停手决策
└── tests/
    ├── __init__.py                # 统一测试发现
    ├── lite/                     # Lite 界面测试
    │   ├── __init__.py
    │   └── test_echo_gui.py
    ├── full/                     # Full 界面、截图和 OCR 测试
    │   ├── __init__.py
    │   ├── sample.png
    │   ├── test_echo_capture.py
    │   ├── test_echo_gui.py
    │   ├── test_echo_ocr.py
    │   └── test_echo_ocr_gui.py
    ├── fixtures/
    │   └── echo.json             # 评分核心测试数据
    ├── test_echo_score.py        # 评分、配置校验及非法输入测试
    ├── test_launch_scripts.py    # macOS/Linux 启动脚本的环境和依赖检查
    ├── test_echo_weight_presets.py # 权重方案持久化、UUID/合并导出、兼容及失败保护
    ├── test_echo_strategy.py     # 激活概率、累计成本策略、解析验证及边界测试
    ├── test_echo_reconstruction.py # 频整器概率、严格升分、预算及边际测试
    ├── test_echo_dynamic_budget.py # 动态换锁与花费拆分的独立有理数穷举验证
    └── test_echo_reconstruction_gui.py # 区域4锁定按钮、表格、缓存及异步测试
```
