catfishAPIAgg
这是一个简单、轻量级的 LLM API 聚合代理服务。

它允许您添加多个 API 后端（例如 OpenAI, Groq, Moonshot, Kimi 等），并根据优先级自动进行故障转移和重试。它内置了一个简单的前端管理面板，用于配置 API、查看统计和实时日志。

✨ 功能
多后端支持：配置多个 API 终端，支持任何兼容 OpenAI 格式的 API。

优先级 & 故障转移：按优先级自动尝试，失败后自动切换到下一个。

流式 & 非流式：透明支持 stream: true 和 stream: false。

工具调用 (Tool Calling)：透明转发 tools 和 tool_calls，结果完整传递。

Web UI 管理：内置前端面板，用于增删改查 API 配置。

统计 & 日志：实时查看请求成功/失败统计和内存日志。

Docker & CI/CD：使用 Docker Compose 一键部署，并自动构建 latest 镜像。

🚀 快速部署 (Docker)
您可以使用 Docker Compose 在您的服务器上快速部署。

步骤 1: 克隆仓库
登录到您的服务器，克隆本仓库并进入目录：

Bash

git clone https://github.com/Catfish872/catfishAPIAgg.git
cd catfishAPIAgg
步骤 2: (重要) 配置环境变量
仓库中包含一个 example.env 文件。您必须将其复制为 .env 文件才能使配置生效：

Bash

cp example.env .env
然后，编辑这个新的 .env 文件，填入您的自定义配置：

代码段

# --- 环境变量配置 ---

# 1. 设置您的管理员密钥 (用于登录管理面板)
ADMIN_KEY=your_very_secret_admin_key_12345

# 2. 设置您希望服务运行的端口 (例如 8001)
# 确保这个端口在您的服务器上是空闲的
PORT=8001
安全提示: .env 文件已被 .gitignore 忽略，您的密钥和配置不会被提交到 Git。

步骤 3: (首次部署) 公开您的镜像
GitHub Actions 会自动为您构建 Docker 镜像，但默认情况下它是私有的，您的服务器将无法拉取。

您需要手动将镜像设置为公开：

前往您的仓库主页 https://github.com/Catfish872/catfishAPIAgg。

在右侧栏点击 "Packages"。

点击 catfishapiagg 这个包。

点击 "Package settings" (包设置)。

在 "Danger Zone" 区域，点击 "Change visibility" (更改可见性) 并设置为 Public。

步骤 4: 启动服务
docker-compose.yml 文件已经配置为自动读取 .env 文件，并拉取您在 GHCR 上的 latest 镜像。

Bash

# 拉取最新的镜像
docker compose pull

# 在后台启动服务
docker compose up -d
服务现在已经在您指定的端口上运行了！

📖 如何使用
服务启动后 (假设您在 .env 中使用了端口 8001):

管理面板: 访问 http://<您的服务器IP>:8001

登录: 使用您在 .env 文件中设置的 ADMIN_KEY 作为密钥进行登录。

API 端点: 代理端点位于 http://<您的服务器IP>:8001/v1/chat/completions

登录后，您可以在 "API 配置" 选项卡中添加您的后端 API (例如 https://api.openai.com/v1) 及其密钥。
