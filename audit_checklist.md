# Hoshi Spot V1 代码、数据与回测审计清单

审计日期：2026-10-27。V1 策略、策略参数和模拟资金配置保持原样；本轮增加诊断工具及审计报告，不执行真实交易。

## 审计结果

| 审计项 | 结果 | 证据 / 限制 |
|---|---|---|
| Freqtrade 版本 | 通过 | 固定镜像 `freqtradeorg/freqtrade:2026.9`；实际命令日志显示 2026.9。 |
| 真实交易安全 | 通过 | `dry_run: true`、`trading_mode: spot`、`can_short=False`、空 Binance key/secret；策略启动及入场闸门拒绝 live runmode。交易命令显式传 `--dry-run`。 |
| 杠杆、合约、提现 | 通过 | 配置为 spot，没有 futures/swap 白名单或借贷逻辑；策略只允许 long 且 leverage 必须为 1；源码无提现/转账实现。 |
| 交易对 / 持仓 | 通过 | 可交易白名单仅 BTC/USDT、ETH/USDT；`max_open_trades=1`。SOL 只列为观察 informative pair。 |
| 初始模拟资金 | 通过 | `dry_run_wallet=80`，回测命令传 `--dry-run-wallet 80`。`stake_amount=unlimited` 与仓位回调一起动态计算，单笔不超过当前账户权益 20%。初始余额的 99% 可用于交易，所以第一笔回测名义仓位约为 15.84 USDT，而非必须正好 16 USDT。 |
| 单笔风险预算 | 部分通过 | `planned_stake` 以 4% stoploss + 每边费率/滑点预算计算风险上限，并与权益 20% 上限取小值；在 80 USDT 下仓位上限仍为约 16 USDT。实际 OHLCV 回测只计 `--fee`，没有执行 `slippage_per_side`。 |
| 最小金额和数量精度 | 通过但有限定 | 本次使用 CCXT 4.5.84 的 Binance 公开市场元数据：BTC/USDT、ETH/USDT 最低名义金额均为 5 USDT；数量精度分别为 0.00001 BTC、0.0001 ETH。所测模拟仓位约 16 USDT，明显高于当前最低名义额，数量由 Freqtrade 下单精度逻辑处理。交易所历史过滤器可能变更，当前元数据不能证明过去每个日期的历史限制完全相同。 |
| 收盘 K 线 / 未来数据 | 通过 | 入场交叉用 `shift(1)` 对比前一根已收盘 K；成交量均线 `.shift(1)`；Freqtrade informative decorator 自动按安全时间差合并 4h。全区间 lookahead-analysis 对 509 个初始信号中的 20 笔抽样：`no bias detected`、0 个偏差入场/出场/指标。该工具抽样并改写最大持仓/下单模型，不能验证收益和风险规则。 |
| 递归稳定性 | 通过 | recursive-analysis 在 BTC/USDT 上测试 EMA20/50 4h、EMA20、RSI14、前 20 根均量；报告全程 0.000% 或浮点舍入零。Freqtrade 该命令本次选择 BTC 单币运行；不能据此宣称 ETH 单独已执行递归分析。 |
| 多时间周期对齐 | 通过 | 策略使用 `@informative("4h")`，而不是未平移的自写 asof merge。Freqtrade 在合并不同周期时将高周期可见时间延至该 K 线收盘；1h 行不会提前看到正在形成的 4h K 线。策略自带 `merge_closed_informative` helper 未被策略交易路径调用。 |
| 预热 | 有限通过 | 1h `startup_candle_count=250`，约 10.4 天；4h EMA50 至少需约 8.3 天的数据。文件从 2021-01-01 UTC 开始，第一段正式回测开始于 2021-01-11 10:00 UTC。日志提示 4h 请求的更早历史在文件中不存在；在第一段开始前可用数据仍超过 50 根 4h K 线，递归检查为零，但早期边缘不如有更早下载数据稳妥。 |
| 数据重复、时区和缺口 | 部分通过 | 1h / 4h 时间均按 UTC 解析；三资产无重复时间戳、无乱序、OHLCV 无空值。BTC/ETH/SOL 的 1h 文件都有相同的 7 个缺口区间，共约缺 14 根 K 线；4h 文件无缺口。缺口在 2024 OOS 前，但包含 2021/2022 部分训练/诊断窗口。见诊断工具的 `OHLCV_DATA_QUALITY` 输出。 |
| 回测保护启用 | 通过 | 六个最终回测 JSON ZIP 的 `enable_protections=true`。回放命令含 `--enable-protections`；默认不应省略此开关。 |
| 日风险检查 | 部分通过 | `loss_limit_reason` 遇到 UTC 当天任一 `stop_loss`/`stoploss` 平仓即拒绝新增交易；Freqtrade StoplossGuard 另设 24 根 1h 回看、1 次止损、锁 24 根。`daily_stoploss_limit` 字段未被读取，实际实现由硬编码 `any()` 完成。EMA/RSI 亏损退出不属于 stoploss，不触发该止损冷却。 |
| 周风险检查 | 部分通过 | 独立风险闸门按 UTC 周一至当前时间累计平仓净损益，周亏达 3% 时停止新增交易。不是 Freqtrade 原生 Protection；取决于策略回调与交易历史可用。 |
| 14 日风险规则 | 未满足当前字面要求 | `max_holding_14d` 是 14 天最大持仓退出，并非 14 日累计亏损窗口。阶段亏损闸门对整个阶段历史累计损益判断是否亏 5%，没有按最近 14 天滚动；MaxDrawdown 则看 100,000 根 1h K 线。若需求是“最近 14 日亏损达初始本金 5% 熔断”，当前代码不满足，需确认原说明书后另行批准修改。 |
| 最大回撤口径 | 已启用但不是硬上限 | `MaxDrawdown` 明确为 `calculation_mode: equity`，即账户权益峰谷回撤；窗口 100,000 根 1h K 线。保护在平仓后评估，且拦截之后的新开仓，不会强制平掉开放仓位。2021 回撤 5.13%、OOS 0.10% 费率回撤 5.12%、0.15% 费率回撤 5.33%，均说明 5% 是触发阈值而非损失保证。 |
| 费率和滑点 | 部分通过 | 原始回测用 `--fee 0.001`，按开仓、平仓各计一次；敏感性使用 0.0015 和 0.002。`slippage_per_side=0.0005` 只用于仓位预算，不改变历史成交价格。没有订单簿/逐笔滑点模型。 |
| 缓存与原始结果 | 复跑确认 | 当前 Freqtrade 默认允许复用最近一天的相同策略和配置缓存，历史命令记录并未保存，不能仅凭原始 ZIP 证明最初每次都禁用了缓存。现以 `--cache none` 重新运行了三个历史区间和三个 OOS 费率组合；摘要的交易数、净损益、回撤、平均持仓均与原报告完全一致。结果 ZIP 记录有费率、配置、策略快照、行情变动和权益曲线。 |
| 可复现性快照 | 有限通过 | ZIP 包含策略文件和净化后的配置，但策略还 import `user_data/hoshi/` 辅助模块，ZIP 并不打包这些依赖；重跑需保留当前这些源文件版本以及相同 OHLCV 文件。建议存档文件校验和。 |
| 旧结果隔离 | 通过 | `user_data/backtest_results/pre_stage_cap_20261027/` 是阶段熔断实现调整前的旧结果，例如 2021 有 63 笔 / -0.091 USDT、OOS 有 214 笔 / -9.759 USDT；它们和当前策略/保护路径不同，不计入本报告的 145 笔逐对归因和 `trade_breakdown.csv`。 |

## 原始回测与缓存复核

当前正式原始结果 ZIP 六份：2021、2022、2023 三个基准费率历史段；2024-01-01 至 2026-10-01 OOS 的 0.10%、0.15%、0.20% 三种费用。它们均记录 `enable_protections=true`。另有 `replay*` 前缀的新回放 ZIP，是本轮 `--cache none` 重跑产物；归因 CSV 只读取原始六份 `backtest-result-*.zip`，不把复跑和旧阶段结果重复计入。本轮用以下参数关闭缓存重新运行：

```powershell
docker compose run --rm hoshi-dry-run backtesting `
  --config /freqtrade/user_data/config.json `
  --strategy HoshiSpotV1 --strategy-path /freqtrade/user_data/strategies `
  --timeframe 1h --timerange 20210111-20211201 `
  --fee 0.001 --dry-run-wallet 80 --enable-protections `
  --export trades --cache none

docker compose run --rm hoshi-dry-run backtesting `
  --config /freqtrade/user_data/config.json `
  --strategy HoshiSpotV1 --strategy-path /freqtrade/user_data/strategies `
  --timeframe 1h --timerange 20220101-20230101 `
  --fee 0.001 --dry-run-wallet 80 --enable-protections `
  --export trades --cache none

docker compose run --rm hoshi-dry-run backtesting `
  --config /freqtrade/user_data/config.json `
  --strategy HoshiSpotV1 --strategy-path /freqtrade/user_data/strategies `
  --timeframe 1h --timerange 20230501-20231001 `
  --fee 0.001 --dry-run-wallet 80 --enable-protections `
  --export trades --cache none

# 复制本命令三次，将 --fee 分别设为 0.001、0.0015、0.002
docker compose run --rm hoshi-dry-run backtesting `
  --config /freqtrade/user_data/config.json `
  --strategy HoshiSpotV1 --strategy-path /freqtrade/user_data/strategies `
  --timeframe 1h --timerange 20240101-20261001 `
  --fee 0.001 --dry-run-wallet 80 --enable-protections `
  --export trades --cache none
```

复核结果与归档结果一致：2021 为 27 笔 / -4.107 USDT / 5.13%回撤；2022 为 50 笔 / -1.938 USDT / 3.56%；2023 为 20 笔 / -1.368 USDT / 2.64%；OOS 三费率依次 48 笔 / -1.220 / 5.12%、47 笔 / -2.112 / 5.33%、45 笔 / -2.635 / 5.04%。

## 偏差检查可复现命令

```powershell
docker compose run --rm hoshi-dry-run lookahead-analysis `
  --config /freqtrade/user_data/config.json `
  --strategy HoshiSpotV1 --strategy-path /freqtrade/user_data/strategies `
  --timerange 20210111-20261001

docker compose run --rm hoshi-dry-run recursive-analysis `
  --config /freqtrade/user_data/config.json `
  --strategy HoshiSpotV1 --strategy-path /freqtrade/user_data/strategies `
  --timeframe 1h --timerange 20210111-20261001
```

逐笔 CSV 生成命令（容器仅挂载项目文件，不访问认证交易接口）：

```powershell
docker compose run --rm `
  --volume .:/project `
  --volume .\tools:/freqtrade/tools:ro `
  --entrypoint python hoshi-dry-run `
  /freqtrade/tools/diagnose_trades.py --output /project/trade_breakdown.csv
```

## 官方参考

- [Freqtrade 2026.9 回测文档](https://www.freqtrade.io/en/2026.9/backtesting/)：缓存、费用、起始资金、结果 ZIP 和 OHLCV 的盘中路径精度限制。
- [Freqtrade 2026.9 策略定制文档](https://www.freqtrade.io/en/2026.9/strategy-customization/)：informative pair 与无前视高周期合并。
- [Freqtrade 2026.9 保护机制文档](https://www.freqtrade.io/en/2026.9/plugins/)：保护机制需通过 `--enable-protections` 加入回测；权益口径 MaxDrawdown 定义。

