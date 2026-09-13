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
