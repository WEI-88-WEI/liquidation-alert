# liquidation-alert

监控 XYZ 白银（XAG）价格，到达 63.2 时触发电话告警。

## 功能
- 每 10 秒获取一次 xyz:XAG 价格
- 价格 ≥ 63.2 时触发电话告警
- 带 30 分钟冷却机制

## 环境变量
- `FWALERT_URL`: 电话告警 webhook（必填）
- `TARGET_PRICE`: 触发价格（默认 63.2）
- `POLL_INTERVAL_SECONDS`: 轮询间隔（默认 10）
- `COOLDOWN_SECONDS`: 告警冷却时间（默认 1800）

## 启动
```bash
uvicorn app:app --host 0.0.0.0 --port 8791
```

## 接口
- `GET /` : 查看当前状态
