# API Failover Gateway

OpenAI 兼容的 API 聚合故障切换网关，借鉴 [CodexPlusPlus](https://github.com/BigPizzaV3/CodexPlusPlus) 的 `RelayRotationSelector` 设计。

当一个 API 供应商请求失败时，自动切换到下一个可用供应商，确保服务高可用。

## 特性

- **三种轮换策略**
  - `failover`：成功保持当前成员，**只有失败才移到下一个**（推荐）
  - `conversation`：每个会话独立绑定一个供应商，同一会话不漂移
  - `round_robin`：每次请求均匀轮询所有供应商
- **Model 路由规则**：按模型名通配符匹配指定供应商，优先级排序
- **OpenAI 兼容接口**：完全兼容 `/v1/chat/completions` 等标准路径
- **流式响应**：支持 SSE 流式输出（`?stream=true`）
- **健康检查**：实时查看各供应商状态和连续失败计数

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置供应商

编辑 `config/providers.yaml`，填入你的 API Key：

```yaml
providers:
  - name: "openveer"
    base_url: "https://api.openveer.com"
    api_key: "${OPENVEER_API_KEY}"   # 从环境变量读取
    timeout: 30
    enabled: true

  - name: "deepseek"
    base_url: "https://api.deepseek.com"
    api_key: "${DEEPSEEK_API_KEY}"
    timeout: 30
    enabled: true
```

在 `.env` 文件中填入真实 Key（不要提交到 Git）：

```
OPENVEER_API_KEY=sk-xxx
DEEPSEEK_API_KEY=sk-xxx
```

### 3. 启动服务

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## 使用方式

### 作为 OpenAI 客户端的后端

```python
from openai import OpenAI

client = OpenAI(
    api_key="any-key-here",   # 网关会忽略，使用配置的 key
    base_url="http://localhost:8000/v1"
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### cURL 测试

```bash
# 健康检查
curl http://localhost:8000/health

# 转发聊天请求
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Conversation-Id: my-chat-1" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'

# 流式请求
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

## 配置说明

### providers.yaml 结构

```yaml
providers:
  - name: "供应商名称"          # 唯一标识
    base_url: "https://api.xxx" # API 地址
    api_key: "${ENV_VAR}"      # 支持环境变量
    timeout: 30                # 超时秒数
    enabled: true              # 是否启用

routes:                        # Model 路由规则（可选）
  - pattern: "gpt-4*"          # 通配符匹配模型名
    provider_name: "openveer"  # 目标供应商
    priority: 100              # 优先级（越高越先匹配）

settings:
  failover_strategy: "failover"  # 策略：failover | conversation | round_robin
  request_timeout: 30
  log_level: "info"
  host: "0.0.0.0"
  port: 8000
```

### 故障切换流程

```
请求到达
    │
    ▼
┌─ Model 路由匹配？ ──── 是 ──→ 指定供应商
│                                │
否                           成功？── 是 ──→ 返回响应
│                                │
▼                                否 ──→ 记录失败，切换到下一个
策略选择供应商
    │
    ▼
发起请求
    │
    ▼
┌─ 成功？ ── 是 ──→ 保持当前供应商，返回响应
│                  │
否                  否 ──→ 记录失败，尝试下一个
│
▼
所有供应商均失败 → 返回 502
```

### 策略说明

| 策略 | 行为 | 适用场景 |
|------|------|---------|
| `failover` | 成功保持当前，失败才切换下一个 | 有明确主备关系时 |
| `conversation` | 每个会话固定绑定一个供应商 | 需要会话内一致性 |
| `round_robin` | 每次请求轮流使用 | 负载均衡 |

## 部署到服务器

```bash
# 克隆项目
git clone https://github.com/Keep-maker/api-failover-gateway.git
cd api-failover-gateway

# 创建虚拟环境并安装依赖
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 配置 API Key
cp .env.example .env
nano .env  # 填入真实 Key

# 使用 systemd 运行（可选）
cp api-gateway.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now api-gateway

# 查看状态
curl http://localhost:8000/health
```

## 项目结构

```
api-failover-gateway/
├── main.py                  # FastAPI 应用入口
├── config/
│   └── providers.yaml       # 供应商配置 + 路由规则
├── src/
│   ├── config.py            # 配置加载（支持 ${ENV} 展开）
│   └── failover.py          # 核心：FailoverClient + FailoverSelector
├── requirements.txt
├── .gitignore
└── outputs/
    └── README.md            # 部署说明
```

## 设计参考

核心故障切换逻辑借鉴自 [CodexPlusPlus](https://github.com/BigPizzaV3/CodexPlusPlus)：

- `RelayRotationSelector` → `FailoverSelector`
- `match_route_pattern` → `RouteRule.matches()`
- `member_relay_ready` → 请求前凭据校验
- 会话绑定、路由匹配、失败检测等设计思想

## License

MIT