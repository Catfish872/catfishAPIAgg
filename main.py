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
from typing import List, Optional, Any, Dict, Literal
from itertools import groupby

from fastapi import FastAPI, Request, HTTPException, Depends, Header, APIRouter
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

from endpoint_presets import (
    EndpointPresetError,
    build_images_generations_payload,
    normalize_image_response_urls,
    wrap_image_response_as_chat_completion,
)
from anthropic_adapter import (
    AnthropicAdapterError,
    OpenAIToAnthropicSSEConverter,
    anthropic_error,
    anthropic_to_openai_chat_request,
    openai_chat_to_anthropic_response,
    openai_error_to_anthropic,
    openai_json_to_anthropic_stream,
)

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
# 生成图片文件目录与对外访问路径
GENERATED_IMAGES_DIR = os.path.join(DATA_DIR, "generated_images")
GENERATED_IMAGES_ROUTE = "/generated-images"

# 确保数据目录存在
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(GENERATED_IMAGES_DIR, exist_ok=True)

# 内存日志 (deque 是线程/异步安全的)
log_deque = collections.deque(maxlen=200)

# 日志显示配置
show_full_response_body = False

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

UserAgentMode = Literal["external", "aggregator", "claude_code", "sillytavern", "custom"]
InjectionPosition = Literal["prepend", "append"]
EndpointPreset = Literal["chat_completions", "images_generations"]


class InjectedMessage(BaseModel):
    """配置项注入消息"""
    role: Literal["system", "user", "assistant", "tool"] = Field(..., description="消息角色")
    content: str = Field(..., description="消息内容")
    position: Optional[InjectionPosition] = Field(None, description="单条消息注入位置：prepend=最前，append=最后；为空时兼容旧配置级 injection_position")


class ApiConfigBase(BaseModel):
    """API 配置的基础模型 (用于创建/更新)"""
    priority: int = Field(..., description="优先级，数字越小越优先")
    url: str = Field(..., description="API 终端地址, e.g., https://api.openai.com/v1")
    api_key: str = Field(..., description="用于该终端的 API Key")
    model: Optional[str] = Field(None, description="要覆盖的模型名称，如果为 null/空，则使用原始请求中的 model")
    max_retries: Optional[int] = Field(0, description="该配置项失败后的重试次数（默认 0，即不重试）")
    request_overrides: Optional[Dict[str, Any]] = Field(default_factory=dict, description="转发时强制覆盖到请求体的参数（支持任意 JSON 对象）")
    injection_position: Optional[InjectionPosition] = Field("prepend", description="旧版配置级注入位置；仅用于兼容未携带 position 的注入消息")
    injected_messages: Optional[List[InjectedMessage]] = Field(default_factory=list, description="按顺序注入到 messages 的消息列表")
    user_agent_mode: Optional[UserAgentMode] = Field("aggregator", description="User-Agent 模式：external/aggregator/claude_code/sillytavern/custom")
    custom_user_agent: Optional[str] = Field(None, description="自定义 User-Agent，仅 user_agent_mode=custom 时使用")
    stream_mode_strategy: Optional[Literal["passthrough", "force_fake_non_stream", "force_fake_stream"]] = Field(
        "passthrough",
        description="流模式策略：passthrough=不变动，force_fake_non_stream=假非流，force_fake_stream=假流式"
    )
    endpoint_preset: Optional[EndpointPreset] = Field(
        "chat_completions",
        description="预设端点：chat_completions=/chat/completions，images_generations=/images/generations"
    )
    # [新增] 熔断机制相关配置
    consecutive_failure_threshold: Optional[int] = Field(None, description="连续失败N次后禁用（默认不启用）")
    disable_duration_seconds: Optional[int] = Field(None, description="禁用时长（秒，默认不启用）")


class ApiConfig(ApiConfigBase):
    """API 配置的完整模型 (包含 ID)"""
    id: str = Field(..., description="唯一的配置 ID")


class ApiConfigCreate(ApiConfigBase):
    """用于创建配置项的 Pydantic 模型，增加了 scheme_name"""
    scheme_name: str = Field("default", description="配置项所属的方案名称")


class LogSettingsUpdate(BaseModel):
    """日志展示设置"""
    show_full_response_body: bool = Field(..., description="是否在日志中显示完整响应体")


class UpstreamModelQueryRequest(BaseModel):
    """使用未保存的表单参数查询上游模型列表"""
    url: str = Field(..., description="OpenAI 兼容 API 终端地址, e.g., https://api.openai.com/v1")
    api_key: str = Field(..., description="用于该终端的 API Key")
    user_agent_mode: Optional[UserAgentMode] = Field("aggregator", description="User-Agent 模式")
    custom_user_agent: Optional[str] = Field(None, description="自定义 User-Agent")

# --- 3. 辅助函数 (日志, JSON I/O, 统计) ---

AGGREGATOR_USER_AGENT = f"{PROJECT_NAME}/{API_VERSION}"
CLAUDE_CODE_USER_AGENT = "claude-cli/2.1.63 (external, cli)"
SILLYTAVERN_USER_AGENT = "node-fetch"


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


async def verify_anthropic_key(
        authorization: Optional[str] = Header(None, description="兼容 Bearer YOUR_ADMIN_KEY"),
        x_api_key: Optional[str] = Header(None, alias="x-api-key", description="Anthropic 风格 API Key"),
        anthropic_version: Optional[str] = Header(None, alias="anthropic-version", description="Anthropic API 版本，仅兼容读取")
):
    """Anthropic 入口鉴权：同时支持 Authorization: Bearer ADMIN_KEY 与 x-api-key: ADMIN_KEY。"""
    if not ADMIN_KEY:
        log_message("!!! 严重错误: ADMIN_KEY 未设置, 所有请求都将失败 !!!")
        raise HTTPException(status_code=500, detail="服务器内部错误: 认证未配置")

    bearer_ok = authorization == f"Bearer {ADMIN_KEY}"
    x_key_ok = x_api_key == ADMIN_KEY
    if not bearer_ok and not x_key_ok:
        log_message("Anthropic 入口认证失败: Authorization/x-api-key 均不匹配")
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


def normalize_injected_messages(raw_messages: Any, default_position: Optional[str] = "prepend") -> List[Dict[str, str]]:
    """将 injected_messages 归一化为可直接发往上游的消息列表，并兼容旧版配置级注入位置。"""
    if not isinstance(raw_messages, list):
        return []

    allowed_roles = {"system", "user", "assistant", "tool"}
    allowed_positions = {"prepend", "append"}
    fallback_position = default_position if default_position in allowed_positions else "prepend"
    normalized: List[Dict[str, str]] = []

    for msg in raw_messages:
        if isinstance(msg, dict):
            role = msg.get("role")
            content = msg.get("content")
            position = msg.get("position")
        elif hasattr(msg, "dict"):
            data = msg.dict()
            role = data.get("role")
            content = data.get("content")
            position = data.get("position")
        else:
            continue

        if role not in allowed_roles:
            continue
        if content is None:
            continue
        if position not in allowed_positions:
            position = fallback_position

        normalized.append({
            "role": role,
            "content": str(content),
            "position": position
        })

    return normalized


def split_injected_messages_by_position(raw_messages: Any, default_position: Optional[str] = "prepend") -> Dict[str, List[Dict[str, str]]]:
    """按单条消息 position 拆分注入消息；发给上游前移除 position 元信息。"""
    groups = {"prepend": [], "append": []}
    for msg in normalize_injected_messages(raw_messages, default_position):
        upstream_msg = {"role": msg["role"], "content": msg["content"]}
        groups[msg["position"]].append(upstream_msg)
    return groups


def resolve_user_agent(user_agent_mode: Optional[str], custom_user_agent: Optional[str], external_user_agent: Optional[str]) -> Optional[str]:
    """根据配置解析最终发往上游的 User-Agent。"""
    mode = user_agent_mode or "aggregator"

    if mode == "external":
        return external_user_agent or AGGREGATOR_USER_AGENT
    if mode == "claude_code":
        return CLAUDE_CODE_USER_AGENT
    if mode == "sillytavern":
        return SILLYTAVERN_USER_AGENT
    if mode == "custom":
        custom_value = (custom_user_agent or "").strip()
        return custom_value or AGGREGATOR_USER_AGENT

    return AGGREGATOR_USER_AGENT


def apply_user_agent_header(headers: Dict[str, str], user_agent_mode: Optional[str], custom_user_agent: Optional[str], external_user_agent: Optional[str]) -> Dict[str, str]:
    """在保持现有请求头构造逻辑不变的基础上，仅追加/覆盖 User-Agent。"""
    resolved_user_agent = resolve_user_agent(user_agent_mode, custom_user_agent, external_user_agent)
    if resolved_user_agent:
        headers["User-Agent"] = resolved_user_agent
    return headers


def extract_total_tokens(payload: Any) -> Optional[int]:
    """兼容 OpenAI / Claude / Gemini 等常见响应结构的总 token 提取。"""

    def as_int(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        return value if isinstance(value, int) else None

    def iter_nodes(obj: Any, max_depth: int = 6):
        stack = [(obj, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > max_depth:
                continue

            if isinstance(current, dict):
                yield current
                for v in current.values():
                    if isinstance(v, (dict, list)):
                        stack.append((v, depth + 1))
            elif isinstance(current, list):
                for item in current:
                    if isinstance(item, (dict, list)):
                        stack.append((item, depth + 1))

    # 1) 先尝试各种“总量字段”
    total_keys = [
        "total_tokens",
        "totalTokenCount",
        "total_token_count",
        "token_count"
    ]
    for node in iter_nodes(payload):
        for key in total_keys:
            val = as_int(node.get(key))
            if val is not None:
                return val

    # 2) 再尝试可求和字段
    pair_keys = [
        ("prompt_tokens", "completion_tokens"),          # OpenAI 兼容
        ("input_tokens", "output_tokens"),              # Claude 常见
        ("promptTokenCount", "candidatesTokenCount"),   # Gemini 常见
        ("inputTokenCount", "outputTokenCount"),
        ("prompt_token_count", "completion_token_count"),
        ("input_token_count", "output_token_count")
    ]

    for node in iter_nodes(payload):
        for a, b in pair_keys:
            va = as_int(node.get(a))
            vb = as_int(node.get(b))
            if va is not None and vb is not None:
                total = va + vb

                # Gemini 常见附加字段，存在则叠加
                for extra_key in ["toolUsePromptTokenCount", "thoughtsTokenCount", "cachedContentTokenCount"]:
                    extra_val = as_int(node.get(extra_key))
                    if extra_val is not None:
                        total += extra_val

                return total

    return None


def resolve_stream_modes(external_is_stream: bool, strategy: Optional[str]) -> Dict[str, Any]:
    """根据外部请求模式与配置策略，决定上游请求模式和下游返回模式。"""
    normalized_strategy = strategy or "passthrough"

    # 假非流：仅当外部是非流请求时才转换；外部本来是流式则不变动
    if normalized_strategy == "force_fake_non_stream":
        if not external_is_stream:
            return {
                "strategy": normalized_strategy,
                "upstream_is_stream": True,
                "downstream_is_stream": False,
                "mode_label": "假非流"
            }
        return {
            "strategy": "passthrough",
            "upstream_is_stream": True,
            "downstream_is_stream": True,
            "mode_label": "不变动"
        }

    # 假流式：仅当外部是流式请求时才转换；外部本来是非流则不变动
    if normalized_strategy == "force_fake_stream":
        if external_is_stream:
            return {
                "strategy": normalized_strategy,
                "upstream_is_stream": False,
                "downstream_is_stream": True,
                "mode_label": "假流式"
            }
        return {
            "strategy": "passthrough",
            "upstream_is_stream": False,
            "downstream_is_stream": False,
            "mode_label": "不变动"
        }

    return {
        "strategy": "passthrough",
        "upstream_is_stream": external_is_stream,
        "downstream_is_stream": external_is_stream,
        "mode_label": "不变动"
    }


def convert_sse_bytes_to_non_stream_json(sse_bytes: bytes) -> Dict[str, Any]:
    """将 SSE 流完整聚合为一个非流式 OpenAI 兼容响应对象。"""
    text = sse_bytes.decode("utf-8", errors="ignore")
    events: List[Dict[str, Any]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue

        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue

        try:
            obj = json.loads(payload)
        except Exception:
            continue

        if isinstance(obj, dict):
            events.append(obj)

    if not events:
        raise ValueError("流式响应中未解析到有效 data 事件")

    # 若上游已经给出了完整对象，优先直接返回
    for obj in reversed(events):
        choices = obj.get("choices")
        if obj.get("object") == "chat.completion":
            return obj
        if isinstance(choices, list) and choices:
            first_choice = choices[0]
            if isinstance(first_choice, dict) and "message" in first_choice:
                return obj

    first_obj = events[0]
    usage_obj = None
    choices_acc: Dict[int, Dict[str, Any]] = {}

    for obj in events:
        usage_candidate = obj.get("usage")
        if isinstance(usage_candidate, dict):
            usage_obj = usage_candidate

        choices = obj.get("choices")
        if not isinstance(choices, list):
            continue

        for choice in choices:
            if not isinstance(choice, dict):
                continue

            idx = choice.get("index", 0)
            if not isinstance(idx, int):
                idx = 0

            state = choices_acc.setdefault(idx, {
                "index": idx,
                "role": "assistant",
                "content": "",
                "finish_reason": None
            })

            delta = choice.get("delta")
            if isinstance(delta, dict):
                role = delta.get("role")
                if isinstance(role, str) and role:
                    state["role"] = role

                content_piece = delta.get("content")
                if isinstance(content_piece, str):
                    state["content"] += content_piece

            message = choice.get("message")
            if isinstance(message, dict):
                role = message.get("role")
                if isinstance(role, str) and role:
                    state["role"] = role

                content = message.get("content")
                if isinstance(content, str):
                    state["content"] = content

            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                state["finish_reason"] = finish_reason

    if not choices_acc:
        return events[-1]

    output_choices = []
    for idx in sorted(choices_acc.keys()):
        state = choices_acc[idx]
        output_choices.append({
            "index": state["index"],
            "message": {
                "role": state["role"],
                "content": state["content"]
            },
            "finish_reason": state["finish_reason"]
        })

    result = {
        "id": first_obj.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": first_obj.get("created") if isinstance(first_obj.get("created"), int) else int(datetime.now().timestamp()),
        "model": first_obj.get("model"),
        "choices": output_choices
    }

    if isinstance(usage_obj, dict):
        result["usage"] = usage_obj

    return result


@app.post("/v1/chat/completions", tags=["Proxy"])
async def proxy_chat_completions(
        request: Request,
        auth: bool = Depends(verify_key)
):
    """
    [重构] OpenAI /v1/chat/completions 代理端点。
    它会根据方案、熔断、轮询和优先级进行故障转移。
    """

    class ClientRequestAborted(Exception):
        """上游客户端已断开，当前请求应立即停止。"""

    async def ensure_client_connected():
        if await request.is_disconnected():
            raise ClientRequestAborted("上游客户端已断开，停止当前请求")

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
        try:
            await ensure_client_connected()
        except ClientRequestAborted as e:
            log_message(f"上游客户端已断开，停止继续切换配置重试: {e}")
            return Response(status_code=499)

        max_retries = config.max_retries if config.max_retries is not None else 0
        if max_retries < 0:
            max_retries = 0

        log_message(
            f"正在尝试方案 '{scheme_name}' 的配置项 ID: {config.id} (Priority: {config.priority}, MaxRetries: {max_retries})")

        # 查找原始分组信息，用于成功后更新轮询状态
        original_group = priority_groups.get(config.priority, [])
        try:
            success_index_in_group = original_group.index(config)
        except ValueError:
            success_index_in_group = 0  # 理论上不会发生

        # 准备请求（固定部分）
        proxy_url = f"{config.url.rstrip('/')}/chat/completions"

        mode_plan = resolve_stream_modes(is_stream, config.stream_mode_strategy)
        upstream_is_stream = mode_plan["upstream_is_stream"]
        downstream_is_stream = mode_plan["downstream_is_stream"]

        proxy_headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json" if not upstream_is_stream else "text/event-stream"
        }
        apply_user_agent_header(
            proxy_headers,
            config.user_agent_mode,
            config.custom_user_agent,
            request.headers.get("user-agent")
        )

        for attempt_no in range(max_retries + 1):
            response_context = None
            try:
                await ensure_client_connected()
            except ClientRequestAborted as e:
                log_message(f"上游客户端已断开，停止继续重试: {e}")
                return Response(status_code=499)

            # 每次重试都从原请求体重新构建，避免脏数据累积
            proxy_body = request_body.copy()

            # 先应用任意 request_overrides（强制覆盖）
            request_overrides = config.request_overrides if isinstance(config.request_overrides, dict) else {}
            if request_overrides:
                proxy_body.update(request_overrides)

            # model 保持配置优先
            if config.model:
                proxy_body["model"] = config.model

            # 注入消息（每条消息可独立配置最前/最后；兼容旧版配置级 injection_position）
            injected_message_groups = split_injected_messages_by_position(config.injected_messages, config.injection_position)
            if injected_message_groups["prepend"] or injected_message_groups["append"]:
                original_messages = proxy_body.get("messages")
                if not isinstance(original_messages, list):
                    original_messages = []

                proxy_body["messages"] = injected_message_groups["prepend"] + original_messages + injected_message_groups["append"]

            try:
                await ensure_client_connected()

                if config.endpoint_preset == "images_generations":
                    images_proxy_url = f"{config.url.rstrip('/')}/images/generations"
                    images_headers = dict(proxy_headers)
                    images_headers["Accept"] = "application/json"
                    if request_body.get("stream", False):
                        log_message(f"配置项 ID: {config.id} 使用 images_generations 预设，上游按非流式请求，向下游返回 fake SSE 图片 markdown")

                    images_body = build_images_generations_payload(proxy_body, config)
                    response = await httpx_client.post(images_proxy_url, headers=images_headers, json=images_body)
                    response.raise_for_status()
                    response_json = response.json()
                    response_json = normalize_image_response_urls(response_json, config.url)
                    image_public_url_prefix = str(request.base_url).rstrip("/") + GENERATED_IMAGES_ROUTE
                    wrapped_json = wrap_image_response_as_chat_completion(
                        response_json,
                        request_body,
                        config,
                        image_output_dir=GENERATED_IMAGES_DIR,
                        image_public_url_prefix=image_public_url_prefix
                    )

                    if show_full_response_body:
                        try:
                            log_message(f"响应体完整内容: {json.dumps(wrapped_json, ensure_ascii=False)}")
                        except Exception:
                            log_message(f"响应体完整内容(序列化失败，使用字符串): {str(wrapped_json)}")

                    log_message(f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次 Images Generations 预设请求成功")
                    await update_stats_and_state(config, True, scheme_name, original_group, success_index_in_group)

                    if request_body.get("stream", False):
                        async def images_fake_stream_generator(final_payload: Dict[str, Any]):
                            content = ""
                            try:
                                content = final_payload.get("choices", [{}])[0].get("message", {}).get("content", "")
                            except Exception:
                                content = ""
                            chunk_payload = {
                                "id": final_payload.get("id"),
                                "object": "chat.completion.chunk",
                                "created": final_payload.get("created"),
                                "model": final_payload.get("model"),
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"role": "assistant", "content": content},
                                        "finish_reason": None
                                    }
                                ]
                            }
                            yield f"data: {json.dumps(chunk_payload, ensure_ascii=False)}\n\n".encode("utf-8")
                            done_payload = {
                                "id": final_payload.get("id"),
                                "object": "chat.completion.chunk",
                                "created": final_payload.get("created"),
                                "model": final_payload.get("model"),
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                            }
                            yield f"data: {json.dumps(done_payload, ensure_ascii=False)}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"

                        return StreamingResponse(images_fake_stream_generator(wrapped_json), media_type="text/event-stream")

                    return JSONResponse(content=wrapped_json, status_code=response.status_code)

                # 策略强制覆盖 stream，确保上游请求模式与策略一致
                proxy_body["stream"] = upstream_is_stream

                if upstream_is_stream:
                    response_context = httpx_client.stream("POST", proxy_url, headers=proxy_headers, json=proxy_body)
                    response = await response_context.__aenter__()

                    if response.status_code >= 400:
                        response_body = await response.aread()
                        error_text = response_body.decode('utf-8')
                        log_message(
                            f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次失败 (HTTP {response.status_code}, 策略={mode_plan['mode_label']}): {error_text}")
                        last_error = f"HTTP {response.status_code}: {error_text}"
                        if show_full_response_body:
                            log_message(f"响应体完整内容: {error_text}")
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
                            def text(self): return self._content if isinstance(self._content, str) else str(
                                self._content)

                        last_error_response = MockResponse(error_content, response.status_code)
                        try:
                            await ensure_client_connected()
                        except ClientRequestAborted as disconnect_error:
                            log_message(f"上游客户端已断开，本次下游失败不计入统计且停止重试: {disconnect_error}")
                            await response_context.__aexit__(None, None, None)
                            response_context = None
                            return Response(status_code=499)

                        await update_stats_and_state(config, False, scheme_name, [], 0)
                        await response_context.__aexit__(None, None, None)
                        response_context = None

                        if attempt_no < max_retries:
                            log_message(f"配置项 ID: {config.id} 即将进行第 {attempt_no + 2} 次重试")
                            continue
                        break

                    if downstream_is_stream:
                        async def final_stream_generator(successful_config, ctx, resp):
                            pending = bytearray()
                            total_tokens = None

                            def try_extract_usage_from_sse_line(line_text: str):
                                nonlocal total_tokens
                                line_text = line_text.strip()
                                if not line_text.startswith("data:"):
                                    return

                                payload = line_text[5:].strip()
                                if payload == "[DONE]":
                                    return

                                try:
                                    data_obj = json.loads(payload)
                                except Exception:
                                    return

                                extracted_total = extract_total_tokens(data_obj)
                                if extracted_total is not None:
                                    total_tokens = extracted_total

                            try:
                                async for chunk in resp.aiter_bytes():
                                    yield chunk
                                    pending.extend(chunk)

                                    while b"\n" in pending:
                                        line_bytes, _, rest = pending.partition(b"\n")
                                        pending = bytearray(rest)
                                        try_extract_usage_from_sse_line(line_bytes.decode("utf-8", errors="ignore"))

                                if pending:
                                    try_extract_usage_from_sse_line(pending.decode("utf-8", errors="ignore"))

                                if total_tokens is not None:
                                    log_message(f"配置项 ID: {successful_config.id} 流式请求结束 (total_tokens={total_tokens}, 策略={mode_plan['mode_label']}，成功统计已在启动时记录)")
                                else:
                                    log_message(f"配置项 ID: {successful_config.id} 流式请求结束 (total_tokens=未知, 策略={mode_plan['mode_label']}，成功统计已在启动时记录)")
                            except Exception as e:
                                error_type = type(e).__name__
                                if "ClientDisconnect" in error_type or "CancelledError" in error_type:
                                    log_message(
                                        f"配置项 ID: {successful_config.id} 流传输被客户端主动断开 (Type: {error_type})")
                                    return
                                else:
                                    log_message(f"配置项 ID: {successful_config.id} 在流传输过程中失败: {repr(e)}")
                                raise
                            finally:
                                await ctx.__aexit__(None, None, None)

                        log_message(
                            f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次流式请求启动成功 (HTTP {response.status_code}, 策略={mode_plan['mode_label']}，立即记录成功并推进轮询)")
                        await update_stats_and_state(config, True, scheme_name, original_group, success_index_in_group)
                        response_context_to_pass = response_context
                        response_context = None
                        return StreamingResponse(
                            final_stream_generator(config, response_context_to_pass, response),
                            media_type="text/event-stream"
                        )

                    # 假非流：上游流式，落地后返回非流
                    stream_bytes = await response.aread()
                    merged_json = convert_sse_bytes_to_non_stream_json(stream_bytes)
                    total_tokens = extract_total_tokens(merged_json)
                    if show_full_response_body:
                        try:
                            log_message(f"响应体完整内容: {json.dumps(merged_json, ensure_ascii=False)}")
                        except Exception:
                            log_message(f"响应体完整内容(序列化失败，使用字符串): {str(merged_json)}")
                    if total_tokens is not None:
                        log_message(f"配置项 ID: {config.id} 假非流成功 (total_tokens={total_tokens})")
                    else:
                        log_message(f"配置项 ID: {config.id} 假非流成功 (total_tokens=未知)")

                    await update_stats_and_state(config, True, scheme_name, original_group, success_index_in_group)
                    await response_context.__aexit__(None, None, None)
                    response_context = None
                    return JSONResponse(content=merged_json, status_code=response.status_code)

                # 上游非流
                response = await httpx_client.post(proxy_url, headers=proxy_headers, json=proxy_body)
                response.raise_for_status()
                response_json = response.json()
                if show_full_response_body:
                    try:
                        log_message(f"响应体完整内容: {json.dumps(response_json, ensure_ascii=False)}")
                    except Exception:
                        log_message(f"响应体完整内容(序列化失败，使用字符串): {str(response_json)}")
                total_tokens = extract_total_tokens(response_json)

                if downstream_is_stream:
                    # 假流式：上游非流，返回单条完整 data + [DONE]
                    async def fake_stream_generator(final_payload: Dict[str, Any]):
                        yield f"data: {json.dumps(final_payload, ensure_ascii=False)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"

                    if total_tokens is not None:
                        log_message(f"配置项 ID: {config.id} 假流式成功 (total_tokens={total_tokens})")
                    else:
                        log_message(f"配置项 ID: {config.id} 假流式成功 (total_tokens=未知)")
                    await update_stats_and_state(config, True, scheme_name, original_group, success_index_in_group)
                    return StreamingResponse(fake_stream_generator(response_json), media_type="text/event-stream")

                if total_tokens is not None:
                    log_message(f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次非流式请求成功 (total_tokens={total_tokens}, 策略={mode_plan['mode_label']})")
                else:
                    log_message(f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次非流式请求成功 (total_tokens=未知, 策略={mode_plan['mode_label']})")
                await update_stats_and_state(config, True, scheme_name, original_group, success_index_in_group)
                return JSONResponse(content=response_json, status_code=response.status_code)

            except ClientRequestAborted as e:
                log_message(f"上游客户端已断开，停止当前代理请求: {e}")
                return Response(status_code=499)
            except asyncio.CancelledError:
                log_message("当前代理请求已被取消，停止继续重试且不计入后端失败")
                raise
            except EndpointPresetError as e:
                last_error = e
                error_content = {
                    "error": {
                        "message": str(e),
                        "type": "invalid_request_error",
                        "code": "images_generations_preset_error"
                    }
                }

                class PresetErrorResponse:
                    def __init__(self, content, status_code_val):
                        self._content, self.status_code = content, status_code_val

                    def json(self):
                        return self._content

                    @property
                    def text(self):
                        return json.dumps(self._content, ensure_ascii=False)

                last_error_response = PresetErrorResponse(error_content, 400)
                log_message(f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次失败 (Images Generations 预设错误): {e}")
                try:
                    await ensure_client_connected()
                except ClientRequestAborted as disconnect_error:
                    log_message(f"上游客户端已断开，本次下游失败不计入统计且停止重试: {disconnect_error}")
                    return Response(status_code=499)
                await update_stats_and_state(config, False, scheme_name, [], 0)
                if attempt_no < max_retries:
                    try:
                        await ensure_client_connected()
                    except ClientRequestAborted as disconnect_error:
                        log_message(f"上游客户端已断开，取消后续重试: {disconnect_error}")
                        return Response(status_code=499)
                    log_message(f"配置项 ID: {config.id} 即将进行第 {attempt_no + 2} 次重试")
                    continue
                break
            except httpx.HTTPStatusError as e:
                last_error, last_error_response = e, e.response
                log_message(
                    f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次失败 (HTTP {e.response.status_code}): {e.response.text}")
                if show_full_response_body:
                    log_message(f"响应体完整内容: {e.response.text}")
                try:
                    await ensure_client_connected()
                except ClientRequestAborted as disconnect_error:
                    log_message(f"上游客户端已断开，本次下游失败不计入统计且停止重试: {disconnect_error}")
                    return Response(status_code=499)
                await update_stats_and_state(config, False, scheme_name, [], 0)
                if attempt_no < max_retries:
                    try:
                        await ensure_client_connected()
                    except ClientRequestAborted as disconnect_error:
                        log_message(f"上游客户端已断开，取消后续重试: {disconnect_error}")
                        return Response(status_code=499)
                    log_message(f"配置项 ID: {config.id} 即将进行第 {attempt_no + 2} 次重试")
                    continue
                break
            except httpx.RequestError as e:
                last_error = e
                log_message(f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次失败 (RequestError): {e}")
                try:
                    await ensure_client_connected()
                except ClientRequestAborted as disconnect_error:
                    log_message(f"上游客户端已断开，本次下游失败不计入统计且停止重试: {disconnect_error}")
                    return Response(status_code=499)
                await update_stats_and_state(config, False, scheme_name, [], 0)
                if attempt_no < max_retries:
                    try:
                        await ensure_client_connected()
                    except ClientRequestAborted as disconnect_error:
                        log_message(f"上游客户端已断开，取消后续重试: {disconnect_error}")
                        return Response(status_code=499)
                    log_message(f"配置项 ID: {config.id} 即将进行第 {attempt_no + 2} 次重试")
                    continue
                break
            except Exception as e:
                error_type = type(e).__name__
                if "ClientDisconnect" in error_type or "CancelledError" in error_type:
                    log_message(f"检测到上游客户端已断开/请求被取消，停止继续重试 (Type: {error_type})")
                    return Response(status_code=499)

                last_error = e
                log_message(f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次失败 (Exception): {e}")
                try:
                    await ensure_client_connected()
                except ClientRequestAborted as disconnect_error:
                    log_message(f"上游客户端已断开，本次下游失败不计入统计且停止重试: {disconnect_error}")
                    return Response(status_code=499)
                await update_stats_and_state(config, False, scheme_name, [], 0)
                if attempt_no < max_retries:
                    try:
                        await ensure_client_connected()
                    except ClientRequestAborted as disconnect_error:
                        log_message(f"上游客户端已断开，取消后续重试: {disconnect_error}")
                        return Response(status_code=499)
                    log_message(f"配置项 ID: {config.id} 即将进行第 {attempt_no + 2} 次重试")
                    continue
                break
            finally:
                if response_context is not None:
                    await response_context.__aexit__(None, None, None)

        log_message(f"配置项 ID: {config.id} 已耗尽重试次数，回退到下一配置项")

    log_message("所有配置项均尝试失败")
    if last_error_response is not None:
        try:
            error_content = last_error_response.json()
        except Exception:
            error_content = last_error_response.text
        return JSONResponse(content=error_content, status_code=last_error_response.status_code)

    return JSONResponse(status_code=500, content={"error": f"所有后端均失败。最后错误: {str(last_error)}"})


@app.post("/v1/messages", tags=["Proxy"])
async def proxy_anthropic_messages(
        request: Request,
        auth: bool = Depends(verify_anthropic_key)
):
    """Anthropic Messages 兼容入口。内部转换为 OpenAI chat.completions 请求并复用现有聚合代理逻辑。"""
    try:
        anthropic_body = await request.json()
    except Exception:
        log_message("Anthropic 请求体 JSON 解析失败")
        return JSONResponse(status_code=400, content=anthropic_error("无效的 JSON 请求体"))

    try:
        openai_body = anthropic_to_openai_chat_request(anthropic_body)
    except AnthropicAdapterError as e:
        log_message(f"Anthropic 请求转换失败: {e}")
        return JSONResponse(status_code=400, content=anthropic_error(str(e)))

    requested_stream = bool(anthropic_body.get("stream", False))

    class InternalOpenAIRequest:
        """最小 Request 适配器，用于复用现有 OpenAI 代理主流程。"""
        def __init__(self, body: Dict[str, Any], original_request: Request):
            self._body = body
            self.headers = original_request.headers
            self._original_request = original_request

        async def json(self):
            return self._body

        async def is_disconnected(self):
            return await self._original_request.is_disconnected()

    internal_request = InternalOpenAIRequest(openai_body, request)
    openai_response = await proxy_chat_completions(internal_request, True)

    if isinstance(openai_response, StreamingResponse):
        if not requested_stream:
            # 正常情况下 Anthropic 非流式请求会让内部 OpenAI body stream=false，不应走到这里；保底返回协议错误。
            log_message("Anthropic 非流式请求收到了内部流式响应，返回协议错误")
            return JSONResponse(status_code=500, content=anthropic_error("内部代理返回了非预期的流式响应", "api_error"))

        async def anthropic_sse_generator():
            pending = bytearray()
            converter = OpenAIToAnthropicSSEConverter(anthropic_body)
            for event_bytes in converter.start_events():
                yield event_bytes
            async for chunk in openai_response.body_iterator:
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                pending.extend(chunk)
                while b"\n" in pending:
                    line_bytes, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    line_text = line_bytes.decode("utf-8", errors="ignore")
                    for event_bytes in converter.feed_line(line_text):
                        yield event_bytes
            if pending:
                line_text = pending.decode("utf-8", errors="ignore")
                for event_bytes in converter.feed_line(line_text):
                    yield event_bytes
            for event_bytes in converter.finish_events():
                yield event_bytes

        return StreamingResponse(anthropic_sse_generator(), media_type="text/event-stream")

    body_bytes = getattr(openai_response, "body", b"")
    status_code = getattr(openai_response, "status_code", 200)
    try:
        openai_payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except Exception:
        openai_payload = body_bytes.decode("utf-8", errors="ignore") if isinstance(body_bytes, (bytes, bytearray)) else str(body_bytes)

    if status_code >= 400:
        error_payload, error_status = openai_error_to_anthropic(openai_payload, status_code)
        return JSONResponse(status_code=error_status, content=error_payload)

    try:
        if requested_stream:
            async def json_to_anthropic_sse_generator():
                for event_bytes in openai_json_to_anthropic_stream(openai_payload, anthropic_body):
                    yield event_bytes

            return StreamingResponse(json_to_anthropic_sse_generator(), media_type="text/event-stream")

        anthropic_payload = openai_chat_to_anthropic_response(openai_payload, anthropic_body)
        return JSONResponse(status_code=status_code, content=anthropic_payload)
    except Exception as e:
        log_message(f"Anthropic 响应转换失败: {e}")
        return JSONResponse(status_code=500, content=anthropic_error(f"响应转换失败: {e}", "api_error"))


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


@admin_router.post("/models/query")
async def query_upstream_models(query: UpstreamModelQueryRequest, request: Request) -> Dict[str, Any]:
    """使用当前表单中未保存的 URL/Key/UA 模式查询上游 OpenAI 兼容模型列表。"""
    upstream_url = f"{query.url.rstrip('/')}/models"
    headers = {
        "Authorization": f"Bearer {query.api_key}",
        "Accept": "application/json"
    }
    apply_user_agent_header(
        headers,
        query.user_agent_mode,
        query.custom_user_agent,
        request.headers.get("user-agent")
    )

    try:
        response = await httpx_client.get(upstream_url, headers=headers)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as e:
        detail = e.response.text or e.response.reason_phrase
        raise HTTPException(status_code=502, detail=f"上游模型接口返回错误 (HTTP {e.response.status_code}): {detail}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"请求上游模型接口失败: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"解析上游模型接口响应失败: {e}")

    raw_models = payload.get("data") if isinstance(payload, dict) else None
    model_ids: List[str] = []
    if isinstance(raw_models, list):
        for item in raw_models:
            if isinstance(item, dict):
                model_id = item.get("id")
            else:
                model_id = None
            if isinstance(model_id, str) and model_id.strip():
                model_ids.append(model_id.strip())

    return {
        "object": "list",
        "data": sorted(set(model_ids))
    }


@admin_router.get("/settings/logs")
async def get_log_settings() -> Dict[str, bool]:
    """获取日志展示设置"""
    return {"show_full_response_body": show_full_response_body}


@admin_router.put("/settings/logs")
async def update_log_settings(settings: LogSettingsUpdate) -> Dict[str, bool]:
    """更新日志展示设置"""
    global show_full_response_body
    show_full_response_body = settings.show_full_response_body
    log_message(f"管理: 已{'开启' if show_full_response_body else '关闭'}完整响应体日志")
    return {"show_full_response_body": show_full_response_body}


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


# --- 8. 静态文件服务 (用于前端和生成图片) ---

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount(GENERATED_IMAGES_ROUTE, StaticFiles(directory=GENERATED_IMAGES_DIR), name="generated_images")


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