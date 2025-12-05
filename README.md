# Catfish API Aggregator (catfishAPIAgg)

一个简单的、轻量级的 LLM (大型语言模型) API 聚合代理。它允许你添加多个 API 渠道，并根据优先级进行轮询和故障转移，同时提供一个统一的、与 OpenAI API 兼容的访问端点。

## ✨ 核心功能

- **多渠道聚合**: 支持添加和管理多个不同的 LLM API 供应商或 Key。
- **优先级与故障转移**: 根据设置的优先级顺序请求上游 API，如果一个失败，会自动尝试下一个。
- **OpenAI 兼容**: 提供与 OpenAI `v1/chat/completions` 完全兼容的代理端点，无缝对接现有应用。
- **动态管理**: 自带简单的 Web UI 和 RESTful API，可随时通过浏览器或 API 调用来增、删、改、查 API 配置。
- **请求统计**: 内置统计功能，可以查看总请求、当日请求的成功与失败次数，以及每个渠道的使用情况。
- **流式与非流式支持**: 完美支持流式（stream）和非流式（json）两种响应模式。
- **容器化部署**: 提供 `Dockerfile` 和 `docker-compose.yml`，一行命令即可轻松部署。

## ✨ v2.0 更新

-   **方案 (模型分组) 功能**
    现在，你可以将不同的 API 配置项组合成一个“方案”，并为该方案命名。在 API 请求时，通过 `model` 字段传递方案名，即可调用该方案下的资源池。如果请求的 `model` 未匹配到任何方案，系统将自动使用默认的第一个方案，增强了容错性。

-   **熔断机制**
    为每个 API 配置项增加了熔断功能。当一个渠道连续失败达到指定次数后，它将被自动禁用一段时间，避免在已知失效的渠道上浪费时间和请求，从而提高整体响应速度和稳定性。你可以在 Web 界面中为每个配置项单独设置。

-   **同优先级轮询**
    对于同一方案中具有相同优先级的多个配置项，系统现在会进行**轮询**调用，而不仅仅是按顺序从第一个开始。这实现了基础的负载均衡，确保所有同级资源都能被均匀使用。

-   **增强的统计视图**
    管理界面的统计页面现在新增了“今日按配置统计”，可以更清晰地查看当天每个渠道的详细成功/失败情况。

-   **健壮性与稳定性修复**
    -   修复了一个在处理高并发失败请求时可能导致服务死锁的严重问题，大幅提升了服务的稳定性。
    -   将默认的上游 API 请求超时时间从 30 秒延长至 90 秒，以更好地适应响应较慢的模型。

---

### ⚠️ 重要变更：API 调用方式

随着“方案”功能的引入，`model` 字段的用途发生了核心变化。

在旧版本中，`model` 字段会被透传或被配置项覆盖。**在新版本中，`model` 字段的核心作用是选择要使用的【方案】。**

#### 调用示例

假设你在管理界面中创建了一个名为 `gpt-4-pool` 的方案，并将你的 GPT-4 API Key 配置在了这个方案下。现在，你应该这样调用 API：

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your_secret_admin_key" \
  -d '{
    "model": "gpt-4-pool",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'```
在管理后台，将你的 API Keys 组织成不同的方案。
在调用 API 时，将 model 字段的值设置为你想要使用的方案名。
如果某个配置项设置了“覆盖 Model”，那么在选中该配置项后，发往上游的请求中 model 字段才会被替换。

## 🚀 部署指南

推荐使用 Docker Compose 进行部署，这是最简单快捷的方式。

### 方法一：使用 Docker Compose (推荐)

**前提条件**: 你需要预先安装 [Docker](https://www.docker.com/) 和 [Docker Compose](https://docs.docker.com/compose/install/)。

**步骤 1: 克隆项目**

```bash
git clone https://github.com/Catfish872/catfishAPIAgg.git
cd catfishAPIAgg
```

**步骤 2: 创建并配置 `.env` 文件**

项目通过 `.env` 文件加载配置。你需要将仓库中的 `example.env` 文件复制一份并重命名为 `.env`。

```bash
mv example.env .env
```

⚠️ **重要**: 修改 `.env` 文件，设置你自己的 `ADMIN_KEY`。这是一个用于访问管理后台和调用 API 的主密钥，请务必设置为一个复杂且安全的字符串。

```env
# .env 文件内容

# 访问管理后台和调用 API 的主密钥，请务必修改为一个复杂密码！
ADMIN_KEY=your_secret_admin_key

# 服务运行的端口
PORT=8080
```

**步骤 3: 启动服务**

在项目根目录下，执行以下命令：

```bash
docker-compose up -d
```

服务将在后台启动。你可以通过以下命令查看日志：

```bash
docker-compose logs -f
```

**步骤 4: 访问**

- **Web 管理界面**: `http://<你的服务器IP>:8080`
- **API 代理端点**: `http://<你的服务器IP>:8080/v1/chat/completions`

### 方法二：本地运行 (用于开发)

**前提条件**: 你需要安装 Python 3.8+。

**步骤 1: 克隆项目并安装依赖**

```bash
git clone https://github.com/Catfish872/catfishAPIAgg.git
cd catfishAPIAgg
pip install -r requirements.txt
```

**步骤 2: 配置环境变量**

与 Docker Compose 方法类似，你需要创建 `.env` 文件。或者，你可以在启动前直接在终端中设置环境变量。

```bash
# 1. 复制文件
mv example.env .env

# 2. 修改 .env 文件中的 ADMIN_KEY
# ... 在编辑器中打开并修改 ...

# 或者，直接在终端设置 (仅对当前会话有效)
export ADMIN_KEY='your_secret_admin_key'
export PORT='8080'
```

**步骤 3: 启动服务**

```bash
python main.py
```

服务将在前台启动，你可以直接在终端看到日志。

## ⚙️ 配置说明

项目通过环境变量进行配置：

- `ADMIN_KEY`: **(必需)** 你的主访问密钥。所有对 `/admin` 路径下管理 API 的请求，以及对 `/v1` 路径下代理 API 的请求，都必须在 HTTP Header 中包含 `Authorization: Bearer <你的ADMIN_KEY>`。
- `PORT`: **(可选, 默认 8080)** 服务监听的端口。在 Docker Compose 中，此端口会映射到宿主机。

## 💡 使用方法

### 1. 访问管理后台

部署成功后，通过浏览器访问 `http://<你的服务器IP>:8080`。

你会看到一个简单的管理界面，在这里你可以：
- **添加 API 配置**: 输入你的上游 API 地址 (如 `https://api.openai.com/v1`)、API Key、优先级（数字越小越优先）等。
- **查看和管理配置**: 列出所有已添加的配置，并可以进行修改和删除。
- **查看统计**: 查看 API 请求的统计数据。
- **查看日志**: 查看最新的内存日志。

### 2. 调用 API 代理

你可以像调用官方 OpenAI API 一样调用本服务的代理端点。

**端点**: `/v1/chat/completions`

**认证**: 在请求头 (Header) 中添加 `Authorization` 字段。

**示例**: 使用 `curl` 发起一个非流式请求。

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your_secret_admin_key" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

**注意**:
- 将 `your_secret_admin_key` 替换为你在 `.env` 文件中设置的 `ADMIN_KEY`。
- `model` 字段的值可以是你请求体中指定的任何模型。如果某个 API 配置项中指定了 `model` 覆盖，代理在请求该渠道时会自动替换。

## 🧩 API 端点 (管理)

所有管理端点都需要 `Authorization: Bearer <ADMIN_KEY>` 头进行认证。

| 方法   | 路径                  | 描述                 |
| ------ | --------------------- | -------------------- |
| `GET`  | `/admin/config`       | 获取所有 API 配置    |
| `POST` | `/admin/config`       | 创建一个新的 API 配置 |
| `PUT`  | `/admin/config/{id}`  | 更新指定的 API 配置   |
| `DELETE`| `/admin/config/{id}`  | 删除指定的 API 配置   |
| `GET`  | `/admin/stats`        | 获取请求统计数据     |
| `GET`  | `/admin/logs`         | 获取最新的内存日志   |

---
## 📄 许可证

本项目采用 [MIT License](LICENSE) 开源。
