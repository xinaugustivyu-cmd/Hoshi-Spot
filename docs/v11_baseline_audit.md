# Hoshi Spot V1.1 基线审计

审计日期：2026-10-27（UTC+8）。开发分支：`hoshi-v1.1-experiment`，从已确认的 V1 提交 `e172f2a8e3f5c3e092ac34aebd563469de5a3c85` 创建。审计时本地 `main` 与 `origin/main` 指向同一提交；本地和远程均无同名 V1.1 开发分支。

开始修改前，工作区只有两个未跟踪项目：`docs/risk_revision_proposal_20261027.md` 和 `baselines/v1_20261027/`，均为上一轮任务生成，已原样保留。已跟踪文件无未提交差异。要求的风控提案、`diagnostic_report.md`、`audit_checklist.md`、`experiment_plan.md`、`trade_breakdown.csv`、V1 策略与 `risk.py`、`indicators.py` 均存在。

基线归档保存 23 个原件副本，见 `baselines/v1_20261027/MANIFEST.sha256`；逐个比较归档、工作原件及清单，**23/23 SHA-256 一致**，无缺失。归档包括策略与辅助依赖、配置、Compose、原始中文回测报告、BTC/ETH/SOL 1h 与 4h 历史数据及六份原始回测 ZIP。Docker 镜像标签为 `freqtradeorg/freqtrade:2026.9`，本地镜像摘要记于归档说明。

关键 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `user_data/strategies/HoshiSpotV1.py` | `77694c4f3c0ecd58bc532a51b14225e6ff8d87dc4dba0a1eef0ee33a3464d454` |
| `user_data/hoshi/risk.py` | `2778b867984e954fb06aa5ba0e781cc97e4dec2df0c8112662960ffc22a849d5` |
| `user_data/config.json` | `8977c258d337df99b2fb4459ae7c06d0b7a4c9bcec412bc9a7910a937674fc08` |
| `docs/backtest_report_20261027.md` | `6bb4f58aa8e8c906fce2673573b1e002140924c36977b59b5142b1a9357db8cc` |
| 2024–2026 0.10% 原始 ZIP | `bf733978f72353876ff5a08056bb19136d610db96dcdd81e3a2bed201b3d06d7` |

结论：V1 基线已确认，可以在**独立新模块与实验目录**开展 V1.1；不得改写上述原件。后续验收将重新执行哈希对账。
