# 项目文件结构

```text
WWEcho/
├── README.md                     # 项目说明、评分规则、使用方法与测试说明
├── WIP.md                        # 当前文件：项目目录结构
├── requirements.txt              # GUI 依赖：PySide6-Essentials
├── run.sh                        # GUI 启动脚本：使用项目虚拟环境运行窗口
├── .gitignore                    # Git 忽略规则（当前包含 .DS_Store）
├── .vscode/
│   └── tasks.json                # VS Code 任务：启动声骸评分 GUI
├── config/
│   ├── activation-costs.json     # 激活成本参数（占位数据，非实际游戏消耗）
│   ├── echo-stats.json           # 基本参数：声骸词条定义、主词条满级值、副词条档位与概率
│   └── score-weights.json        # 评分权重：主/副词条默认评分权重（占位值，可自定义）
├── docs/
│   ├── activation-recommendation-algorithms.md # 激活建议算法设计；部分已有原型实现
│   ├── desktop-app-plan.md       # 桌面程序技术方案、功能规划与实施顺序
│   └── echo-tuning-strategy.md   # 调谐规则、概率数据、数据来源与建模说明
├── examples/
│   └── echo.json                 # 声骸输入示例
├── src/
│   ├── echo_score.py             # 评分核心、输入校验及 JSON 命令行入口
│   ├── echo_strategy.py          # 激活策略分析：概率上下界、阈值决策及成本估算
│   └── echo_gui.py               # PySide6 图形界面：评分与后台策略分析
└── tests/
    ├── test_echo_score.py        # 评分、配置校验、非法输入及命令行测试
    ├── test_echo_gui.py          # GUI 交互、实时评分、后台任务及权重导入导出测试
    └── test_echo_strategy.py     # 激活策略概率、阈值决策、成本及边界测试
```


