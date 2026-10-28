# Hoshi Spot V1.1：PR #1 安全门禁续验

日期：2026-10-28（香港时间；回放日志为 UTC）。分支：`hoshi-v1.1-experiment`。范围仅为代码、离线注入、历史回放和短时无交易检查。本轮没有启动 A/B/C 实时 Dry Run，没有执行真实订单，没有合并 PR。原始 V1 的 23 个清单项目与工作区原件重新计算 SHA-256，**23/23 一致**。

## P0-1：三组共享公开行情

`LiveMarketJournal` 把 BTC/ETH 的 1h、4h 已收盘 K 线组成一条原子事件；事件 ID 由 UTC 决策时刻和 OHLCV 内容计算，另存实际接收时刻。A/B/C 以只读方式访问同一 SQLite，交易前逐币逐周期比较 Freqtrade 的已分析行与事件内容。缺失、重复、乱序、过期、未来 4h、异常 OHLCV、损坏数据库均拒绝新开仓；故障使旧快照失效，只有后续完整事件可恢复。审计 JSONL 可按顺序重放，且核对接收时刻和事件 ID。

生产者仅从 [Binance 市场数据专用域名](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md)读取公开 `/api/v3/klines`；本轮先以假传输离线注入，并在无交易依赖的镜像中短时读取 BTC/ETH 各 1h、4h 公开历史 K 线。后续人工批准前，未启动连续实时数据服务。注入测试见 `tests/test_v11_live_events.py`，短时只读结果见 `user_data/logs/v11_public_market_smoke.txt`。

## P0-2：真实订单路径隔离

已检查 Freqtrade 2026.9 的 `Exchange.create_order` 与 Binance CCXT 同步、异步 `request → fetch2 → fetch` 调用链。受控入口先安装同步/异步私有端点拦截，再导入 Freqtrade；它只接受固定模拟配置和精确 `trade --config ... --strategy ... --dry-run` 参数。非 Dry Run、任何交易所凭据、合约/借币、杠杆、不同资金或风控参数、额外命令行及环境覆盖均拒绝。APPROVED 仍仅是审批数据库状态，无下单回调。

A/B/C 容器只接入 Docker `internal: true` 的 `hoshi-paper-only` 网络；一次短时无交易容器检查中，直连外网返回 `ENETUNREACH (101)`。实际 A 容器内的 CCXT 私有订单调用在网络前被拒绝。唯一双网络容器是公开行情进程，其 HTTP 网关只接受 Binance 现货公共路径 GET，写入/私有路径、绝对 URL 和签名查询均被拒绝。独立行情镜像运行时无法导入 `freqtrade`、`ccxt`，也没有 `freqtrade` 命令。三组配置只向内部只读网关请求现货元数据，且限定 CCXT `fetchMarkets.types=["spot"]`。默认 Compose profile 不包含任何 V1.1 服务；三组检查入口均以 `--check` 短时退出（0/0/0），没有运行交易进程。

这里的证明限定于本 PR 提供的镜像、Compose 网络、只读挂载及受控入口。A/B/C 研究镜像仍包含 Freqtrade 通用代码；隔离靠入口、CCXT 边界、无密钥、内部网络和只读网关共同成立，不能描述成“研究镜像没有订单源码”。主机管理员若改写 Compose 或镜像，应重新验收。

## P1：最终 A 组历史回放

**BLOCKED，不能提供最终全区间指标。**回放命令使用固定 A 策略、80 USDT、BTC/ETH、2024-01-01 至 2026-10-01、每侧 0.1% 手续费、`--enable-protections --cache none`。仅回测时叠加市场数据专用域名，以加载现货 `exchangeInfo`；部署配置未改。回测入口仍安装订单拦截。先前 `01` 因离线网关不可用、`02` 因 CCXT 默认触及合约元数据被拦截，均未生成结果；`03` 在代码冻结前主动中止，其部分账本和日志留在忽略目录，**不得视为完整回放**。

最终代码的 `A-final-20261027-04` 在历史时点 **2025-03-24 14:00 UTC** 发生 SQLite `disk I/O error`。风控立即生成 `.emergency.json` 并进入 `EMERGENCY_HALT`；为避免把故障后的 2025–2026 区间写成有效回放，已停止该进程（退出码 137）。因此**没有完整回测 ZIP**，不能宣布第三门禁通过。部分账本截至 2025-03-24 13:00 UTC 有 **10,934** 条权益采样、**47 笔、−0.959835 USDT**；它们与旧 A 组完整回放的 47 笔和净额一致，但这只验证故障前路径。首次长期停机仍为 **2024-07-23 14:00 UTC / `max_drawdown`**，部分账本最高收盘采样回撤 **5.2325%**。上述数字不能替代全区间回放和完整风险事件复核。

故障后只读检查：`PRAGMA quick_check=ok`；G 盘 NTFS 状态 Healthy/OK，剩余空间约 146 GB；同期 Windows System 错误/警告 0 条。没有足够证据把故障归因于磁盘硬件、Docker 挂载或并发读取。保留 `user_data/logs/v11_A_final_20261027_04.txt`、部分 `user_data/experiments/risk_state/A-A-final-20261027-04.sqlite` 与 `.emergency.json`，不删除、不复用该 Run ID，不清除紧急标记来追求好看的结果。旧 ZIP、账本、报告保持原样。

故障证据 SHA-256：部分 SQLite `CD06A0CC8DC7BC739914F82231463E07737587DFBA1A40C1A9A0BA7CB0405D87`；紧急标记 `58213A927CA21816779886659CEA8762BA54A75F23CDF59833AA395801EEB014`；完整回放日志 `E8B2AD409769B1BE8D64025D4462B1D650175CD90867EBD194D94627773002FA`。19 个已加载的策略、行情、配置和 Compose 文件在回放起止时 SHA-256 一致，清单保存在 `user_data/logs/v11_final_source_hashes.txt`。

## P1：短时安全验收

`test_v11_*` 全部 **67 通过、0 失败**（完整原始输出：`user_data/logs/v11_p0_tests_final.txt`）。覆盖 A/B/C 独立交易库/风控库路径、同一风控参数哈希、跨重启人工停机、数据故障后两根新收盘 K 线恢复、模拟钱包与交易账目对账、同一事件顺序、未满足门禁拒绝启动、APPROVED 无订单路径、B 连续两根 1h 退出、C 已收盘 4h 趋势与未来数据隔离。B/C 没有长区间调参或绩效筛选。Compose 配置检查通过，市场专用镜像离线构建通过；证据分别见 `user_data/logs/v11_market_build_final.txt`、`user_data/logs/v11_network_check.txt`、`user_data/logs/v11_actual_container_order_block.txt`、`user_data/logs/v11_resolved_config_check.txt`、`user_data/logs/v11_check_{A,B,C}.txt`。

| 门禁 | 结论 | 证据边界 |
|---|---|---|
| 共享公开行情 | PASS（离线和短时只读） | 同一事件 ID、UTC、实际接收时刻、异常暂停、顺序回放；尚未做连续实时采样 |
| 执行层隔离 | PASS（指定镜像/Compose） | 同步/异步订单路径拦截、无密钥、内部网络、网关只读；不覆盖主机管理员改写部署 |
| 最终 A 回放与短时验收 | **BLOCKED** | 短时安全项通过；完整 A 回放因 SQLite I/O 故障中止，无全区间 ZIP |

## 本轮运行命令与日志

下列命令只记录本轮 `04` 的离线历史回放条件，**不要复用该 Run ID 或在存储故障未定位前重启**。实际 `04` 使用忽略目录中的同内容覆盖文件；已验证它与仓库内 `user_data/v11_configs/backtest_public_only.json` 的 JSON 完全相等。以下把该路径替换为仓库文件，便于审阅；未来复测须另设新 Run ID 和输出名。

```powershell
docker run --rm --network bridge `
  --volume "G:\Miracle\exchange\user_data:/freqtrade/user_data" --workdir /freqtrade `
  --env PYTHONPATH=/freqtrade/user_data --env HOSHI_V11_RUN_ID=A-final-20261027-04 `
  --env FREQTRADE__API_SERVER__USERNAME=offline-research `
  --env FREQTRADE__API_SERVER__PASSWORD=offline-only-password-123456 `
  --env FREQTRADE__API_SERVER__JWT_SECRET_KEY=offline-only-jwt-secret-123456789012345678901234 `
  --entrypoint python freqtradeorg/freqtrade:2026.9 -m hoshi_v11.guarded_freqtrade `
  backtesting --config /freqtrade/user_data/v11_configs/A.json `
  --config /freqtrade/user_data/v11_configs/backtest_public_only.json `
  --strategy HoshiSpotV11A --strategy-path /freqtrade/user_data/strategies `
  --timeframe 1h --timerange 20240101-20261001 --fee 0.001 `
  --dry-run-wallet 80 --enable-protections --cache none --export trades `
  --export-filename /freqtrade/user_data/backtest_results/v11_A_final_20261027_04.json
```

候选配置入口检查仅执行 `python -m hoshi_v11.safe_launcher --check A|B|C ...`；测试执行 `python -m unittest discover -s /freqtrade/tests -p 'test_v11_*' -q`。两者均未调用 `trade`。完整命令行、测试输出及镜像构建日志保留在上文列出的 `user_data/logs/` 文件中。

## 精度与剩余限制

历史权益采样基于 1h K 线及模拟费用，无法重建 K 线内部价格路径、盘口或真实滑点。公共行情短时检查与离线注入不等于 14 天连续运行；断线超出 72 小时需人工处理。候选账户风险库和共享实时账本本轮未为长期服务初始化。下一步需在**不改风控规则**前提下定位 SQLite I/O 故障，在隔离环境完成新的全区间 A 回放并对账；通过前不得申请启动 14 天 Dry Run。原始 V1 及历史结果未改变；任何未来实时模拟仍必须只用虚拟资金、现货、无交易所密钥。
