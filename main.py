import os
import asyncio
import base64
import re
import httpx
import uvicorn
import collections
import aiofiles
import json
import uuid
import ipaddress
import copy
import time
from datetime import datetime, timedelta, timezone
from threading import RLock
from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict, Literal
from itertools import groupby
from urllib.parse import urljoin

from fastapi import FastAPI, Request, HTTPException, Depends, Header, APIRouter
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

from endpoint_presets import (
    EndpointPresetError,
    build_images_request_plan,
    convert_response_base64_images_to_urls,
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
# IP 连续认证失败自动封禁阈值；封禁后需在控制台手动解除
IP_FAILURE_BAN_THRESHOLD = 5
# 明细数据保留期：累计请求数量长期保留，路径明细和生成图片按 1 个月清理
DETAIL_RETENTION_DAYS = 30
DETAIL_RETENTION_SECONDS = DETAIL_RETENTION_DAYS * 24 * 60 * 60
# 单个 IP 最多保留的路径明细数量，避免 stats.json 因资源路径无限增长
MAX_IP_PATH_STATS = 200
# 普通统计写盘节流间隔；关键事件仍会强制刷新
STATS_FLUSH_INTERVAL_SECONDS = 10
# 生成图片后台清理间隔；磁盘历史文件低频清理，内存图片短 TTL 高频清理
GENERATED_IMAGE_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
MEMORY_GENERATED_IMAGE_TTL_SECONDS = int(os.environ.get("MEMORY_GENERATED_IMAGE_TTL_SECONDS", 10 * 60))
MEMORY_GENERATED_IMAGE_CLEANUP_INTERVAL_SECONDS = int(os.environ.get("MEMORY_GENERATED_IMAGE_CLEANUP_INTERVAL_SECONDS", 60))

# 确保数据目录存在
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(GENERATED_IMAGES_DIR, exist_ok=True)

# 内存日志 (deque 是线程/异步安全的)
log_deque = collections.deque(maxlen=200)

# 日志显示配置
show_full_response_body = False

# 异步文件读写锁
file_lock = asyncio.Lock()
# 配置与统计缓存锁：高频请求路径优先读写内存，减少磁盘读盘/写盘压力
config_lock = asyncio.Lock()
stats_lock = asyncio.Lock()
config_cache: Optional[Dict[str, List[dict]]] = None
stats_cache: Optional[dict] = None
stats_dirty = False
stats_last_flush_monotonic = 0.0
stats_last_path_cleanup_date: Optional[str] = None
generated_image_cleanup_task: Optional[asyncio.Task] = None
stats_flush_task: Optional[asyncio.Task] = None
memory_generated_images_lock = RLock()
memory_generated_images: Dict[str, Dict[str, Any]] = {}

# 全局 httpx 客户端 (用于连接池)
# 设置一个合理的超时时间，例如 300 秒，兼容耗时较长的同步生图请求
httpx_client = httpx.AsyncClient(timeout=300.0)

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
ImageUpstreamMode = Literal[
    "openai_edit_image",
    "generation_images_array",
    "generation_ref_assets_array",
    "generation_reference_images_array",
    "custom"
]
ImageCustomReferenceMode = Literal["single", "array", "object_array"]


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
    image_upstream_mode: Optional[ImageUpstreamMode] = Field(
        "generation_reference_images_array",
        description="图片上游模式：openai_edit_image=有图走 edits；generation_* 在 generations 端点用不同字段承载参考图；custom=自定义"
    )
    image_generation_path: Optional[str] = Field("/images/generations", description="图片文生图路径，默认 /images/generations")
    image_edit_path: Optional[str] = Field("/images/edits", description="图片图生图/编辑路径，默认 /images/edits")
    image_custom_generation_path: Optional[str] = Field(None, description="自定义图片无图路径；为空使用 image_generation_path")
    image_custom_edit_path: Optional[str] = Field(None, description="自定义图片有图路径；为空使用 image_edit_path")
    image_custom_reference_field: Optional[str] = Field(None, description="自定义参考图字段名")
    image_custom_reference_mode: Optional[ImageCustomReferenceMode] = Field("array", description="自定义参考图字段模式：single=第一张字符串，array=全部数组，object_array=对象数组")
    image_custom_reference_object_url_field: Optional[str] = Field("image_url", description="自定义参考图对象数组模式下，对象内承载 URL 的字段名")
    image_custom_include_reference_when_empty: Optional[bool] = Field(False, description="无参考图时是否仍发送自定义空参考图字段")
    image_task_poll_timeout_seconds: Optional[int] = Field(300, description="图片 202 异步任务最大等待秒数，默认 300 秒")
    image_task_poll_interval_seconds: Optional[float] = Field(2.0, description="图片 202 异步任务轮询间隔秒数，默认 2 秒")
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


BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """返回明确的北京时间，避免容器/服务器默认 UTC 导致统计日期偏移。"""
    return datetime.now(BEIJING_TZ)


def beijing_date_str() -> str:
    return beijing_now().strftime("%Y-%m-%d")


def beijing_isoformat() -> str:
    return beijing_now().isoformat()


def parse_stats_datetime(value: Any) -> Optional[datetime]:
    """兼容旧版无时区 ISO 字符串；无时区时按北京时间解释。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING_TZ)
        return parsed.astimezone(BEIJING_TZ)
    except Exception:
        return None


def sanitize_log_text(value: Any) -> str:
    """避免日志记录大块内联 base64 图片，防止错误路径放大内存和 IO 压力。"""
    text = value if isinstance(value, str) else str(value)
    if "base64" not in text and "data:image/" not in text:
        return text
    text = re.sub(
        r"data:(image/[a-zA-Z0-9.+-]+);base64,[A-Za-z0-9+/=\r\n]+",
        r"data:\1;base64,<omitted>",
        text,
        flags=re.MULTILINE
    )
    text = re.sub(
        r'("b64_json"\s*:\s*")[^"]+(")',
        r'\1<omitted>\2',
        text
    )
    return text


def log_message(message: str):
    """向内存日志队列中添加一条日志"""
    now = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    safe_message = sanitize_log_text(message)
    log_deque.append(f"[{now}] {safe_message}")
    print(safe_message)  # 同时也打印到控制台


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


# 配置相关的 I/O 与缓存
def _strip_optional_text(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return value


def normalize_config_text_fields(config_data: dict) -> bool:
    """清理配置中的文本字段首尾空白，返回是否发生变更。"""
    if not isinstance(config_data, dict):
        return False

    changed = False
    required_text_fields = ["url", "api_key"]
    optional_text_fields = [
        "model",
        "custom_user_agent",
        "image_generation_path",
        "image_edit_path",
        "image_custom_generation_path",
        "image_custom_edit_path",
        "image_custom_reference_field",
        "image_custom_reference_object_url_field",
    ]

    for field in required_text_fields:
        value = config_data.get(field)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped != value:
                config_data[field] = stripped
                changed = True

    for field in optional_text_fields:
        value = config_data.get(field)
        normalized = _strip_optional_text(value)
        if normalized != value:
            config_data[field] = normalized
            changed = True

    return changed


async def load_config_data_from_disk() -> Dict[str, List[dict]]:
    """从磁盘读取配置并归一化；仅启动、缓存缺失或异常回退时使用。"""
    configs_data = await read_json_file(CONFIG_FILE, {})
    changed = False

    if isinstance(configs_data, list):
        log_message("检测到旧版配置文件格式（列表），自动迁移到方案格式 `{'default': ...}`")
        configs_data = {"default": configs_data}
        changed = True

    if not isinstance(configs_data, dict):
        configs_data = {}
        changed = True

    normalized: Dict[str, List[dict]] = {}
    for scheme_name, configs_list in configs_data.items():
        if not isinstance(configs_list, list):
            continue
        scheme_key = str(scheme_name).strip() or "default"
        if scheme_key != str(scheme_name):
            changed = True
        normalized_items = []
        for item in configs_list:
            if not isinstance(item, dict):
                continue
            item_copy = dict(item)
            changed = normalize_config_text_fields(item_copy) or changed
            normalized_items.append(item_copy)
        normalized[scheme_key] = normalized_items

    if changed:
        await write_json_file(CONFIG_FILE, normalized)

    return normalized


def build_scheme_models(configs_data: Dict[str, List[dict]]) -> Dict[str, List[ApiConfig]]:
    schemes: Dict[str, List[ApiConfig]] = {}
    for scheme_name, configs_list in configs_data.items():
        configs = [ApiConfig(**data) for data in configs_list]
        configs.sort(key=lambda x: x.priority)
        schemes[scheme_name] = configs
    return schemes


async def get_config_cache_data() -> Dict[str, List[dict]]:
    """获取配置缓存；普通代理请求不再每次读盘。"""
    global config_cache
    async with config_lock:
        if config_cache is None:
            config_cache = await load_config_data_from_disk()
        return copy.deepcopy(config_cache)


async def refresh_config_cache() -> Dict[str, List[dict]]:
    """强制从磁盘刷新配置缓存；仅启动或异常修复场景使用。"""
    global config_cache
    async with config_lock:
        config_cache = await load_config_data_from_disk()
        return copy.deepcopy(config_cache)


async def get_all_schemes() -> Dict[str, List[ApiConfig]]:
    """获取所有方案及其 API 配置；缓存优先，异常时回退一次磁盘读取。"""
    try:
        return build_scheme_models(await get_config_cache_data())
    except Exception as e:
        log_message(f"配置缓存读取失败，回退磁盘读取: {e}")
        return build_scheme_models(await refresh_config_cache())


async def save_all_schemes(schemes: Dict[str, List[ApiConfig]]):
    """保存所有方案配置，并同步刷新内存缓存。"""
    global config_cache
    schemes_data = {}
    for scheme_name, configs in schemes.items():
        scheme_key = str(scheme_name).strip() or "default"
        schemes_data[scheme_key] = []
        for config in configs:
            config_data = config.dict()
            normalize_config_text_fields(config_data)
            schemes_data[scheme_key].append(config_data)
    await write_json_file(CONFIG_FILE, schemes_data)
    async with config_lock:
        config_cache = copy.deepcopy(schemes_data)


# 统计相关的 I/O、缓存与明细清理
def get_default_stats():
    """获取默认的统计数据结构。"""
    return {
        "total": {"success": 0, "fail": 0},
        "today": {
            "date": beijing_date_str(),
            "success": 0,
            "fail": 0,
            "by_config_id": {}
        },
        "by_config_id": {},
        "round_robin_state": {},
        "by_ip": {}
    }


def normalize_request_path(path: Any) -> str:
    """只记录 URL 路径，不记录查询参数。"""
    if not isinstance(path, str) or not path.strip():
        return "/"
    normalized = path.strip().split("?", 1)[0] or "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized[:500]


def normalize_path_stats_entry(entry: Any) -> dict:
    if not isinstance(entry, dict):
        entry = {}
    entry.setdefault("total", 0)
    entry.setdefault("success", 0)
    entry.setdefault("fail", 0)
    entry.setdefault("last_seen_at", None)
    entry.setdefault("last_success_at", None)
    entry.setdefault("last_fail_at", None)
    entry.setdefault("last_fail_reason", None)
    return entry


def ensure_ip_stats_entry(stats: dict, ip: str) -> dict:
    """确保 IP 统计项存在并补齐字段。"""
    by_ip = stats.setdefault("by_ip", {})
    entry = by_ip.setdefault(ip, {})
    entry.setdefault("total", 0)
    entry.setdefault("success", 0)
    entry.setdefault("fail", 0)
    entry.setdefault("consecutive_fails", 0)
    entry.setdefault("is_banned", False)
    entry.setdefault("banned_at", None)
    entry.setdefault("last_seen_at", None)
    entry.setdefault("last_success_at", None)
    entry.setdefault("last_fail_at", None)
    entry.setdefault("last_fail_reason", None)
    if not isinstance(entry.get("paths"), dict):
        entry["paths"] = {}
    return entry


def ensure_ip_path_entry(ip_entry: dict, path: str) -> dict:
    paths = ip_entry.setdefault("paths", {})
    if not isinstance(paths, dict):
        paths = {}
        ip_entry["paths"] = paths
    normalized_path = normalize_request_path(path)
    path_entry = normalize_path_stats_entry(paths.setdefault(normalized_path, {}))
    paths[normalized_path] = path_entry
    return path_entry


def prune_ip_paths(ip_entry: dict, now_dt: Optional[datetime] = None) -> bool:
    """清理单个 IP 下超过 1 个月或超出数量上限的路径明细；累计计数不受影响。"""
    paths = ip_entry.get("paths")
    if not isinstance(paths, dict) or not paths:
        ip_entry["paths"] = {}
        return False

    now_dt = now_dt or beijing_now()
    cutoff = now_dt - timedelta(seconds=DETAIL_RETENTION_SECONDS)
    changed = False

    for path, path_entry in list(paths.items()):
        normalized_entry = normalize_path_stats_entry(path_entry)
        paths[path] = normalized_entry
        last_seen = parse_stats_datetime(normalized_entry.get("last_seen_at"))
        if last_seen and last_seen < cutoff:
            del paths[path]
            changed = True

    if len(paths) > MAX_IP_PATH_STATS:
        sorted_items = sorted(
            paths.items(),
            key=lambda item: (parse_stats_datetime(item[1].get("last_seen_at")) or datetime.min.replace(tzinfo=BEIJING_TZ)),
            reverse=True
        )
        kept = dict(sorted_items[:MAX_IP_PATH_STATS])
        if len(kept) != len(paths):
            ip_entry["paths"] = kept
            changed = True

    return changed


def prune_expired_path_details(stats: dict, force: bool = False) -> bool:
    """按天轻量清理路径明细，避免每次请求都全量扫描。"""
    global stats_last_path_cleanup_date
    today = beijing_date_str()
    if not force and stats_last_path_cleanup_date == today:
        return False

    changed = False
    for entry in stats.get("by_ip", {}).values():
        if isinstance(entry, dict):
            changed = prune_ip_paths(entry, beijing_now()) or changed
    stats_last_path_cleanup_date = today
    return changed


def normalize_stats_structure(stats: Any) -> dict:
    """兼容旧版 stats.json，补齐新增统计字段，避免旧数据读取后缺字段。"""
    default_stats = get_default_stats()
    if not isinstance(stats, dict):
        stats = default_stats

    if not isinstance(stats.get("total"), dict):
        stats["total"] = default_stats["total"]
    stats["total"].setdefault("success", 0)
    stats["total"].setdefault("fail", 0)

    if not isinstance(stats.get("today"), dict):
        stats["today"] = default_stats["today"]
    stats["today"].setdefault("date", default_stats["today"]["date"])
    stats["today"].setdefault("success", 0)
    stats["today"].setdefault("fail", 0)
    if not isinstance(stats["today"].get("by_config_id"), dict):
        stats["today"]["by_config_id"] = {}

    if not isinstance(stats.get("by_config_id"), dict):
        stats["by_config_id"] = {}
    if not isinstance(stats.get("round_robin_state"), dict):
        stats["round_robin_state"] = {}
    if not isinstance(stats.get("by_ip"), dict):
        stats["by_ip"] = {}
    for ip, entry in list(stats["by_ip"].items()):
        if not isinstance(entry, dict):
            del stats["by_ip"][ip]
            continue
        ensure_ip_stats_entry(stats, ip)
        prune_ip_paths(entry, beijing_now())

    return stats


async def load_stats_from_disk() -> dict:
    default_data = get_default_stats()
    stats = default_data
    try:
        async with file_lock:
            if os.path.exists(STATS_FILE):
                async with aiofiles.open(STATS_FILE, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    if content:
                        stats = json.loads(content)
            else:
                async with aiofiles.open(STATS_FILE, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(default_data, indent=2, ensure_ascii=False))
    except Exception as e:
        log_message(f"读取/创建 {STATS_FILE} 失败: {e}. 使用默认值。")
        stats = default_data
    return normalize_stats_structure(stats)


async def write_stats_to_disk(stats: dict):
    async with file_lock:
        async with aiofiles.open(STATS_FILE, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(stats, indent=2, ensure_ascii=False))


async def flush_stats_if_needed(force: bool = False):
    """将内存统计节流落盘；关键安全事件可 force=True。"""
    global stats_dirty, stats_last_flush_monotonic
    async with stats_lock:
        if stats_cache is None:
            return
        if not stats_dirty and not force:
            return
        now_mono = time.monotonic()
        if not force and now_mono - stats_last_flush_monotonic < STATS_FLUSH_INTERVAL_SECONDS:
            return
        snapshot = copy.deepcopy(stats_cache)
        stats_dirty = False
        stats_last_flush_monotonic = now_mono

    try:
        await write_stats_to_disk(snapshot)
    except Exception as e:
        async with stats_lock:
            stats_dirty = True
        log_message(f"写入 {STATS_FILE} 失败: {e}")


async def mark_stats_dirty(force_flush: bool = False):
    """标记统计缓存已变更；可在 stats_lock 内部调用，不能再次获取同一把锁。"""
    global stats_dirty
    stats_dirty = True
    if force_flush:
        await flush_stats_if_needed(force=True)


def reset_today_if_needed_unlocked(stats: dict) -> bool:
    today_str = beijing_date_str()
    if stats.get("today", {}).get("date") == today_str:
        return False
    stats["today"] = {
        "date": today_str,
        "success": 0,
        "fail": 0,
        "by_config_id": {}
    }
    return True


async def init_stats_cache() -> dict:
    global stats_cache, stats_dirty
    async with stats_lock:
        stats_cache = await load_stats_from_disk()
        changed = reset_today_if_needed_unlocked(stats_cache)
        changed = prune_expired_path_details(stats_cache, force=True) or changed
        stats_dirty = stats_dirty or changed
        snapshot = copy.deepcopy(stats_cache)
    if changed:
        await flush_stats_if_needed(force=True)
    return snapshot


async def get_stats() -> dict:
    """获取统计数据；缓存优先，避免统计页轮询反复读盘。"""
    global stats_cache, stats_dirty
    async with stats_lock:
        if stats_cache is None:
            stats_cache = await load_stats_from_disk()
        changed = reset_today_if_needed_unlocked(stats_cache)
        changed = prune_expired_path_details(stats_cache) or changed
        stats_dirty = stats_dirty or changed
        snapshot = copy.deepcopy(stats_cache)
    if changed:
        await flush_stats_if_needed()
    return snapshot


async def read_stats_unlocked() -> dict:
    """兼容旧调用名：返回内存统计快照，不再高频读盘。"""
    return await get_stats()


async def write_stats_unlocked(stats: dict):
    """兼容旧调用名：更新内存缓存并节流落盘。"""
    global stats_cache, stats_dirty
    async with stats_lock:
        stats_cache = normalize_stats_structure(stats)
        stats_dirty = True
    await flush_stats_if_needed()


def normalize_ip_candidate(value: Optional[str]) -> Optional[str]:
    """从头部候选值中提取并校验 IP，避免伪造头部携带任意字符串污染统计页。"""
    if not value:
        return None
    candidate = value.strip().strip('"').strip("'")
    if not candidate:
        return None

    if candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.rsplit(":", 1)[0]

    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def get_client_ip(request: Request) -> str:
    """优先从常见反代头识别真实客户端 IP，最后回退到直连地址。"""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        for item in forwarded_for.split(","):
            ip = normalize_ip_candidate(item)
            if ip:
                return ip

    for header_name in ["x-real-ip", "cf-connecting-ip"]:
        ip = normalize_ip_candidate(request.headers.get(header_name))
        if ip:
            return ip

    if request.client and request.client.host:
        ip = normalize_ip_candidate(request.client.host)
        if ip:
            return ip
    return "unknown"


def is_admin_authorization_valid(request: Request) -> bool:
    """判断请求是否携带正确管理员密钥；用于已封禁 IP 的安全解封通道。"""
    if not ADMIN_KEY:
        return False
    authorization = request.headers.get("authorization")
    x_api_key = request.headers.get("x-api-key")
    return authorization == f"Bearer {ADMIN_KEY}" or x_api_key == ADMIN_KEY


async def get_ip_stats_entry(ip: str) -> Optional[dict]:
    """读取指定 IP 的统计项；缓存优先。"""
    stats = await get_stats()
    entry = stats.get("by_ip", {}).get(ip)
    return entry if isinstance(entry, dict) else None


async def record_ip_request_seen(ip: str, path: str = "/"):
    """在全局中间件中记录每一次进入服务的请求 IP 和路径；仅更新内存并节流落盘。"""
    now_str = beijing_isoformat()
    normalized_path = normalize_request_path(path)
    async with stats_lock:
        if stats_cache is None:
            current_stats = await load_stats_from_disk()
            globals()["stats_cache"] = current_stats
        stats = stats_cache
        reset_today_if_needed_unlocked(stats)
        entry = ensure_ip_stats_entry(stats, ip)
        entry["total"] = int(entry.get("total", 0) or 0) + 1
        entry["last_seen_at"] = now_str
        path_entry = ensure_ip_path_entry(entry, normalized_path)
        path_entry["total"] = int(path_entry.get("total", 0) or 0) + 1
        path_entry["last_seen_at"] = now_str
        prune_ip_paths(entry, beijing_now())
        await mark_stats_dirty(False)
    await flush_stats_if_needed()


async def record_ip_auth_result(request: Request, is_success: bool, reason: str = ""):
    """记录认证成功/失败；连续失败达到阈值后永久封禁，成功则清零连续失败。"""
    ip = get_client_ip(request)
    path = normalize_request_path(request.url.path)
    now_str = beijing_isoformat()
    should_force_flush = False

    async with stats_lock:
        if stats_cache is None:
            current_stats = await load_stats_from_disk()
            globals()["stats_cache"] = current_stats
        stats = stats_cache
        reset_today_if_needed_unlocked(stats)
        entry = ensure_ip_stats_entry(stats, ip)
        entry["last_seen_at"] = now_str
        path_entry = ensure_ip_path_entry(entry, path)

        if is_success:
            entry["success"] = int(entry.get("success", 0) or 0) + 1
            entry["consecutive_fails"] = 0
            entry["last_success_at"] = now_str
            path_entry["success"] = int(path_entry.get("success", 0) or 0) + 1
            path_entry["last_success_at"] = now_str
        else:
            entry["fail"] = int(entry.get("fail", 0) or 0) + 1
            entry["consecutive_fails"] = int(entry.get("consecutive_fails", 0) or 0) + 1
            entry["last_fail_at"] = now_str
            entry["last_fail_reason"] = reason or "认证失败"
            path_entry["fail"] = int(path_entry.get("fail", 0) or 0) + 1
            path_entry["last_fail_at"] = now_str
            path_entry["last_fail_reason"] = reason or "认证失败"
            if entry["consecutive_fails"] >= IP_FAILURE_BAN_THRESHOLD and not entry.get("is_banned"):
                entry["is_banned"] = True
                entry["banned_at"] = now_str
                should_force_flush = True
                log_message(f"IP 自动封禁: {ip} 连续认证失败 {entry['consecutive_fails']} 次，已永久封禁，需控制台手动解除")

        prune_ip_paths(entry, beijing_now())
        await mark_stats_dirty(False)
    await flush_stats_if_needed(force=should_force_flush)


@app.middleware("http")
async def ip_ban_middleware(request: Request, call_next):
    """全局 IP 封禁拦截：覆盖所有路径；正确管理员密钥访问 IP 解封接口时允许穿透。"""
    ip = get_client_ip(request)
    request_path = normalize_request_path(request.url.path)
    await record_ip_request_seen(ip, request_path)
    entry = await get_ip_stats_entry(ip)
    is_banned = bool(entry and entry.get("is_banned"))

    is_ip_unblock_path = request.url.path.startswith("/admin/stats/ip/") and request.url.path.endswith("/unblock")
    if is_banned and not (is_ip_unblock_path and is_admin_authorization_valid(request)):
        return JSONResponse(status_code=403, content={
            "error": "当前 IP 已因连续认证失败被封禁，请在控制台手动解除",
            "ip": ip
        })

    return await call_next(request)


async def update_stats_and_state(
        config: ApiConfig,
        is_success: bool,
        scheme_name: str,
        priority_group: List[ApiConfig],
        success_index_in_group: int,
        advance_round_robin: bool = True
):
    """更新统计数据、熔断状态和轮询状态；内存优先，普通事件节流落盘。"""
    should_force_flush = False
    async with stats_lock:
        if stats_cache is None:
            current_stats = await load_stats_from_disk()
            globals()["stats_cache"] = current_stats
        stats = stats_cache
        reset_today_if_needed_unlocked(stats)

        key = "success" if is_success else "fail"
        stats["total"][key] = stats["total"].get(key, 0) + 1
        stats["today"][key] = stats["today"].get(key, 0) + 1

        if "by_config_id" not in stats:
            stats["by_config_id"] = {}
        if config.id not in stats["by_config_id"]:
            stats["by_config_id"][config.id] = {"success": 0, "fail": 0, "consecutive_fails": 0}

        if "by_config_id" not in stats["today"]:
            stats["today"]["by_config_id"] = {}
        if config.id not in stats["today"]["by_config_id"]:
            stats["today"]["by_config_id"][config.id] = {"success": 0, "fail": 0}

        stats["by_config_id"][config.id][key] = stats["by_config_id"][config.id].get(key, 0) + 1
        stats["today"]["by_config_id"][config.id][key] = stats["today"]["by_config_id"][config.id].get(key, 0) + 1

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
                disabled_until_time = beijing_now() + timedelta(seconds=duration)
                config_stats["disabled_until"] = disabled_until_time.isoformat()
                should_force_flush = True
                log_message(f"熔断触发: 配置项 ID {config.id} 已被禁用，直到 {disabled_until_time.strftime('%Y-%m-%d %H:%M:%S')}")

        if is_success and advance_round_robin:
            if "round_robin_state" not in stats:
                stats["round_robin_state"] = {}
            if scheme_name not in stats["round_robin_state"]:
                stats["round_robin_state"][scheme_name] = {}
            next_index = (success_index_in_group + 1) % len(priority_group) if priority_group else 0
            stats["round_robin_state"][scheme_name][str(config.priority)] = next_index

        await mark_stats_dirty(False)
    await flush_stats_if_needed(force=should_force_flush)


async def advance_round_robin_state_only(
        config: ApiConfig,
        scheme_name: str,
        priority_group: List[ApiConfig],
        success_index_in_group: int
):
    """仅推进配置项 round-robin 指针，不记录成功/失败，不影响熔断。用于图片 202 任务已被上游接收。"""
    async with stats_lock:
        if stats_cache is None:
            current_stats = await load_stats_from_disk()
            globals()["stats_cache"] = current_stats
        stats = stats_cache
        if "round_robin_state" not in stats:
            stats["round_robin_state"] = {}
        if scheme_name not in stats["round_robin_state"]:
            stats["round_robin_state"][scheme_name] = {}
        next_index = (success_index_in_group + 1) % len(priority_group) if priority_group else 0
        stats["round_robin_state"][scheme_name][str(config.priority)] = next_index
        await mark_stats_dirty(False)
    await flush_stats_if_needed()


# --- 4. 认证依赖 ---

async def verify_key(
        request: Request,
        authorization: Optional[str] = Header(None, description="认证密钥，格式: Bearer YOUR_ADMIN_KEY")
):
    """依赖项：验证 ADMIN_KEY，并记录来源 IP 的认证成功/失败。"""
    if not ADMIN_KEY:
        log_message("!!! 严重错误: ADMIN_KEY 未设置, 所有请求都将失败 !!!")
        await record_ip_auth_result(request, False, "ADMIN_KEY 未设置")
        raise HTTPException(status_code=500, detail="服务器内部错误: 认证未配置")

    if authorization != f"Bearer {ADMIN_KEY}":
        log_message(f"认证失败: 提供的 Key {authorization} 不正确")
        await record_ip_auth_result(request, False, "OpenAI/Admin Authorization 不匹配")
        raise HTTPException(status_code=401, detail="无效的认证密钥")

    await record_ip_auth_result(request, True)
    return True


async def verify_anthropic_key(
        request: Request,
        authorization: Optional[str] = Header(None, description="兼容 Bearer YOUR_ADMIN_KEY"),
        x_api_key: Optional[str] = Header(None, alias="x-api-key", description="Anthropic 风格 API Key"),
        anthropic_version: Optional[str] = Header(None, alias="anthropic-version", description="Anthropic API 版本，仅兼容读取")
):
    """Anthropic 入口鉴权：同时支持 Authorization: Bearer ADMIN_KEY 与 x-api-key: ADMIN_KEY。"""
    if not ADMIN_KEY:
        log_message("!!! 严重错误: ADMIN_KEY 未设置, 所有请求都将失败 !!!")
        await record_ip_auth_result(request, False, "ADMIN_KEY 未设置")
        raise HTTPException(status_code=500, detail="服务器内部错误: 认证未配置")

    bearer_ok = authorization == f"Bearer {ADMIN_KEY}"
    x_key_ok = x_api_key == ADMIN_KEY
    if not bearer_ok and not x_key_ok:
        log_message("Anthropic 入口认证失败: Authorization/x-api-key 均不匹配")
        await record_ip_auth_result(request, False, "Anthropic Authorization/x-api-key 均不匹配")
        raise HTTPException(status_code=401, detail="无效的认证密钥")

    await record_ip_auth_result(request, True)
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


def _as_token_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _iter_json_nodes(obj: Any, max_depth: int = 6):
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


def extract_total_tokens(payload: Any) -> Optional[int]:
    """兼容 OpenAI / Claude / Gemini 等常见响应结构的总 token 提取。"""

    # 1) 先尝试各种“总量字段”
    total_keys = [
        "total_tokens",
        "totalTokenCount",
        "total_token_count",
        "token_count"
    ]
    for node in _iter_json_nodes(payload):
        for key in total_keys:
            val = _as_token_int(node.get(key))
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

    for node in _iter_json_nodes(payload):
        for a, b in pair_keys:
            va = _as_token_int(node.get(a))
            vb = _as_token_int(node.get(b))
            if va is not None and vb is not None:
                total = va + vb

                # Gemini 常见附加字段，存在则叠加
                for extra_key in ["toolUsePromptTokenCount", "thoughtsTokenCount", "cachedContentTokenCount"]:
                    extra_val = _as_token_int(node.get(extra_key))
                    if extra_val is not None:
                        total += extra_val

                return total

    return None


def extract_cached_tokens(payload: Any) -> Optional[int]:
    """提取缓存命中的 token 数量，兼容常见 OpenAI / Claude / Gemini 命名。"""
    direct_keys = [
        "cached_tokens",                 # OpenAI: usage.prompt_tokens_details.cached_tokens
        "cachedTokens",
        "cached_token_count",
        "cachedTokenCount",
        "cache_read_input_tokens",       # Claude: usage.cache_read_input_tokens
        "cacheReadInputTokens",
        "cache_read_tokens",
        "cacheReadTokens",
        "cachedContentTokenCount",       # Gemini cached content
    ]
    for node in _iter_json_nodes(payload):
        for key in direct_keys:
            val = _as_token_int(node.get(key))
            if val is not None:
                return val

    detail_pairs = [
        ("prompt_tokens_details", "cached_tokens"),
        ("promptTokensDetails", "cachedTokens"),
        ("input_token_details", "cache_read"),
        ("inputTokenDetails", "cacheRead"),
    ]
    for node in _iter_json_nodes(payload):
        for outer_key, inner_key in detail_pairs:
            detail = node.get(outer_key)
            if not isinstance(detail, dict):
                continue
            val = _as_token_int(detail.get(inner_key))
            if val is not None:
                return val

    return None


_OPENAI_EDIT_DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$", re.DOTALL)


def image_media_type_to_filename(media_type: str) -> str:
    """根据图片 MIME 类型生成稳定的上传文件名。"""
    normalized = (media_type or "image/png").lower().split(";")[0].strip()
    extension_map = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/bmp": "bmp",
    }
    return f"image.{extension_map.get(normalized, 'png')}"


async def resolve_openai_edit_image_file(image_value: Any) -> tuple:
    """把 chat 多模态参考图转换为 OpenAI /images/edits 标准 multipart 文件字段。"""
    if not isinstance(image_value, str) or not image_value.strip():
        raise EndpointPresetError("OpenAI Edit 模式需要有效的参考图 URL 或 data URL")

    raw = image_value.strip()
    data_url_match = _OPENAI_EDIT_DATA_URL_RE.match(raw)
    if data_url_match:
        media_type = data_url_match.group(1)
        try:
            image_bytes = base64.b64decode(data_url_match.group(2), validate=False)
        except Exception as e:
            raise EndpointPresetError(f"OpenAI Edit 参考图 data URL 解码失败: {e}")
        if not image_bytes:
            raise EndpointPresetError("OpenAI Edit 参考图 data URL 为空")
        return (image_media_type_to_filename(media_type), image_bytes, media_type)

    if raw.startswith("http://") or raw.startswith("https://"):
        try:
            response = await httpx_client.get(raw)
            response.raise_for_status()
        except Exception as e:
            raise EndpointPresetError(f"OpenAI Edit 参考图下载失败: {e}")
        media_type = response.headers.get("content-type", "image/png").split(";")[0].strip() or "image/png"
        if not media_type.startswith("image/"):
            media_type = "image/png"
        image_bytes = response.content
        if not image_bytes:
            raise EndpointPresetError("OpenAI Edit 参考图下载结果为空")
        return (image_media_type_to_filename(media_type), image_bytes, media_type)

    raise EndpointPresetError("OpenAI Edit 标准 multipart 模式仅支持 data URL 或 http(s) 图片 URL")


def build_openai_edit_multipart_data(payload: Dict[str, Any]) -> Dict[str, str]:
    """从图片 payload 中提取 OpenAI /images/edits multipart 文本字段。"""
    data: Dict[str, str] = {}
    for key, value in payload.items():
        if key == "image" or value is None:
            continue
        if isinstance(value, bool):
            data[key] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            data[key] = json.dumps(value, ensure_ascii=False)
        else:
            data[key] = str(value)
    return data


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


def _nested_get(obj: Any, path: List[str]) -> Any:
    current = obj
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def extract_image_task_id(payload: Any) -> Optional[str]:
    """从常见 202 任务壳中提取 task id。"""
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("task_id"),
        payload.get("id"),
        _nested_get(payload, ["task", "id"]),
        _nested_get(payload, ["data", "task_id"]),
        _nested_get(payload, ["data", "id"]),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_image_task_status(payload: Any) -> Optional[str]:
    """从常见任务壳中提取状态。"""
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("status"),
        payload.get("state"),
        _nested_get(payload, ["task", "status"]),
        _nested_get(payload, ["task", "state"]),
        _nested_get(payload, ["data", "status"]),
        _nested_get(payload, ["data", "state"]),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def image_response_has_data(payload: Any) -> bool:
    """判断是否是可包装的图片最终响应。"""
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return False
    for item in data:
        if isinstance(item, dict):
            if isinstance(item.get("url"), str) and item.get("url").strip():
                return True
            if isinstance(item.get("b64_json"), str) and item.get("b64_json").strip():
                return True
    return False


def extract_image_task_poll_url(payload: Any, upstream_base_url: str, request_path: str, task_id: str) -> str:
    """从响应自描述 URL 或当前图片路径推导任务查询 URL。"""
    if isinstance(payload, dict):
        for key in ["poll_url", "status_url", "task_url"]:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                raw = value.strip()
                if raw.startswith("http://") or raw.startswith("https://"):
                    return raw
                return urljoin(upstream_base_url.rstrip("/") + "/", raw.lstrip("/"))

    normalized_path = "/" + str(request_path or "").strip().strip("/")
    if normalized_path.endswith("/images/generations") or normalized_path.endswith("/images/edits"):
        poll_path = f"/images/generations/{task_id}"
    else:
        poll_path = f"{normalized_path.rstrip('/')}/{task_id}"
    return f"{upstream_base_url.rstrip('/')}{poll_path}"


async def resolve_images_response_with_auto_poll(
        initial_response: httpx.Response,
        upstream_base_url: str,
        request_path: str,
        headers: Dict[str, str],
        config: ApiConfig,
        ensure_client_connected,
        on_task_accepted=None
) -> Dict[str, Any]:
    """处理图片响应：同步 data 直返；HTTP 202 + task_id 自动轮询同一上游任务。"""
    try:
        payload = initial_response.json()
    except Exception as e:
        raise EndpointPresetError(f"上游 images 响应不是有效 JSON: {e}")

    if image_response_has_data(payload):
        return payload

    if initial_response.status_code != 202:
        raise EndpointPresetError("上游 images 响应缺少 data 数组")

    task_id = extract_image_task_id(payload)
    if not task_id:
        raise EndpointPresetError("上游 images 返回 202 但缺少 task_id/id")

    poll_url = extract_image_task_poll_url(payload, upstream_base_url, request_path, task_id)
    timeout_seconds = config.image_task_poll_timeout_seconds if config.image_task_poll_timeout_seconds is not None else 300
    interval_seconds = config.image_task_poll_interval_seconds if config.image_task_poll_interval_seconds is not None else 2.0
    try:
        timeout_seconds = max(1, int(timeout_seconds))
    except Exception:
        timeout_seconds = 300
    try:
        interval_seconds = max(0.5, float(interval_seconds))
    except Exception:
        interval_seconds = 2.0

    if on_task_accepted:
        await on_task_accepted(task_id, poll_url)

    waiting_statuses = {"queued", "pending", "running", "processing", "in_progress", "created", "accepted"}
    failure_statuses = {"failed", "failure", "error", "cancelled", "canceled", "refunded", "expired"}

    deadline = beijing_now() + timedelta(seconds=timeout_seconds)
    poll_count = 0
    last_payload: Any = payload
    while beijing_now() < deadline:
        await ensure_client_connected()
        await asyncio.sleep(interval_seconds)
        await ensure_client_connected()
        poll_count += 1

        response = await httpx_client.get(poll_url, headers=headers)
        if response.status_code >= 400:
            raise EndpointPresetError(f"图片任务 {task_id} 查询失败 (HTTP {response.status_code}): {sanitize_log_text(response.text)}")
        try:
            current_payload = response.json()
        except Exception as e:
            raise EndpointPresetError(f"图片任务 {task_id} 查询响应不是有效 JSON: {e}")
        last_payload = current_payload

        if image_response_has_data(current_payload):
            log_message(f"图片任务 {task_id} 第 {poll_count} 次查询获得最终 data")
            return current_payload

        status = extract_image_task_status(current_payload)
        if status in failure_statuses:
            raise EndpointPresetError(f"图片任务 {task_id} 状态失败: {status}")
        if status and status not in waiting_statuses:
            log_message(f"图片任务 {task_id} 返回未知状态 '{status}'，继续等待直到超时")

    try:
        excerpt = json.dumps(last_payload, ensure_ascii=False)[:500]
    except Exception:
        excerpt = str(last_payload)[:500]
    raise EndpointPresetError(f"图片任务 {task_id} 等待超时 ({timeout_seconds}s)，最后响应: {excerpt}")


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
        "created": first_obj.get("created") if isinstance(first_obj.get("created"), int) else int(beijing_now().timestamp()),
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
    now_time = beijing_now()

    # 过滤掉被熔断的配置
    active_configs = []
    for config in target_scheme_configs:
        config_stats = stats.get("by_config_id", {}).get(config.id, {})
        disabled_until_str = config_stats.get("disabled_until")
        if disabled_until_str:
            disabled_until_time = parse_stats_datetime(disabled_until_str)
            if disabled_until_time and now_time < disabled_until_time:
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
        upstream_base_url = config.url.strip()
        upstream_api_key = config.api_key.strip()
        upstream_model = config.model.strip() if isinstance(config.model, str) and config.model.strip() else None
        proxy_url = f"{upstream_base_url.rstrip('/')}/chat/completions"

        mode_plan = resolve_stream_modes(is_stream, config.stream_mode_strategy)
        upstream_is_stream = mode_plan["upstream_is_stream"]
        downstream_is_stream = mode_plan["downstream_is_stream"]

        proxy_headers = {
            "Authorization": f"Bearer {upstream_api_key}",
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
            if upstream_model:
                proxy_body["model"] = upstream_model

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
                    images_path, images_body = build_images_request_plan(proxy_body, config)
                    images_proxy_url = f"{upstream_base_url.rstrip('/')}{images_path}"
                    images_headers = dict(proxy_headers)
                    images_headers["Accept"] = "application/json"
                    if request_body.get("stream", False):
                        log_message(f"配置项 ID: {config.id} 使用 images_generations 预设，上游按非流式请求，向下游返回 fake SSE 图片 markdown")

                    is_standard_openai_edit = config.image_upstream_mode == "openai_edit_image" and images_path.rstrip("/").endswith("/images/edits") and isinstance(images_body.get("image"), str)
                    if is_standard_openai_edit:
                        multipart_headers = dict(images_headers)
                        multipart_headers.pop("Content-Type", None)
                        image_file = await resolve_openai_edit_image_file(images_body.get("image"))
                        multipart_data = build_openai_edit_multipart_data(images_body)
                        response = await httpx_client.post(
                            images_proxy_url,
                            headers=multipart_headers,
                            data=multipart_data,
                            files={"image": image_file}
                        )
                    else:
                        response = await httpx_client.post(images_proxy_url, headers=images_headers, json=images_body)
                    response.raise_for_status()
                    image_task_accepted = False

                    async def on_image_task_accepted(task_id: str, poll_url: str):
                        nonlocal image_task_accepted
                        if image_task_accepted:
                            return
                        image_task_accepted = True
                        log_message(f"配置项 ID: {config.id} Images 任务已被上游接受 (task_id={task_id})，提前推进配置项轮询，查询地址: {poll_url}")
                        await advance_round_robin_state_only(config, scheme_name, original_group, success_index_in_group)

                    response_json = await resolve_images_response_with_auto_poll(
                        response,
                        upstream_base_url.rstrip('/'),
                        images_path,
                        images_headers,
                        config,
                        ensure_client_connected,
                        on_task_accepted=on_image_task_accepted
                    )
                    image_public_url_prefix = str(request.base_url).rstrip("/") + GENERATED_IMAGES_ROUTE
                    response_json = normalize_image_response_urls(response_json, upstream_base_url)
                    response_json = convert_response_base64_images_to_urls(
                        response_json,
                        GENERATED_IMAGES_DIR,
                        image_public_url_prefix,
                        image_saver=save_decoded_image_to_memory
                    )
                    wrapped_json = wrap_image_response_as_chat_completion(
                        response_json,
                        request_body,
                        config,
                        image_output_dir=GENERATED_IMAGES_DIR,
                        image_public_url_prefix=image_public_url_prefix,
                        image_saver=save_decoded_image_to_memory
                    )

                    if show_full_response_body:
                        try:
                            log_message(f"响应体完整内容: {json.dumps(wrapped_json, ensure_ascii=False)}")
                        except Exception:
                            log_message(f"响应体完整内容(序列化失败，使用字符串): {str(wrapped_json)}")

                    log_message(f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次 Images 预设请求成功 (path={images_path})")
                    await update_stats_and_state(
                        config,
                        True,
                        scheme_name,
                        original_group,
                        success_index_in_group,
                        advance_round_robin=not image_task_accepted
                    )

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
                        safe_error_text = sanitize_log_text(error_text)
                        log_message(
                            f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次失败 (HTTP {response.status_code}, 策略={mode_plan['mode_label']}): {safe_error_text}")
                        last_error = f"HTTP {response.status_code}: {safe_error_text}"
                        if show_full_response_body:
                            log_message(f"响应体完整内容: {safe_error_text}")
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
                            cached_tokens = None

                            def try_extract_usage_from_sse_line(line_text: str):
                                nonlocal total_tokens, cached_tokens
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

                                extracted_cached = extract_cached_tokens(data_obj)
                                if extracted_cached is not None:
                                    cached_tokens = extracted_cached

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

                                total_tokens_text = total_tokens if total_tokens is not None else "未知"
                                cached_tokens_text = cached_tokens if cached_tokens is not None else "未知"
                                log_message(f"配置项 ID: {successful_config.id} 流式请求结束 (total_tokens={total_tokens_text}, cached_tokens={cached_tokens_text}, 策略={mode_plan['mode_label']})")
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
                            f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次流式请求启动成功 (HTTP {response.status_code}, 策略={mode_plan['mode_label']})")
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
                    image_public_url_prefix = str(request.base_url).rstrip("/") + GENERATED_IMAGES_ROUTE
                    merged_json = convert_response_base64_images_to_urls(
                        merged_json,
                        GENERATED_IMAGES_DIR,
                        image_public_url_prefix,
                        image_saver=save_decoded_image_to_memory
                    )
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
                image_public_url_prefix = str(request.base_url).rstrip("/") + GENERATED_IMAGES_ROUTE
                response_json = convert_response_base64_images_to_urls(
                    response_json,
                    GENERATED_IMAGES_DIR,
                    image_public_url_prefix,
                    image_saver=save_decoded_image_to_memory
                )
                if show_full_response_body:
                    try:
                        log_message(f"响应体完整内容: {json.dumps(response_json, ensure_ascii=False)}")
                    except Exception:
                        log_message(f"响应体完整内容(序列化失败，使用字符串): {str(response_json)}")
                total_tokens = extract_total_tokens(response_json)

                if downstream_is_stream:
                    # 假流式：上游非流，转换为标准 chat.completion.chunk + [DONE]
                    async def fake_stream_generator(final_payload: Dict[str, Any]):
                        choice = {}
                        try:
                            choices = final_payload.get("choices")
                            if isinstance(choices, list) and choices:
                                choice = choices[0] if isinstance(choices[0], dict) else {}
                        except Exception:
                            choice = {}

                        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                        content = message.get("content") if isinstance(message.get("content"), str) else ""
                        role = message.get("role") if isinstance(message.get("role"), str) else "assistant"

                        chunk_payload = {
                            "id": final_payload.get("id"),
                            "object": "chat.completion.chunk",
                            "created": final_payload.get("created"),
                            "model": final_payload.get("model"),
                            "choices": [
                                {
                                    "index": choice.get("index", 0) if isinstance(choice, dict) else 0,
                                    "delta": {"role": role, "content": content},
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
                            "choices": [
                                {
                                    "index": choice.get("index", 0) if isinstance(choice, dict) else 0,
                                    "delta": {},
                                    "finish_reason": choice.get("finish_reason") or "stop"
                                }
                            ]
                        }
                        yield f"data: {json.dumps(done_payload, ensure_ascii=False)}\n\n".encode("utf-8")
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
                safe_error_text = sanitize_log_text(e.response.text)
                log_message(
                    f"配置项 ID: {config.id} 第 {attempt_no + 1}/{max_retries + 1} 次失败 (HTTP {e.response.status_code}): {safe_error_text}")
                if show_full_response_body:
                    log_message(f"响应体完整内容: {safe_error_text}")
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


@admin_router.post("/stats/config/{config_id}/unblock")
async def unblock_config(config_id: str):
    """手动解除指定配置项的熔断禁用状态；关键事件强制刷新到磁盘。"""
    changed = False
    async with stats_lock:
        if stats_cache is None:
            globals()["stats_cache"] = await load_stats_from_disk()
        stats = stats_cache
        config_stats = stats.get("by_config_id", {}).get(config_id)
        if not isinstance(config_stats, dict):
            raise HTTPException(status_code=404, detail="未找到该配置项的统计记录")

        if "disabled_until" in config_stats:
            del config_stats["disabled_until"]
            changed = True
        if config_stats.get("consecutive_fails", 0) != 0:
            config_stats["consecutive_fails"] = 0
            changed = True
        if changed:
            await mark_stats_dirty(False)

    if changed:
        await flush_stats_if_needed(force=True)
        log_message(f"管理: 手动解除配置项 {config_id} 的熔断禁用状态")

    return {"ok": True, "config_id": config_id, "changed": changed}


@admin_router.post("/stats/ip/{ip}/unblock")
async def unblock_ip(ip: str):
    """手动解除指定 IP 的封禁状态；关键事件强制刷新到磁盘。"""
    changed = False
    async with stats_lock:
        if stats_cache is None:
            globals()["stats_cache"] = await load_stats_from_disk()
        stats = stats_cache
        ip_stats = stats.get("by_ip", {}).get(ip)
        if not isinstance(ip_stats, dict):
            raise HTTPException(status_code=404, detail="未找到该 IP 的统计记录")

        if ip_stats.get("is_banned"):
            ip_stats["is_banned"] = False
            changed = True
        if ip_stats.get("banned_at") is not None:
            ip_stats["banned_at"] = None
            changed = True
        if ip_stats.get("consecutive_fails", 0) != 0:
            ip_stats["consecutive_fails"] = 0
            changed = True
        if changed:
            await mark_stats_dirty(False)

    if changed:
        await flush_stats_if_needed(force=True)
        log_message(f"管理: 手动解除 IP {ip} 的封禁状态")

    return {"ok": True, "ip": ip, "changed": changed}


@admin_router.get("/logs")
async def get_logs() -> List[str]:
    """获取最新的 200 条内存日志"""
    return list(log_deque)


@admin_router.post("/models/query")
async def query_upstream_models(query: UpstreamModelQueryRequest, request: Request) -> Dict[str, Any]:
    """使用当前表单中未保存的 URL/Key/UA 模式查询上游 OpenAI 兼容模型列表。"""
    upstream_url = f"{query.url.strip().rstrip('/')}/models"
    headers = {
        "Authorization": f"Bearer {query.api_key.strip()}",
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


async def stats_periodic_flush_loop():
    """后台周期性落盘脏统计，降低请求主链路 IO 压力。"""
    while True:
        try:
            await asyncio.sleep(STATS_FLUSH_INTERVAL_SECONDS)
            await flush_stats_if_needed()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_message(f"统计后台刷盘任务异常: {e}")


def prune_expired_memory_generated_images(now_ts: Optional[float] = None) -> int:
    """清理过期的内存生成图片；不设置容量上限，只按 TTL 生命周期回收。"""
    now = now_ts or time.time()
    deleted = 0
    with memory_generated_images_lock:
        for filename, entry in list(memory_generated_images.items()):
            expires_at = float(entry.get("expires_at", 0) or 0)
            if expires_at <= now:
                memory_generated_images.pop(filename, None)
                deleted += 1
    return deleted


def save_decoded_image_to_memory(
        decoded: Dict[str, Any],
        output_dir: Optional[str],
        public_url_prefix: Optional[str]
) -> Optional[str]:
    """把 base64 解码后的图片保存到短生命周期内存缓存，并返回兼容的公开 URL。"""
    if not public_url_prefix:
        return None
    image_bytes = decoded.get("bytes")
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        return None

    prune_expired_memory_generated_images()
    extension = str(decoded.get("extension") or ".png")
    if not extension.startswith("."):
        extension = f".{extension}"
    filename = f"img_{int(time.time())}_{uuid.uuid4().hex}{extension}"
    now = time.time()
    with memory_generated_images_lock:
        memory_generated_images[filename] = {
            "bytes": bytes(image_bytes),
            "media_type": str(decoded.get("media_type") or "image/png"),
            "created_at": now,
            "expires_at": now + MEMORY_GENERATED_IMAGE_TTL_SECONDS,
        }
    return f"{public_url_prefix.rstrip('/')}/{filename}"


def get_memory_generated_image(filename: str) -> Optional[Dict[str, Any]]:
    """读取未过期的内存生成图片；访问时顺手清理当前过期条目。"""
    now = time.time()
    with memory_generated_images_lock:
        entry = memory_generated_images.get(filename)
        if not entry:
            return None
        expires_at = float(entry.get("expires_at", 0) or 0)
        if expires_at <= now:
            memory_generated_images.pop(filename, None)
            return None
        return dict(entry)


async def cleanup_generated_images_once() -> int:
    """删除超过保留期的生成图片文件；失败只写日志，不影响主服务。"""
    cutoff_ts = time.time() - DETAIL_RETENTION_SECONDS
    deleted = 0
    try:
        for filename in os.listdir(GENERATED_IMAGES_DIR):
            file_path = os.path.join(GENERATED_IMAGES_DIR, filename)
            try:
                if not os.path.isfile(file_path):
                    continue
                if os.path.getmtime(file_path) < cutoff_ts:
                    os.remove(file_path)
                    deleted += 1
            except Exception as e:
                log_message(f"清理生成图片失败 {file_path}: {e}")
    except Exception as e:
        log_message(f"扫描生成图片目录失败: {e}")
    if deleted:
        log_message(f"已清理超过 {DETAIL_RETENTION_DAYS} 天的生成图片 {deleted} 个")
    return deleted


async def generated_image_cleanup_loop():
    """后台周期清理过期生成图片；新图片清理内存，旧磁盘文件低频兼容清理。"""
    last_disk_cleanup_ts = 0.0
    while True:
        try:
            deleted_memory = prune_expired_memory_generated_images()
            if deleted_memory:
                log_message(f"已清理过期内存生成图片 {deleted_memory} 个")

            now = time.time()
            if now - last_disk_cleanup_ts >= GENERATED_IMAGE_CLEANUP_INTERVAL_SECONDS:
                await cleanup_generated_images_once()
                last_disk_cleanup_ts = now

            await asyncio.sleep(MEMORY_GENERATED_IMAGE_CLEANUP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_message(f"生成图片后台清理任务异常: {e}")
            await asyncio.sleep(60)


# --- 7. 启动和关闭事件 ---

@app.on_event("startup")
async def startup_event():
    """应用启动时执行"""
    global stats_flush_task, generated_image_cleanup_task
    if not ADMIN_KEY:
        log_message("=" * 50)
        log_message("!!! 严重警告: 环境变量 'ADMIN_KEY' 未设置 !!!")
        log_message("!!! 服务已启动, 但所有 API 请求都将因 401/500 错误而失败 !!!")
        log_message("=" * 50)
    else:
        log_message(f"服务启动，ADMIN_KEY 已加载。")

    log_message("正在初始化配置与统计缓存...")
    await refresh_config_cache()
    await init_stats_cache()
    await cleanup_generated_images_once()
    stats_flush_task = asyncio.create_task(stats_periodic_flush_loop())
    generated_image_cleanup_task = asyncio.create_task(generated_image_cleanup_loop())
    log_message(f"{PROJECT_NAME} 已启动，监听端口 {PORT}")


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时执行"""
    for task in [stats_flush_task, generated_image_cleanup_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    await flush_stats_if_needed(force=True)
    await httpx_client.aclose()
    log_message(f"{PROJECT_NAME} 正在关闭")


# --- 8. 静态文件服务 (用于前端和生成图片) ---

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get(f"{GENERATED_IMAGES_ROUTE}/{{filename:path}}", tags=["Generated Images"])
async def read_generated_image(filename: str):
    """从短生命周期内存缓存返回生成图片；找不到时兼容读取历史磁盘文件。"""
    safe_filename = os.path.basename(filename)
    if not safe_filename or safe_filename != filename:
        raise HTTPException(status_code=404, detail="图片不存在或已过期")

    entry = get_memory_generated_image(safe_filename)
    if entry:
        headers = {
            "Cache-Control": f"private, max-age={max(MEMORY_GENERATED_IMAGE_TTL_SECONDS, 0)}",
            "X-Generated-Image-Storage": "memory",
        }
        return Response(
            content=entry.get("bytes") or b"",
            media_type=str(entry.get("media_type") or "image/png"),
            headers=headers
        )

    legacy_file_path = os.path.join(GENERATED_IMAGES_DIR, safe_filename)
    if os.path.isfile(legacy_file_path):
        return FileResponse(legacy_file_path)

    raise HTTPException(status_code=404, detail="图片不存在或已过期")


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