import os
import asyncio
import httpx
import uvicorn
import collections
import aiofiles
import json
import uuid
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict
from itertools import groupby

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
# 设置一个合理的超时时间，例如 90 秒
httpx_client = httpx.AsyncClient(timeout=90.0)

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
    # [新增] 熔断机制相关配置
    consecutive_failure_threshold: Optional[int] = Field(None, description="连续失败N次后禁用（默认不启用）")
    disable_duration_seconds: Optional[int] = Field(None, description="禁用时长（秒，默认不启用）")


class ApiConfig(ApiConfigBase):
    """API 配置的完整模型 (包含 ID)"""
    id: str = Field(..., description="唯一的配置 ID")


class ApiConfigCreate(ApiConfigBase):
    """用于创建配置项的 Pydantic 模型，增加了 scheme_name"""
    scheme_name: str = Field("default", description="配置项所属的方案名称")


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
async def get_all_schemes() -> Dict[str, List[ApiConfig]]:
    """获取所有方案及其 API 配置"""
    configs_data = await read_json_file(CONFIG_FILE, {})

    # [新增] 向后兼容逻辑：如果读到的是列表（旧格式），自动转换为带 "default" 方案的字典
    if isinstance(configs_data, list):
        log_message("检测到旧版配置文件格式（列表），自动迁移到方案格式 `{'default': ...}`")
        configs_data = {"default": configs_data}
        # 将迁移后的结果写回文件
        await write_json_file(CONFIG_FILE, configs_data)

    schemes = {}
    for scheme_name, configs_list in configs_data.items():
        configs = [ApiConfig(**data) for data in configs_list]
        # 优先级数字越小越靠前
        configs.sort(key=lambda x: x.priority)
        schemes[scheme_name] = configs

    return schemes


async def save_all_schemes(schemes: Dict[str, List[ApiConfig]]):
    """保存所有方案配置"""
    schemes_data = {
        scheme_name: [config.dict() for config in configs]
        for scheme_name, configs in schemes.items()
    }
    await write_json_file(CONFIG_FILE, schemes_data)


# 统计相关的 I/O
def get_default_stats():
    """获取默认的统计数据结构"""
    return {
        "total": {"success": 0, "fail": 0},
        "today": {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "success": 0,
            "fail": 0,
            "by_config_id": {}  # [新增] 今日按配置统计
        },
        "by_config_id": {},
        "round_robin_state": {}  # [新增] 轮询状态
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
            "fail": 0,
            "by_config_id": {}  # [新增] 重置今日按配置统计
        }

        # 清理不存在的 config (保留原有逻辑)
        all_schemes = await get_all_schemes()
        config_ids = {c.id for scheme in all_schemes.values() for c in scheme}
        stats["by_config_id"] = {
            cid: data for cid, data in stats.get("by_config_id", {}).items() if cid in config_ids
        }
        await write_json_file(STATS_FILE, stats)

    return stats


async def update_stats_and_state(
        config: ApiConfig,
        is_success: bool,
        scheme_name: str,
        priority_group: List[ApiConfig],
        success_index_in_group: int
):
    """
    [修复] 更新统计数据、熔断状态和轮询状态 (无死锁版本)
    """
    async with file_lock:
        # --- 1. 直接在锁内进行无锁的文件读取 ---
        default_data = get_default_stats()
        stats = default_data
        try:
            if os.path.exists(STATS_FILE):
                async with aiofiles.open(STATS_FILE, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    if content:
                        stats = json.loads(content)
            else:
                # 文件不存在，使用默认值并尝试写入
                async with aiofiles.open(STATS_FILE, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(default_data, indent=2))
        except Exception as e:
            log_message(f"在 update_stats_and_state 中读/创建 {STATS_FILE} 失败: {e}. 使用默认值。")
            stats = default_data

        # --- 2. 直接在锁内进行日期检查 ---
        today_str = datetime.now().strftime("%Y-%m-%d")
        if stats.get("today", {}).get("date") != today_str:
            stats["today"] = {
                "date": today_str,
                "success": 0,
                "fail": 0,
                "by_config_id": {}
            }

        # --- 3. 更新统计数据 ---
        key = "success" if is_success else "fail"

        stats["total"][key] = stats["total"].get(key, 0) + 1
        stats["today"][key] = stats["today"].get(key, 0) + 1

        if "by_config_id" not in stats: stats["by_config_id"] = {}
        if config.id not in stats["by_config_id"]:
            stats["by_config_id"][config.id] = {"success": 0, "fail": 0, "consecutive_fails": 0}

        if "by_config_id" not in stats["today"]: stats["today"]["by_config_id"] = {}
        if config.id not in stats["today"]["by_config_id"]:
            stats["today"]["by_config_id"][config.id] = {"success": 0, "fail": 0}

        stats["by_config_id"][config.id][key] = stats["by_config_id"][config.id].get(key, 0) + 1
        stats["today"]["by_config_id"][config.id][key] = stats["today"]["by_config_id"][config.id].get(key, 0) + 1

        # --- 4. 更新熔断状态 ---
        config_stats = stats["by_config_id"][config.id]
        if is_success:
            config_stats["consecutive_fails"] = 0
            if "disabled_until" in config_stats:
                del config_stats["disabled_until"]
        else:
            current_fails = config_stats.get("consecutive_fails", 0) + 1
            config_stats["consecutive_fails"] = current_fails

            threshold = config.consecutive_failure_threshold
            duration = config.disable_duration_seconds
            if threshold is not None and duration is not None and current_fails >= threshold:
                disabled_until_time = datetime.now() + timedelta(seconds=duration)
                config_stats["disabled_until"] = disabled_until_time.isoformat()
                log_message(
                    f"熔断触发: 配置项 ID {config.id} 已被禁用，直到 {disabled_until_time.strftime('%Y-%m-%d %H:%M:%S')}")

        # --- 5. 仅在成功时更新轮询状态 ---
        if is_success:
            if "round_robin_state" not in stats: stats["round_robin_state"] = {}
            if scheme_name not in stats["round_robin_state"]:
                stats["round_robin_state"][scheme_name] = {}

            next_index = (success_index_in_group + 1) % len(priority_group) if priority_group else 0
            stats["round_robin_state"][scheme_name][str(config.priority)] = next_index

        # --- 6. 直接在锁内进行无锁的文件写入 ---
        try:
            async with aiofiles.open(STATS_FILE, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(stats, indent=2, ensure_ascii=False))
        except Exception as e:
            log_message(f"在 update_stats_and_state 中写入 {STATS_FILE} 失败: {e}")


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
    [重构] 提供一个模型列表端点，返回所有方案的名称。
    """
    schemes = await get_all_schemes()
    model_ids = sorted(list(schemes.keys()))

    model_data = []
    for model_id in model_ids:
        model_data.append({
            "id": model_id,
            "object": "model",
            "created": 1,
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
    [重构] OpenAI /v1/chat/completions 代理端点。
    它会根据方案、熔断、轮询和优先级进行故障转移。
    """
    try:
        request_body = await request.json()
    except Exception:
        log_message("请求体 JSON 解析失败")
        return JSONResponse(status_code=400, content={"error": "无效的 JSON 请求体"})

    is_stream = request_body.get("stream", False)
    requested_model = request_body.get("model")

    # --- 1. 获取方案配置 ---
    all_schemes = await get_all_schemes()
    if not all_schemes:
        log_message("代理失败: 没有任何 API 配置")
        return JSONResponse(status_code=500, content={"error": "没有配置可用的 API 后端"})

    target_scheme_configs = all_schemes.get(requested_model)
    scheme_name = requested_model

    if not target_scheme_configs:
        # [调整] 如果找不到模型（方案），默认使用第一个方案
        sorted_scheme_names = sorted(list(all_schemes.keys()))
        scheme_name = sorted_scheme_names[0]
        target_scheme_configs = all_schemes[scheme_name]
        log_message(f"模型 '{requested_model}' 未找到对应的方案，默认使用第一个方案 '{scheme_name}'")

    # --- 2. 构建尝试队列 (熔断、轮询) ---
    stats = await get_stats()
    now_time = datetime.now()

    # 过滤掉被熔断的配置
    active_configs = []
    for config in target_scheme_configs:
        config_stats = stats.get("by_config_id", {}).get(config.id, {})
        disabled_until_str = config_stats.get("disabled_until")
        if disabled_until_str:
            disabled_until_time = datetime.fromisoformat(disabled_until_str)
            if now_time < disabled_until_time:
                log_message(f"配置项 ID: {config.id} 当前被熔断禁用，跳过。")
                continue
        active_configs.append(config)

    if not active_configs:
        log_message(f"方案 '{scheme_name}' 中的所有配置项都处于熔断状态。")
        return JSONResponse(status_code=503, content={"error": "所有后端服务当前都不可用"})

    # 按优先级分组并进行轮询排序
    attempt_queue = []
    priority_groups = {k: list(g) for k, g in groupby(active_configs, key=lambda c: c.priority)}

    round_robin_state_for_scheme = stats.get("round_robin_state", {}).get(scheme_name, {})

    for priority in sorted(priority_groups.keys()):
        group = priority_groups[priority]
        next_index = round_robin_state_for_scheme.get(str(priority), 0)

        # 确保 next_index 不会越界
        if next_index >= len(group):
            next_index = 0

        # 轮询排序
        reordered_group = group[next_index:] + group[:next_index]
        attempt_queue.extend(reordered_group)

    # --- 3. 循环尝试队列 ---
    last_error = None
    last_error_response = None

    for config in attempt_queue:
        log_message(f"正在尝试方案 '{scheme_name}' 的配置项 ID: {config.id} (Priority: {config.priority})")

        # 查找原始分组信息，用于成功后更新轮询状态
        original_group = priority_groups.get(config.priority, [])
        try:
            success_index_in_group = original_group.index(config)
        except ValueError:
            success_index_in_group = 0  # 理论上不会发生

        # 准备请求
        proxy_url = f"{config.url.rstrip('/')}/chat/completions"
        proxy_headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json" if not is_stream else "text/event-stream"
        }

        proxy_body = request_body.copy()
        if config.model:
            proxy_body["model"] = config.model

        response_context = None
        try:
            if is_stream:
                response_context = httpx_client.stream("POST", proxy_url, headers=proxy_headers, json=proxy_body)
                response = await response_context.__aenter__()

                if response.status_code >= 400:
                    response_body = await response.aread()
                    error_text = response_body.decode('utf-8')
                    log_message(f"配置项 ID: {config.id} 失败 (HTTP {response.status_code}): {error_text}")
                    last_error = f"HTTP {response.status_code}: {error_text}"
                    try:
                        error_content = json.loads(error_text)
                    except Exception:
                        error_content = error_text

                    class MockResponse:
                        def __init__(self, content, status_code_val):
                            self._content, self.status_code = content, status_code_val

                        def json(self): return self._content if isinstance(self._content, dict) else {
                            "error": self.text}

                        @property
                        def text(self): return self._content if isinstance(self._content, str) else str(self._content)

                    last_error_response = MockResponse(error_content, response.status_code)
                    await update_stats_and_state(config, False, scheme_name, [], 0)
                    await response_context.__aexit__(None, None, None)
                    response_context = None
                    continue

                async def final_stream_generator(successful_config, ctx, resp):
                    try:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                        log_message(f"配置项 ID: {successful_config.id} 流式请求成功 (流结束)")
                        await update_stats_and_state(successful_config, True, scheme_name, original_group,
                                                     success_index_in_group)
                    except Exception as e:
                        error_type = type(e).__name__
                        if "ClientDisconnect" in error_type or "CancelledError" in error_type:
                            log_message(
                                f"配置项 ID: {successful_config.id} 流传输被客户端主动断开 (Type: {error_type})")
                        else:
                            log_message(f"配置项 ID: {successful_config.id} 在流传输过程中失败: {repr(e)}")
                        raise
                    finally:
                        await ctx.__aexit__(None, None, None)

                log_message(f"配置项 ID: {config.id} 流式请求启动成功 (HTTP {response.status_code})")
                response_context_to_pass = response_context
                response_context = None
                return StreamingResponse(
                    final_stream_generator(config, response_context_to_pass, response),
                    media_type="text/event-stream"
                )
            else:
                response = await httpx_client.post(proxy_url, headers=proxy_headers, json=proxy_body)
                response.raise_for_status()
                log_message(f"配置项 ID: {config.id} 非流式请求成功")
                await update_stats_and_state(config, True, scheme_name, original_group, success_index_in_group)
                return JSONResponse(content=response.json(), status_code=response.status_code)

        except httpx.HTTPStatusError as e:
            last_error, last_error_response = e, e.response
            log_message(f"配置项 ID: {config.id} 失败 (HTTP {e.response.status_code}): {e.response.text}")
            await update_stats_and_state(config, False, scheme_name, [], 0)
        except httpx.RequestError as e:
            last_error = e
            log_message(f"配置项 ID: {config.id} 失败 (RequestError): {e}")
            await update_stats_and_state(config, False, scheme_name, [], 0)
        except Exception as e:
            last_error = e
            log_message(f"配置项 ID: {config.id} 失败 (Exception): {e}")
            await update_stats_and_state(config, False, scheme_name, [], 0)
        finally:
            if response_context is not None:
                await response_context.__aexit__(None, None, None)

    log_message("所有配置项均尝试失败")
    if last_error_response is not None:
        try:
            error_content = last_error_response.json()
        except Exception:
            error_content = last_error_response.text
        return JSONResponse(content=error_content, status_code=last_error_response.status_code)

    return JSONResponse(status_code=500, content={"error": f"所有后端均失败。最后错误: {str(last_error)}"})


# --- 6. 管理 API (带认证) ---

admin_router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(verify_key)]
)


@admin_router.get("/config", response_model=Dict[str, List[ApiConfig]])
async def get_all_configs():
    """[重构] 获取所有方案及其配置项"""
    return await get_all_schemes()


@admin_router.post("/config", response_model=ApiConfig)
async def create_config(config_in: ApiConfigCreate):
    """[重构] 创建一条新的 API 配置项并指定方案"""
    schemes = await get_all_schemes()
    scheme_name = config_in.scheme_name

    if scheme_name not in schemes:
        schemes[scheme_name] = []

    config_data = config_in.dict(exclude={"scheme_name"})
    new_config = ApiConfig(id=str(uuid.uuid4()), **config_data)
    schemes[scheme_name].append(new_config)

    await save_all_schemes(schemes)
    log_message(f"管理: 在方案 '{scheme_name}' 中创建了新的配置项 {new_config.id}")
    return new_config


@admin_router.put("/config/{config_id}", response_model=ApiConfig)
async def update_config(config_id: str, config_in: ApiConfigBase):
    """[重构] 更新指定的 API 配置项"""
    schemes = await get_all_schemes()
    updated_config = None

    for scheme_name, configs in schemes.items():
        for i, config in enumerate(configs):
            if config.id == config_id:
                updated_config = config.copy(update=config_in.dict(exclude_unset=True))
                schemes[scheme_name][i] = updated_config
                break
        if updated_config:
            break

    if not updated_config:
        raise HTTPException(status_code=404, detail="未找到该配置项")

    await save_all_schemes(schemes)
    log_message(f"管理: 更新了配置项 {config_id}")
    return updated_config


@admin_router.delete("/config/{config_id}", status_code=204)
async def delete_config(config_id: str):
    """[重构] 删除指定的 API 配置项"""
    schemes = await get_all_schemes()
    found = False

    for scheme_name in list(schemes.keys()):
        original_len = len(schemes[scheme_name])
        schemes[scheme_name] = [c for c in schemes[scheme_name] if c.id != config_id]
        if len(schemes[scheme_name]) < original_len:
            found = True
            # 如果方案变为空，则删除该方案
            if not schemes[scheme_name]:
                del schemes[scheme_name]
            break

    if not found:
        raise HTTPException(status_code=404, detail="未找到该配置项")

    await save_all_schemes(schemes)
    log_message(f"管理: 删除了配置项 {config_id}")
    return


@admin_router.get("/stats")
async def get_statistics():
    """获取请求统计数据 (包含日期重置逻辑)"""
    return await get_stats()


@admin_router.get("/logs")
async def get_logs() -> List[str]:
    """获取最新的 200 条内存日志"""
    return list(log_deque)


app.include_router(admin_router)


# --- 7. 启动和关闭事件 ---

@app.on_event("startup")
async def startup_event():
    """应用启动时执行"""
    if not ADMIN_KEY:
        log_message("=" * 50)
        log_message("!!! 严重警告: 环境变量 'ADMIN_KEY' 未设置 !!!")
        log_message("!!! 服务已启动, 但所有 API 请求都将因 401/500 错误而失败 !!!")
        log_message("=" * 50)
    else:
        log_message(f"服务启动，ADMIN_KEY 已加载。")

    log_message("正在初始化配置文件...")
    await get_all_schemes()
    await get_stats()
    log_message(f"{PROJECT_NAME} 已启动，监听端口 {PORT}")


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时执行"""
    await httpx_client.aclose()
    log_message(f"{PROJECT_NAME} 正在关闭")


# --- 8. 静态文件服务 (用于前端) ---

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
