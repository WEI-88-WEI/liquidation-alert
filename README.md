# liquidation-alert

Hyperliquid HIP-3 多市场价格监控与电话告警服务。目前支持：

- `xyz`：XYZ
- `io`：EntropyIO

每个市场可以配置多个币种和目标价格，也可以按滚动窗口振幅触发告警。配置可以通过本机 Web 管理页面修改，保存后立即生效，不需要改代码或重启服务。

## 功能

- 同时监控 XYZ 与 EntropyIO
- 每个币种支持多个上涨或下跌目标价
- 每个 `dex:symbol` 独立计算目标价与波动告警冷却
- 默认在 60 秒窗口振幅达到 1% 时告警
- 每个 DEX 每轮只请求一次 Hyperliquid API
- 配置校验、原子持久化与运行时热更新
- HTTP 状态接口和 Web 管理页面
- 地址开平仓监控、网页卡片通知与原生 / HIP-3 全部当前合约持仓查询

## 安装与启动

需要 Python 3.10 或更高版本。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

uvicorn app:app --host 0.0.0.0 --port 8794
```

启动前至少需要在 `.env` 中设置：

```env
FWALERT_URL=https://your-fwalert-url.com/call
```

访问 `http://服务器IP:8794/admin`。管理页面不需要用户名或密码，可以：

- 选择 XYZ 或 EntropyIO
- 从实时资产列表选择币种
- 启用或暂停监控
- 单独控制波动告警
- 添加多个上涨/下跌目标价
- 保存并立即应用配置

服务监听所有网络接口（`0.0.0.0:8794`），任何能连接服务器 8794 端口的人都可以打开管理页面并修改配置。请确保服务器防火墙或云安全组已放行 TCP 8794。

## 配置来源

首次启动时，服务读取 `.env` 中的 `COINS_CONFIG`。旧格式没有 `dex` 时会自动按 `xyz` 处理：

```env
COINS_CONFIG='[
  {
    "dex": "xyz",
    "symbol": "CL",
    "volatility_enabled": true,
    "targets": [
      {"price": 98, "direction": "up"}
    ]
  },
  {
    "dex": "io",
    "symbol": "OAI",
    "volatility_enabled": true,
    "targets": [
      {"price": 150, "direction": "down"}
    ]
  }
]'
```

在管理页面第一次保存后，配置会原子写入 `coins_config.json`，并优先于环境变量。此后增删币种或目标价都不再需要修改 `.env` 或重启。删除该文件后重启，可重新使用 `.env` 中的初始配置。

### 环境变量

| 变量 | 说明 | 默认值 | 必填 |
|---|---|---:|:---:|
| `FWALERT_URL` | 电话告警 webhook | - | 是 |
| `COINS_CONFIG_PATH` | 持久化配置文件路径 | `coins_config.json` | 否 |
| `COINS_CONFIG` | 首次启动的币种配置 | `[]` | 否 |
| `POLL_INTERVAL_SECONDS` | 轮询间隔（秒） | `10` | 否 |
| `COOLDOWN_SECONDS` | 每个市场的告警冷却（秒） | `1800` | 否 |
| `ENABLE_MONITORING` | 是否启动监控线程 | `true` | 否 |
| `VOLATILITY_WINDOW_SECONDS` | 波动统计窗口（秒） | `60` | 否 |
| `VOLATILITY_THRESHOLD_PERCENT` | 波动告警阈值（百分比） | `1` | 否 |
| `WALLETS_CONFIG_PATH` | 地址监控持久化配置路径 | `wallets_config.json` | 否 |
| `WALLETS_CONFIG` | 地址监控首次启动配置（JSON 对象） | `{"poll_interval_seconds":15,"wallets":[]}` | 否 |
| `WALLET_STATE_PATH` | 成交事件与去重检查点文件路径 | `wallet_monitor_state.json` | 否 |

`direction` 可以是：

- `up`：价格大于或等于目标价时触发
- `down`：价格小于或等于目标价时触发

波动振幅计算方式：

```text
振幅 = (窗口最高价 - 窗口最低价) / 窗口最低价 * 100
```

## HTTP 接口

| 接口 | 鉴权 | 用途 |
|---|---|---|
| `GET /` | 无 | 查看运行状态、实时价格和最近错误 |
| `GET /admin` | 无 | 打开管理页面 |
| `GET /api/config` | 无 | 读取当前配置 |
| `PUT /api/config` | 无 | 校验、保存并热应用配置 |
| `GET /api/assets` | 无 | 获取 XYZ 与 EntropyIO 的实时资产列表 |
| `GET /alert-data` | 无 | 读取最近的电话告警记录（最新在前，`limit` 默认 100，最大 10000） |
| `GET /alert` | 无 | 电话告警记录网页版表格（`limit` 默认 100，最大 500，页内可切 50/100/200/500 与自动刷新） |
| `GET /wallets` | 无 | 地址监控配置、开平仓卡片通知与持仓页面 |
| `GET /api/wallets` | 无 | 地址监控运行状态、缓存持仓和最近 100 条成交事件 |
| `GET /api/wallets/config` | 无 | 读取地址监控配置 |
| `PUT /api/wallets/config` | 无 | 校验、保存并热应用地址监控配置 |
| `GET /api/wallets/events` | 无 | 最近成交事件，可按 `address` 筛选；`limit` 默认 100，最大 500 |
| `GET /api/wallets/{address}/positions` | 无 | 读取已配置地址的缓存持仓；`refresh=true` 立即查询 HL |

`GET /alert-data` 读取 `alerts_log.jsonl`（服务同目录），只返回文件末尾的 `limit` 条，所以文件再大也不会一次读进内存；文件不存在或读取失败时返回空列表，不会报错。

```bash
curl -s 'http://服务器IP:8794/alert-data?limit=20'
```

```json
{
  "count": 20,
  "alerts_log_path": "/root/repos/liquidation-alert/alerts_log.jsonl",
  "fwalert_configured": true,
  "items": [
    {
      "event": "volatility_reached",
      "market": "io:OAI",
      "price": 1510.171,
      "percent_move": 1.4326854376093963,
      "beijing_time": "2026-09-13T15:39:17.305630+08:00",
      "status_code": 200
    }
  ]
}
```

每条记录代表一次电话告警尝试：`status_code` 存在表示 webhook 调用成功（已拨出），`error` 字段存在表示失败（例如 `missing_fwalert_url`）。注意 `trigger_phone_alert` 在冷却期内被抑制时不写记录。

### 网页版：`GET /alert`

```
http://服务器IP:8794/alert
```

服务端渲染的表格，列：时间（北京）、事件、市场、价格、触发条件、结果。右上角可以：

- 切换显示条数（50 / 100 / 200 / 500，页面最多 500 条）
- 打开 `JSON` 链接跳到 `/alert-data`
- 勾选「自动刷新 15s」（选择记在浏览器本地，刷新后保持）

页面顶部显示当前窗口条数、近 24 小时条数和页面生成时间。两种事件都会翻译成人话：价格到达显示「目标价 84（向下到达）」，波动告警显示「60s 振幅 1.43%（阈值 1%，区间 1510.171 ~ 1531.807）」。

历史记录做了兼容：`64ba662`（2026-09-05 多 DEX 版本）之前的记录没有 `market` 字段，页面退回显示 `symbol`；更早的版本还会写 `suppressed: true` + `reason: "cooldown"`（冷却期抑制）的记录，页面单独标成「冷却期抑制（cooldown）」而不是「已拨出」。所有字段在渲染前都会 HTML 转义。

状态中的币种使用完整市场 ID，例如：

```json
{
  "coins": {
    "xyz:NBIS": {
      "dex": "xyz",
      "symbol": "NBIS",
      "price": 42.18
    },
    "io:NBIS": {
      "dex": "io",
      "symbol": "NBIS",
      "price": 42.21
    }
  }
}
```

## 地址开平仓监控：`GET /wallets`

访问 `http://服务器IP:8794/wallets`，默认在「监控看板」查看仓位和通知；切换到「监控配置」tab 添加 HL 钱包地址、备注，点击「保存并生效」。最多配置 20 个地址，每个地址可以独立暂停或启用。该功能只读取公开账户数据，无需私钥，也无需配置 `FWALERT_URL`。请填写实际交易账户或子账户地址，代理签名钱包地址不代表对应交易账户。

页面提供：

- **成交卡片通知**：显示地址 / 备注、完整品种名（如 `BTC`、`xyz:CL`）、开仓 / 加仓 / 减仓 / 平仓 / 反手、多空方向、成交数量、成交价格、已实现盈亏和北京时间。页面打开后，新增成交会在右下角提示。
- **当前所有合约仓位**：每个地址独立折叠展示，点击地址展开该地址的全部仓位；自动同步会保留展开状态。覆盖 HL 原生合约及 API 动态发现的 HIP-3 市场，显示方向、数量、开仓均价、仓位价值、未实现盈亏、杠杆及强平价。可以筛选地址或在展开后单独刷新持仓；已暂停地址仍可手动查询。
- **网页配置**：在独立的「监控配置」tab 编辑地址、备注、开关及检查间隔，均支持热更新。切换 tab 会保留未保存的修改和仓位展开状态。浏览器每 5 秒读取服务缓存，自动同步其他页面已保存的地址和检查间隔，同时保留正在编辑的草稿。草稿与最新保存配置发生冲突时会提示重新载入，阻止旧配置覆盖新修改。

「下单 / 关单」以**实际成交造成的仓位变化**为准，未成交挂单、撤单和现货买卖不会触发开平仓卡片。监控使用成交记录，能识别两轮检查之间完成的开仓和平仓，不依赖两次持仓快照的差异。

默认检查间隔 **15 秒**，可以在页面改为 5～300 秒。为控制 HL API 请求量，启用多个地址时实际等待间隔为 `max(配置间隔, 启用地址数 × 3)` 秒；每轮完成后再等待该间隔，网络耗时会延长检测延迟。页面显示实际间隔，持仓请求失败时保留上次完整结果并显示错误和更新时间。

首次启用从启用时刻监控，不会把历史成交当成新通知。暂停期间的成交在重新启用后不补发。最近 1000 条事件及成交检查点保存在 `wallet_monitor_state.json`，重启后继续查询并去重；页面最多显示 100 条。HL 成交接口仅提供最近 10000 条成交，长时间离线或极高频交易可能导致历史缺口，无法保证补齐接口已不再提供的记录。

首次启动也可以在 `.env` 中配置：

```env
WALLETS_CONFIG='{
  "poll_interval_seconds": 15,
  "wallets": [
    {
      "address": "0x1111111111111111111111111111111111111111",
      "label": "关注地址",
      "enabled": true
    }
  ]
}'
```

将示例地址替换为实际关注的账户地址。网页保存后 `wallets_config.json` 优先于环境变量，配置与价格监控的 `coins_config.json` 独立。保留配置文件中由服务维护的检查点会话信息，以便重启后正确识别暂停 / 重新启用的时间。`ENABLE_MONITORING=false` 会停止价格和地址两个后台监控线程，查询和配置路由仍可用。

通过 HTTP 保存配置的请求体与上述 JSON 相同。`GET /api/wallets/config` 返回 `config.version`；PUT 时可额外传入 `expected_version`，服务会在保存前校验版本，不匹配时返回 HTTP 409 且不修改配置。网页会自动携带此字段；未传时兼容原有 API 调用。所有地址监控接口沿用现有服务的无登录方式。

## systemd

仓库提供 `systemd.liquidation-alert.service`。复制 service 文件到 systemd 目录并确认部署路径后：

```bash
systemctl daemon-reload
systemctl enable --now liquidation-alert.service
systemctl status liquidation-alert.service
```

服务监听 `0.0.0.0:8794`，并使用进程内监控线程和状态。请保持 Uvicorn 为单 worker，不要添加 `--workers` 参数。

## 测试

```bash
python -m unittest discover -s tests -v
```
