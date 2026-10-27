# Hoshi Trade V1

Hoshi Trade V1 是 Binance 现货公开行情研究与 Freqtrade 模拟交易项目。默认模拟余额为 80 USDT、每笔仓位不超过模拟权益的 20%、最多同时持仓一笔，只交易 BTC/USDT 与 ETH/USDT；SOL/USDT 只读取观察行情。项目不包含 Binance 交易密钥、真实下单或提现实现。

## 第一阶段需求审查

策略说明书的交易规则可由 Freqtrade 实现：4 小时 EMA20/EMA50 趋势过滤，1 小时 RSI14 上穿 50 且不高于 65、收盘上穿 EMA20、成交量超过此前 20 根均量的 1.1 倍、最新 K 线涨幅不超过 3%；信号只用收盘 K 线。计划止盈为 6%，止损为 4%，EMA20/RSI 趋势失效提示退出，最长持仓 14 天。说明书中的多资产排序没有“明显差别”的数值定义，V1 采用白名单顺序 BTC 优先作为平局规则；不添加额外入场条件。

当前稳定发布采用 **Freqtrade 2026.9**，Docker 镜像固定到该版本。官方提供 Binance OHLCV 下载、informative timeframe、回测、Lookahead Analysis、Dry Run 和 WebUI；4 小时趋势通过 Freqtrade informative pair 合并。Freqtrade 的 dataframe 不含未完成 K 线，回测信号在收盘时产生，并假设下一根开盘成交。可从 [官方发布记录](https://github.com/freqtrade/freqtrade/releases) 和 [稳定版文档](https://www.freqtrade.io/en/stable/)核对版本与命令。

### 技术风险和需要在后续阶段明确的事项

- **小资金交易门槛：** 16 USDT 的名义仓位须满足当时 Binance 交易对最小名义金额和数量精度。若不满足，策略拒绝开仓，不会抬高仓位。
- **风险阈值：** 单次 4% 止损与估算往返手续费/滑点约占初始权益 0.86%，符合说明书约 1% 计划风险。Freqtrade 的 StoplossGuard 用于止损后 24 小时停开新仓；MaxDrawdown 使用权益口径 5% 阈值，最长 100,000 根 1h K 线回看并锁仓（约 11.4 年），视为本阶段熔断。风控模块另外限制当天已有止损、当周累计净亏损 3%、阶段累计亏损达到初始实验本金 5% 后终止新开仓；阶段亏损采用所有已记录平仓的累计净损益，不是滚动 14 天阈值。
- **重大事件：** blackout 时间以 UTC 维护于 `user_data/hoshi/event_blackouts.csv`。首版已按说明书日期记录 2026-10-28 18:00 UTC 的 FOMC 决议发布前后各 6 小时；日期和发布时间以 [美联储官方日历](https://www.federalreserve.gov/newsevents/2026-october.htm)为准。只暂停新开仓，不更改持仓止损。
- **模拟限制：** Freqtrade Dry Run 是本机撮合模拟，不等同于 Binance 测试账户或真实成交。模拟止损、跳空、订单延迟和费用均有模型差异。
- **费率与滑点：** 回测默认显式传入 `--fee 0.001`（每边 0.1% 费率，Freqtrade 往返计两次）；额外滑点不由 OHLCV 回测原生重建。可用 `--fee 0.0015` 作为每边手续费加 0.05% 滑点的近似压力情景，报告须注明是近似模型。
- **环境状态：** Docker Desktop 4.97.0、Engine 29.9.2、Compose 5.6.1 已安装。Freqtrade 2026.9 镜像已拉取；配置校验、策略加载、历史下载、回测与偏差分析已实际运行。项目与 Docker/WSL 数据均放在 G 盘。

## 分阶段开发计划

1. **需求审查（完成）：** 提取说明书所有规则，核对 Freqtrade 稳定版本和官方能力，登记参数缺口和安全约束。
2. **基础工程（已完成整合检查）：** 固定 Freqtrade 2026.9 镜像；配置 Binance 公开行情、WebUI、1h/4h 数据；Compose 固定 `--dry-run` 和 `FREQTRADE__DRY_RUN=true`。
3. **策略及风控（已实现并在容器中加载）：** 完成 EMA/RSI/成交量规则、informative 4h 趋势、止盈止损/趋势/14 日退出、仓位限制、事件暂停、止损冷却、账户亏损闸门和 Dry Run 建议日志。Freqtrade 2026.9 的 protections 由策略属性提供。
4. **历史回测（已执行初轮）：** 下载 2021-01 至 2026-10 BTC/ETH/SOL 的 1h/4h 公共 K 线，运行上涨、下跌、横盘和样本外区间；报告见 [实测回测摘要](docs/backtest_report_20261027.md)。`lookahead-analysis` 未发现未来偏差，`recursive-analysis` 未发现指标未来偏差。样本外策略为负收益，尚未达到说明书净收益为正、Profit Factor ≥1.2 的观察门槛。
5. **模拟运行（连续观察暂缓）：** 尚未启动长期 Dry Run。先审查负向样本外表现和风险闸门，再决定是否把 Dry Run 用于工程监控；这不是实盘交易许可。

## 目录

```text
.
├── docker-compose.yml
├── README.md
├── .env.example
├── docs/
├── tests/
└── user_data/
    ├── config.json
    ├── strategies/HoshiSpotV1.py
    ├── hoshi/
    │   ├── advisor.py
    │   ├── event_blackouts.csv
    │   ├── indicators.py
    │   └── risk.py
    ├── data/
    ├── backtest_results/
    └── logs/
```

## Windows 11 安装和启动

1. Docker Desktop 已安装到 `G:\Miracle\docker\DockerDesktop`，WSL 引擎数据在 `G:\Miracle\docker\wsl`。打开 PowerShell 并切换到项目根目录。
2. 复制环境变量模板，并设置 WebUI 的本地访问密码和随机 JWT secret。此项目不需要 Binance API key 或 secret。

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

3. 下载固定版本镜像并检查 Compose 配置。

   ```powershell
   docker compose pull
   docker compose config
   docker compose run --rm hoshi-dry-run show-config --config /freqtrade/user_data/config.json
   ```

   合并配置中必须仍显示 `dry_run: true`、`trading_mode: spot`、空的交易密钥、80 USDT 钱包和 BTC/ETH 白名单。WebUI 只绑定到本机 `127.0.0.1:8080`。`exchange.enable_ws` 当前设为 `false`，使用 REST 行情轮询；本机 Binance WebSocket 曾超时。

### 下载历史 K 线

以下命令下载 BTC/ETH 的 1h 和 4h 公共现货 K 线，不请求私有账户接口。时间范围可按研究问题调整。

```powershell
docker compose run --rm hoshi-dry-run download-data `
  --config /freqtrade/user_data/config.json `
  --pairs BTC/USDT ETH/USDT SOL/USDT `
  --timeframes 1h 4h `
  --timerange 20210101- `
  --data-format-ohlcv feather
```

SOL 数据用于观察；SOL 不在策略交易白名单中。补齐本地已有时间段之前的数据时加 `--prepend`；之后再次运行相同时间范围可补齐后续数据。历史行情下载可能受 Binance API 速率限制或网络可用性影响。当前已下载区间截至 2026-10-27，保存在 `user_data/data/binance/`。

### 回测

每个区间都应独立报告，先固定区间和规则，再检查样本外结果。示例：前段数据用于研究，较新数据作为样本外；上下行/横盘阶段需结合实际 K 线区间选定，不要根据回测结果反复挑选有利区间。

```powershell
# 研究区间示例
docker compose run --rm hoshi-dry-run backtesting `
  --config /freqtrade/user_data/config.json `
  --strategy HoshiSpotV1 --timeframe 1h `
  --timerange 20210101-20241231 --fee 0.001 `
  --dry-run-wallet 80 --enable-protections

# 样本外示例
docker compose run --rm hoshi-dry-run backtesting `
  --config /freqtrade/user_data/config.json `
  --strategy HoshiSpotV1 --timeframe 1h `
  --timerange 20250101- --fee 0.0015 `
  --dry-run-wallet 80 --enable-protections

# 检查未来数据偏差和递归指标稳定性
docker compose run --rm hoshi-dry-run lookahead-analysis `
  --config /freqtrade/user_data/config.json --strategy HoshiSpotV1
docker compose run --rm hoshi-dry-run recursive-analysis `
  --config /freqtrade/user_data/config.json --strategy HoshiSpotV1 --timeframe 1h
```

Freqtrade 原生回测报告交易次数、盈亏、胜率、持续时间和回撤。`tools/summarize_backtests.py` 从原始 Freqtrade ZIP 和同区间 OHLCV 生成指标及 50/50 BTC/ETH 买入持有比较；此基准是未再平衡的参考，不代表相同风险。

### 启动模拟交易和 WebUI

```powershell
docker compose up -d
docker compose logs -f hoshi-dry-run
```

浏览器访问 `http://127.0.0.1:8080`，使用 `.env` 的 WebUI 账号。策略入场回调仅允许 Dry Run 或离线回测；拒绝 live runmode。Compose 配置、环境变量和启动命令均固定要求模拟模式。2026-10-27 已完成一次 20 秒启动冒烟检查：Freqtrade 2026.9 进入 RUNNING、API 服务启动并明确报告所有交易均为模拟；测试后已停止并移除临时容器。该检查验证了服务启动，不代表长期数据稳定性或策略有效性。停止容器：

```powershell
docker compose down
```

运行日志和模拟成交保存在 `user_data/logs/` 与 `user_data/tradesv3.dryrun.sqlite`。建议日志包含候选信号解释、模拟仓位、有效期和退出原因；日志不应包含密钥。

## 本地自动化测试

纯核心风险和指标测试不依赖 Freqtrade：

```powershell
docker compose run --rm `
  --volume .\tests:/freqtrade/tests:ro `
  --volume .\docker-compose.yml:/freqtrade/docker-compose.yml:ro `
  -e PYTHONPATH=/freqtrade/user_data `
  --entrypoint python hoshi-dry-run `
  -m unittest discover -s /freqtrade/tests -v
```

Docker 可用后，另外运行 Freqtrade 的策略加载、配置 schema 和回测分析命令。`lookahead-analysis` 与 `recursive-analysis` 是整合验收项；本地单元测试不能代替 Freqtrade 内部数据合并验证。

## AI 建议接口

`user_data/hoshi/advisor.py` 接收通过风控的确定性信号和指标快照，返回结构化建议：交易对、UTC 生成时间/失效时间、参考价、USDT 仓位、+6%/-4% 参考价格、理由和风险。`record_human_review()` 仅记录审批结论。AI 接口没有交易所对象、API 密钥或订单调用；人工记录也不会授权程序下单。未来接模型只能扩充解释输入输出，不得替换 `RiskSettings` 和 entry gate。

