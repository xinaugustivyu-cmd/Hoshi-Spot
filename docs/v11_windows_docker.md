# Hoshi V1.1 Windows / Docker Desktop 操作说明

状态：只供**离线配置检查和代码审查**。三组实时 Dry Run 尚未通过共享行情事件源与镜像下单路径的安全验收，策略会主动拒绝启动。不要运行 `docker compose up`。

项目目录为 `G:\Miracle\exchange`。V1.1 使用固定镜像 `freqtradeorg/freqtrade:2026.9`；A/B/C 的三份配置位于 `user_data/v11_configs/`，Compose 仅准备本机 WebUI 端口 8081/8082/8083。账户各有独立目录 `user_data/v11_accounts/A|B|C`，包含未来的模拟交易库、日志和风险账本；当前不会自动创建风险账本，也不会启动实时模拟。

## 只读检查

在 PowerShell 切换到项目目录后，复制 `.env.v11.example` 为 `.env.v11`，仅设置各账户**本机 WebUI**的不同用户名、强密码和 JWT secret。不要填写任何 Binance API Key 或 Secret。`.env.v11` 已被 Git 忽略。

```powershell
Set-Location G:\Miracle\exchange
Copy-Item .env.v11.example .env.v11
notepad .env.v11
docker compose --env-file .env.v11 -f docker-compose.v11.yml config --quiet
```

`config --quiet` 只验证容器配置，不启动服务，也不打印 WebUI 密码。启动器还提供 `--check` 模式，可在无网络的临时容器内验证每份 JSON 和环境安全约束；自动化测试已覆盖 `dry_run=false`、非现货、杠杆、交易所密钥和不允许的环境覆盖项。

## 模拟运行门禁

V1.1 现阶段仅允许运行 `python -m unittest` 与 Freqtrade `backtesting`。历史回放使用 `HOSHI_V11_RUN_ID` 为每次实验创建新账本；同名账本存在时拒绝覆盖。A/B/C 回放共享本地公开历史 K 线和一致的行情事件 ID；历史数据没有真实网络接收时刻，记录中保持空值。

三组 Dry Run 在以下证据全部完成、经人工审查后才可考虑手动启动：实时共享公共行情事件层；三个账户的风控账本显式初始化与故障恢复演练；无真实下单路径的镜像级验证；WebUI 强密码；数据库隔离和完整退出路径测试。即使未来启动，也仅限 Binance Spot Dry Run，不能提供实盘密钥或通过人工审批触发真实订单。
