# liquidation-alert

多币种价格监控告警服务，支持多个币种、多个价格阈值（支持上涨/下跌两个方向），到达目标价时触发电话告警。

## 功能

- 支持配置**多个币种**
- 每个币种可设置**多个目标价格**
- 支持两个方向：
  - `up`：价格 ≥ 目标价 时触发
  - `down`：价格 ≤ 目标价 时触发
- 每个币种**独立冷却**机制
- 每 10 秒轮询一次价格
- 提供 HTTP 接口查看实时状态

## 配置

所有配置通过 `.env` 文件完成。

### 环境变量

| 变量                    | 说明                     | 默认值   | 必填 |
|-------------------------|--------------------------|----------|------|
| `FWALERT_URL`           | 电话告警 webhook         | -        | 是   |
| `POLL_INTERVAL_SECONDS` | 轮询间隔（秒）           | 10       | 否   |
| `COOLDOWN_SECONDS`      | 告警冷却时间（秒）       | 1800     | 否   |
| `ENABLE_MONITORING`     | 是否开启监控             | true     | 否   |
| `COINS_CONFIG`          | 多币种配置（JSON）       | -        | 是   |

### COINS_CONFIG 配置示例

```env
COINS_CONFIG='[
  {
    "symbol": "SILVER",
    "targets": [
      {"price": 63.2, "direction": "up"},
      {"price": 60.0, "direction": "down"}
    ]
  },
  {
    "symbol": "GOLD",
    "targets": [
      {"price": 2400, "direction": "up"}
    ]
  },
  {
    "symbol": "BTC",
    "targets": [65000]
  }
]'
```

- `direction` 可选，默认为 `up`
- 每个币种的冷却独立计算

## 启动

```bash
# 开发启动
uvicorn app:app --host 0.0.0.0 --port 8794

# 使用 systemd（推荐）
systemctl restart liquidation-alert.service
```

## 接口

- `GET /`：查看当前所有币种监控状态

返回示例：

```json
{
  "service": "liquidation-alert",
  "running": true,
  "coins": {
    "SILVER": {
      "price": 63.25,
      "targets": [...]
    }
  },
  "loop_count": 142
}
```

## 注意事项

- 服务默认使用 Hyperliquid XYZ 的价格源
- 建议配合 systemd 守护运行
- 修改配置后需重启服务生效