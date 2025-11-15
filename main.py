import os
import asyncio
import httpx
import uvicorn
import collections
import aiofiles
import json
import uuid
from datetime import datetime
from pydantic import BaseModel, Field
from typing import List, Optional, Any

from fastapi import FastAPI, Request, HTTPException, Depends, Header, APIRouter
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# --- 1. 全局配置和初始化 ---

# 项目配置
PROJECT_NAME = "catfishAPIAgg"
API_VERSION = "v1"

# 从环境变量读取配置
# 你的主访问密钥，必须在启动时设置
ADMIN_KEY = os.environ.get("ADMIN_KEY")
# 服务端口
PORT = int(os.environ.get("PORT", 8080))
# 数据目录
DATA_DIR = "data"
# 配置文件路径
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
# 统计文件路径
STATS_FILE = os.path.join(DATA_DIR, "stats.json")

# 确保数据目录存在
os.makedirs(DATA_DIR, exist_ok=True)

# 内存日志 (deque 是线程/异步安全的)
log_deque = collections.deque(maxlen=200)

# 异步文件读写锁
file_lock = asyncio.Lock()

# 全局 httpx 客户端 (用于连接池)
# 设置一个合理的超时时间，例如 30 秒
httpx_client = httpx.AsyncClient(timeout=30.0)

# FastAPI 应用实例
app = FastAPI(
    title=PROJECT_NAME,
    version=API_VERSION,
    description="一个简单的 LLM API 聚合代理"
)


# --- 2. Pydantic 数据模型 ---

class ApiConfigBase(BaseModel):
    """API 配置的基础模型 (用于创建/更新)"""
    priority: int = Field(..., description="优先级，数字越小越优先")
    url: str = Field(..., description="API 终端地址, e.g., https://api.openai.com/v1")
    api_key: str = Field(..., description="用于该终端的 API Key")
    model: Optional[str] = Field(None, description="要覆盖的模型名称，如果为 null/空，则使用原始请求中的 model")


class ApiConfig(ApiConfigBase):
    """API 配置的完整模型 (包含 ID)"""
    id: str = Field(..., description="唯一的配置 ID")


# --- 3. 辅助函数 (日志, JSON I/O, 统计) ---

def log_message(message: str):
    """向内存日志队列中添加一条日志"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_deque.append(f"[{now}] {message}")
    print(message)  # 同时也打印到控制台


async def read_json_file(file_path: str, default_data: Any) -> Any:
    """带锁读取 JSON 文件，如果文件不存在则创建并返回默认值"""
    async with file_lock:
        if not os.path.exists(file_path):
            try:
                async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(default_data, indent=2))
                return default_data
            except Exception as e:
                log_message(f"创建 JSON 文件 {file_path} 失败: {e}")
                return default_data

        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                content = await f.read()
                return json.loads(content)
        except Exception as e:
            log_message(f"读取 JSON 文件 {file_path} 失败: {e}. 返回默认值。")
            return default_data


async def write_json_file(file_path: str, data: Any):
    """带锁写入 JSON 文件"""
    async with file_lock:
        try:
            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:
            log_message(f"写入 JSON 文件 {file_path} 失败: {e}")


# 配置相关的 I/O
async def get_configs() -> List[ApiConfig]:
    """获取所有 API 配置，并按优先级排序"""
    configs_data = await read_json_file(CONFIG_FILE, [])
    configs = [ApiConfig(**data) for data in configs_data]
    # 优先级数字越小越靠前
    configs.sort(key=lambda x: x.priority)
    return configs


async def save_configs(configs: List[ApiConfig]):
    """保存所有 API 配置"""
    configs_data = [config.dict() for config in configs]
    await write_json_file(CONFIG_FILE, configs_data)


# 统计相关的 I/O
def get_default_stats():
    """获取默认的统计数据结构"""
    return {
        "total": {"success": 0, "fail": 0},
        "today": {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "success": 0,
            "fail": 0
        },
        "by_config_id": {}
    }


async def get_stats() -> dict:
    """获取统计数据，并处理日期重置"""
    stats = await read_json_file(STATS_FILE, get_default_stats())

    # 检查日期是否是今天，如果不是，重置 today 并更新日期
    today_str = datetime.now().strftime("%Y-%m-%d")
    if stats.get("today", {}).get("date") != today_str:
        stats["today"] = {
            "date": today_str,
            "success": 0,
            "fail": 0
        }
        # 顺便清理一下 by_config_id 中不存在的 config
        all_configs = await get_configs()
        config_ids = {c.id for c in all_configs}
        stats["by_config_id"] = {
            cid: data for cid, data in stats.get("by_config_id", {}).items() if cid in config_ids
        }
        await write_json_file(STATS_FILE, stats)

    return stats


async def update_stats(config_id: str, is_success: bool):
    """更新统计数据"""
    # 我们使用一个锁来保证读-改-写的原子性
    async with file_lock:

        # 1. 读取 (使用内部"不加锁"的逻辑，避免重入死锁)
        # --- (这是从 read_json_file 复制并移除锁的逻辑) ---
        default_data = get_default_stats()
        stats = default_data  # 默认为 default_data

        if not os.path.exists(STATS_FILE):
            try:
                # 尝试创建默认文件
                async with aiofiles.open(STATS_FILE, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(default_data, indent=2))
                stats = default_data
            except Exception as e:
                log_message(f"创建 JSON 文件 {STATS_FILE} 失败 (在 update_stats 中): {e}")
                stats = default_data
        else:
            try:
                # 尝试读取现有文件
                async with aiofiles.open(STATS_FILE, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    stats = json.loads(content)
            except Exception as e:
                log_message(f"读取 JSON 文件 {STATS_FILE} 失败 (在 update_stats 中): {e}. 返回默认值。")
                stats = default_data
        # --- (不加锁的读取逻辑结束) ---

        # 2. 检查日期
        today_str = datetime.now().strftime("%Y-%m-%d")
        if stats.get("today", {}).get("date") != today_str:
            stats["today"] = {
                "date": today_str,
                "success": 0,
                "fail": 0
            }

        # 3. 修改
        key = "success" if is_success else "fail"

        stats["total"][key] = stats["total"].get(key, 0) + 1
        stats["today"][key] = stats["today"].get(key, 0) + 1

        if config_id not in stats["by_config_id"]:
            stats["by_config_id"][config_id] = {"success": 0, "fail": 0}
        stats["by_config_id"][config_id][key] = stats["by_config_id"][config_id].get(key, 0) + 1

        # 4. 写入 (使用内部"不加锁"的逻辑)
        try:
            async with aiofiles.open(STATS_FILE, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(stats, indent=2, ensure_ascii=False))
        except Exception as e:
            log_message(f"写入 STATS_FILE 失败: {e}")


# --- 4. 认证依赖 ---

async def verify_key(authorization: str = Header(..., description="认证密钥，格式: Bearer YOUR_ADMIN_KEY")):
    """依赖项：验证 ADMIN_KEY"""
    if not ADMIN_KEY:
        log_message("!!! 严重错误: ADMIN_KEY 未设置, 所有请求都将失败 !!!")
        raise HTTPException(status_code=500, detail="服务器内部错误: 认证未配置")

    if authorization != f"Bearer {ADMIN_KEY}":
        log_message(f"认证失败: 提供的 Key {authorization} 不正确")
        raise HTTPException(status_code=401, detail="无效的认证密钥")
    return True


# --- 5. 核心代理端点 ---
@app.get("/v1", tags=["Proxy"])
async def v1_root_check():
    """
    一个简单的端点，用于响应对 /v1 根路径的 GET 请求。
    """
    return {"status": "ok", "message": f"{PROJECT_NAME} API {API_VERSION} is running."}


@app.get("/v1/models", tags=["Proxy"])
async def get_models(auth: bool = Depends(verify_key)):
    """
    提供一个模型列表端点，返回配置中所有不重复的模型名称。
    """
    configs = await get_configs()

    # 收集所有配置中明确指定的、不重复的模型名称
    model_ids = set()
    for config in configs:
        if config.model:
            model_ids.add(config.model)

    # 格式化为 OpenAI API 兼容的格式
    model_data = []
    sorted_model_ids = sorted(list(model_ids))  # 排序以保证每次返回顺序一致

    for model_id in sorted_model_ids:
        model_data.append({
            "id": model_id,
            "object": "model",
            "created": 1,  # 使用一个静态时间戳
            "owned_by": "catfishapiagg",
        })

    return {
        "object": "list",
        "data": model_data,
    }


@app.post("/v1/chat/completions", tags=["Proxy"])
async def proxy_chat_completions(
        request: Request,
        auth: bool = Depends(verify_key)
):
    """
    OpenAI /v1/chat/completions 代理端点。
    它会根据配置的优先级进行轮询和故障转移。
    """
    try:
        request_body = await request.json()
    except Exception:
        log_message("请求体 JSON 解析失败")
        return JSONResponse(status_code=400, content={"error": "无效的 JSON 请求体"})

    is_stream = request_body.get("stream", False)
    configs = await get_configs()

    if not configs:
        log_message("代理失败: 没有任何 API 配置")
        return JSONResponse(status_code=500, content={"error": "没有配置可用的 API 后端"})

    last_error = None
    last_error_response = None

    for config in configs:
        log_message(f"正在尝试配置项 ID: {config.id} (Priority: {config.priority})")

        # 准备请求
        proxy_url = f"{config.url.rstrip('/')}/chat/completions"
        proxy_headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json" if not is_stream else "text/event-stream"
        }

        # 复制请求体并替换 model (如果需要)
        proxy_body = request_body.copy()
        if config.model:
            proxy_body["model"] = config.model

        response_context = None  # 用于流式传输的上下文
        try:
            if is_stream:
                # --- 处理流式请求 ---

                # 1. 准备流上下文
                response_context = httpx_client.stream(
                    "POST",
                    proxy_url,
                    headers=proxy_headers,
                    json=proxy_body
                )

                # 2. 异步进入上下文 (建立连接并获取头)
                response = await response_context.__aenter__()

                # 3. 检查 HTTP 状态码
                if response.status_code >= 400:
                    # 这是一个 HTTP 错误 (例如 401)
                    # 读取错误体
                    response_body = await response.aread()
                    error_text = response_body.decode('utf-8')
                    log_message(f"配置项 ID: {config.id} 失败 (HTTP {response.status_code}): {error_text}")

                    # 记录最后错误
                    last_error = f"HTTP {response.status_code}: {error_text}"
                    try:
                        error_content = json.loads(error_text)
                    except Exception:
                        error_content = error_text

                    # 定义一个简单的类来模拟 httpx.Response 的部分接口
                    class MockResponse:
                        def __init__(self, content, status_code_val):
                            self._content = content
                            self.status_code = status_code_val

                        def json(self):
                            if isinstance(self._content, dict):
                                return self._content
                            try:
                                return json.loads(self.text)
                            except Exception:
                                return {"error": self.text}

                        @property
                        def text(self):
                            if isinstance(self._content, str):
                                return self._content
                            return str(self._content)

                    last_error_response = MockResponse(error_content, response.status_code)

                    await update_stats(config.id, is_success=False)

                    # 在 continue 之前必须关闭 response
                    await response_context.__aexit__(None, None, None)
                    response_context = None  # 标记为已关闭

                    continue  # <--- 关键：失败了，尝试下一个 config

                # 4. 如果状态码是成功的 (2xx)

                async def final_stream_generator(config_id_success, ctx, resp):
                    """
                    这个生成器负责迭代数据块，并在最后 (或异常时)
                    正确地关闭 httpx 上下文。
                    """
                    try:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                        # 只有在循环正常结束后 (没抛异常) 才算成功
                        log_message(f"配置项 ID: {config_id_success} 流式请求成功 (流结束)")
                        await update_stats(config_id_success, is_success=True)
                    except Exception as e:
                        # 捕获流传输过程中的错误
                        # --- [诊断日志] ---
                        # 改进日志：打印异常类型和 repr()，以便捕获空消息异常
                        error_type = type(e).__name__
                        error_repr = repr(e)

                        if error_type == "CancelledError" or "ClientDisconnect" in error_type:
                            log_message(f"配置项 ID: {config_id_success} 流传输被客户端主动断开 (Type: {error_type})")
                        else:
                            log_message(
                                f"配置项 ID: {config_id_success} 在流传输过程中失败: [Type: {error_type}, Repr: {error_repr}]")
                        # --- [诊断日志结束] ---

                        # 重新抛出异常，让 FastAPI 知道连接已断开
                        raise
                    finally:
                        # 无论如何都要关闭上下文
                        await ctx.__aexit__(None, None, None)

                log_message(f"配置项 ID: {config.id} 流式请求启动成功 (HTTP {response.status_code})")

                # 标记 response_context 将由生成器管理，防止外层 try/except/finally 再次关闭它
                response_context_to_pass = response_context
                response_context = None

                return StreamingResponse(
                    final_stream_generator(config.id, response_context_to_pass, response),
                    media_type="text/event-stream"
                )

            else:
                # --- 处理非流式请求 --- (保持不变)
                response = await httpx_client.post(
                    proxy_url,
                    headers=proxy_headers,
                    json=proxy_body
                )

                # 检查错误，如果失败则会引发 HTTPStatusError
                response.raise_for_status()

                # 成功
                log_message(f"配置项 ID: {config.id} 非流式请求成功")
                await update_stats(config.id, is_success=True)
                return JSONResponse(content=response.json(), status_code=response.status_code)

        except httpx.HTTPStatusError as e:
            # (这个只会在非流式请求中被捕获)
            last_error = e
            last_error_response = e.response
            log_message(f"配置项 ID: {config.id} 失败 (HTTP {e.response.status_code}): {e.response.text}")
            await update_stats(config.id, is_success=False)
        except httpx.RequestError as e:
            # (这个会在流式 (连接失败) 和非流式 (连接失败) 中被捕获)
            last_error = e
            log_message(f"配置项 ID: {config.id} 失败 (RequestError): {e}")
            await update_stats(config.id, is_success=False)
        except Exception as e:
            # (其他未知错误)
            last_error = e
            log_message(f"配置项 ID: {config.id} 失败 (Exception): {e}")
            await update_stats(config.id, is_success=False)

        finally:
            # 如果 response_context 仍存在 (意味着 __aenter__ 失败或未被正确处理)
            # 确保它被关闭。
            if response_context is not None:
                await response_context.__aexit__(None, None, None)

    # --- 所有配置都已尝试 ---
    log_message("所有配置项均尝试失败")

    # 按要求：直接返回原始的最终错误信息
    if last_error_response is not None:
        try:
            # 尝试解析 JSON 错误体
            error_content = last_error_response.json()
        except Exception:
            # 如果不是 JSON，返回原始文本
            error_content = last_error_response.text
        return JSONResponse(content=error_content, status_code=last_error_response.status_code)

    # 如果是连接错误等没有 response 的情况
    return JSONResponse(
        status_code=500,
        content={"error": f"所有后端均失败。最后错误: {str(last_error)}"}
    )

# --- 6. 管理 API (带认证) ---

admin_router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(verify_key)]  # 此路由组下的所有端点都需要认证
)


@admin_router.get("/config", response_model=List[ApiConfig])
async def get_all_configs():
    """获取所有 API 配置项"""
    return await get_configs()


@admin_router.post("/config", response_model=ApiConfig)
async def create_config(config_in: ApiConfigBase):
    """创建一条新的 API 配置项"""
    configs = await get_configs()
    new_config = ApiConfig(id=str(uuid.uuid4()), **config_in.dict())
    configs.append(new_config)
    await save_configs(configs)
    log_message(f"管理: 创建了新的配置项 {new_config.id}")
    return new_config


@admin_router.put("/config/{config_id}", response_model=ApiConfig)
async def update_config(config_id: str, config_in: ApiConfigBase):
    """更新指定的 API 配置项"""
    configs = await get_configs()
    config_to_update = None
    for i, config in enumerate(configs):
        if config.id == config_id:
            config_to_update = config
            updated_config = config.copy(update=config_in.dict())
            configs[i] = updated_config
            break

    if not config_to_update:
        raise HTTPException(status_code=404, detail="未找到该配置项")

    await save_configs(configs)
    log_message(f"管理: 更新了配置项 {config_id}")
    return updated_config


@admin_router.delete("/config/{config_id}", status_code=204)
async def delete_config(config_id: str):
    """删除指定的 API 配置项"""
    configs = await get_configs()
    original_len = len(configs)
    configs = [config for config in configs if config.id != config_id]

    if len(configs) == original_len:
        raise HTTPException(status_code=404, detail="未找到该配置项")

    await save_configs(configs)
    log_message(f"管理: 删除了配置项 {config_id}")
    return


@admin_router.get("/stats")
async def get_statistics():
    """获取请求统计数据 (包含日期重置逻辑)"""
    # log_message("管理: 请求统计数据")
    return await get_stats()


@admin_router.get("/logs")
async def get_logs() -> List[str]:
    """获取最新的 200 条内存日志"""
    # log_message("管理: 请求内存日志")
    return list(log_deque)


# 将管理路由组添加到主应用
app.include_router(admin_router)


# --- 7. 启动和关闭事件 ---

@app.on_event("startup")
async def startup_event():
    """应用启动时执行"""
    # 检查 ADMIN_KEY 是否设置
    if not ADMIN_KEY:
        log_message("=" * 50)
        log_message("!!! 严重警告: 环境变量 'ADMIN_KEY' 未设置 !!!")
        log_message("!!! 服务已启动, 但所有 API 请求都将因 401/500 错误而失败 !!!")
        log_message("=" * 50)
    else:
        log_message(f"服务启动，ADMIN_KEY 已加载。")

    # 尝试读取/创建配置文件，确保它们是可写的
    log_message("正在初始化配置文件...")
    await get_configs()
    await get_stats()
    log_message(f"{PROJECT_NAME} 已启动，监听端口 {PORT}")


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时执行"""
    await httpx_client.aclose()
    log_message(f"{PROJECT_NAME} 正在关闭")


# --- 8. 静态文件服务 (用于前端) ---

# 挂载 static 目录
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", tags=["Frontend"])
async def read_index():
    """提供前端主页"""
    index_path = "static/index.html"
    if not os.path.exists(index_path):
        log_message("前端文件 'static/index.html' 未找到")
        return JSONResponse(status_code=404, content={"error": "前端文件未找到"})
    return FileResponse(index_path)


# --- 9. 本地开发运行 ---

if __name__ == "__main__":
    if not ADMIN_KEY:
        print("=" * 50)
        print("!!! 启动警告: 环境变量 'ADMIN_KEY' 未设置 !!!")
        print("!!! 请在启动前设置: export ADMIN_KEY='your_secret_key' !!!")
        print("!!! 为方便测试，将使用 'admin' 作为临时密钥 !!!")
        print("=" * 50)
        ADMIN_KEY = "admin"

    print(f"--- 正在以开发模式启动 {PROJECT_NAME} ---")
    print(f"--- 管理密钥 (ADMIN_KEY): {ADMIN_KEY} ---")
    print(f"--- 访问 http://0.0.0.0:{PORT} ---")

    uvicorn.run(app, host="0.0.0.0", port=PORT)
