# API Failover Gateway v2 — 借鉴 CodexPlusPlus 的 API 聚合故障切换

## 核心设计（来自 CodexPlusPlus）

### 三种轮换策略
| 策略 | 行为 |
|------|------|
| `failover` | 成功保持当前成员，**只有失败才移到下一个**，全局共享指针 |
| `conversation` | 每个会话独立绑定一个成员，同一会话不漂移 |
| `round_robin` | 每次请求均匀轮询（兼容旧版） |

### Model 路由规则
可按模型名精确/通配符匹配指定 provider，优先级高于策略：
```yaml
routes:
  - pattern: "gpt-4*"   → openveer   (priority: 100)
  - pattern: "deepseek*" → deepseek  (priority: 100)
  - pattern: "glm*"     → siliconflow(priority: 90)
  - pattern: "*"        → openveer   (priority: 0, 兜底)
```

### 故障切换流程
1. 先按 model 路由匹配目标 provider
2. 发起请求，超时或 5xx 视为失败
3. 记录失败 → 移动指针到下一个 provider
4. 自动重试一次（若路由未命中），仍失败则返回 502

### 健康检查端点
- `GET /health` — 返回各 provider 状态、连续失败计数
- `GET /` — 返回服务信息、策略、所有路由规则

## 项目结构
```
/opt/api-gateway/
├── main.py                  # FastAPI 应用入口
├── config/
│   └── providers.yaml       # 供应商配置 + 路由规则
├── src/
│   ├── config.py            # YAML 配置加载（支持 ${ENV} 展开）
│   └── failover.py          # FailoverClient + FailoverSelector + RouteRule
├── venv/                    # Python 虚拟环境
├── .env                     # API Key（从环境变量注入）
└── requirements.txt
```

## 配置方法
编辑 `.env` 填入真实 API Key，然后重启：
```bash
nano /opt/api-gateway/.env
systemctl restart api-gateway
```

## 使用方式
```python
from openai import OpenAI
client = OpenAI(api_key="任意值", base_url="http://服务器IP:8000/v1")
resp = client.chat.completions.create(model="gpt-4o", messages=[...])
```

```bash
# 健康检查
curl http://服务器IP:8000/health

# 转发请求（可传 X-Conversation-Id 会话绑定）
curl -X POST http://服务器IP:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Conversation-Id: my-chat-1" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
```

## 服务管理
```bash
systemctl start/stop/restart api-gateway
journalctl -u api-gateway -f   # 实时日志
```