# 项目文件结构

```text
WWEcho/
├── README.md                     # 项目说明、评分规则、使用方法与测试说明
├── WIP.md                        # U R Here --> 项目目录结构
├── requirements.txt              # GUI 依赖：PySide6-Essentials
├── run.sh                        # GUI 启动脚本：cmd ./run.sh 启动
├── .gitignore                    # Git 忽略规则
├── .vscode/
│   └── tasks.json                ## VS Code 任务：启动声骸评分 GUI
├── config/
│   ├── activation-costs.json     ## 用户提供的激活消耗、弃置回收与默认成本权重
│   ├── echo-stats.json           ## 基本参数：声骸词条定义、主词条满级值、副词条档位与概率
│   └── score-weights.json        ## 评分权重：主/副词条默认评分权重（占位值，可自定义）
├── docs/
│   ├── activation-recommendation-algorithms.md ## 激活建议算法设计；部分已有原型实现
│   ├── desktop-app-plan.md       ## 桌面程序技术方案、功能规划与实施顺序
│   ├── echo-tuning-strategy.md   ## 调谐规则、概率数据、数据来源与建模说明
│   └── reconstruction-algorithms.md ## 频整器概率、收益、同预算比较与边际公式
├── examples/
│   └── echo.json                 ## 声骸输入示例
├── src/
│   ├── echo_costs.py             ## 轻量成本配置：GUI 与策略共用的默认值和校验
│   ├── echo_score.py             ## 评分核心、输入校验及 JSON 命令行入口
│   ├── echo_strategy.py          ## 激活策略：概率上下界与材料权重累计成本最优停止
│   ├── echo_reconstruction.py    ## 频整器：精确离散锁法分析与固定锁法预算比较
│   └── echo_gui.py               ## PySide6 图形界面：评分与后台策略分析
└── tests/
    ├── test_echo_score.py        # 评分、配置校验、非法输入及命令行测试
    ├── test_echo_gui.py          # GUI 交互、实时评分、后台任务及权重导入导出测试
    ├── test_echo_strategy.py     # 激活概率、累计成本策略、解析验证及边界测试
    ├── test_echo_reconstruction.py # 频整器概率、严格升分、预算及边际测试
    └── test_echo_reconstruction_gui.py # 区域4锁定按钮、表格、缓存及异步测试
```


