"""
MaiBot_SNS - 社交平台信息采集与记忆写入插件

通过MCP桥接调用社交平台API（如小红书），采集内容并写入ChatHistory记忆系统。
支持做梦模块集成、定时任务和手动命令触发。
"""

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Tuple, Type, Optional, Dict, Any

from src.plugin_system import (
    BasePlugin,
    BaseCommand,
    BaseTool,
    ComponentInfo,
    ConfigField,
    ToolParamType,
    register_plugin,
    get_logger,
)
from src.plugin_system.base.config_types import ConfigSection
from src.plugin_system.base.base_events_handler import BaseEventHandler
from src.plugin_system.base.component_types import EventType
from src.plugin_system.base.component_types import PythonDependency
from src.plugin_system.apis import tool_api, llm_api, database_api
from src.common.database.database_model import ChatHistory

logger = get_logger("maibot_sns")

def _get_data_dir() -> Path:
    """获取插件运行时可写目录（默认 data/maibot_sns，可用环境变量覆盖）。"""
    env_dir = os.getenv("MAIBOT_SNS_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return Path("data") / "maibot_sns"


def _ensure_data_dir() -> Path:
    data_dir = _get_data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return data_dir


# 运行时文件路径（严禁写入插件目录）
DATA_DIR = _ensure_data_dir()
CACHE_FILE = DATA_DIR / "failed_writes.json"
STATE_FILE = DATA_DIR / "collector_state.json"


def _load_state() -> Dict[str, Any]:
    """加载插件状态（预览缓存等）。"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    """保存插件状态（预览缓存等）。"""
    try:
        _ensure_data_dir()
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"保存状态失败: {e}")


def _normalize_config(config: Any) -> Dict[str, Any]:
    """将包含点号键的配置归一化为嵌套 dict，兼容多种 config 结构。"""
    if not isinstance(config, dict):
        return {}

    def deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
        for k, v in src.items():
            if k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
                deep_merge(dst[k], v)
            else:
                dst[k] = v
        return dst

    def set_dotted(dst: Dict[str, Any], dotted_key: str, value: Any) -> None:
        parts = [p for p in dotted_key.split(".") if p]
        if not parts:
            return
        cur = dst
        for p in parts[:-1]:
            if p not in cur or not isinstance(cur.get(p), dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = value

    out: Dict[str, Any] = {}
    for k, v in config.items():
        v_norm = _normalize_config(v) if isinstance(v, dict) else v
        if isinstance(k, str) and "." in k:
            set_dotted(out, k, v_norm)
        else:
            if k in out and isinstance(out.get(k), dict) and isinstance(v_norm, dict):
                deep_merge(out[k], v_norm)  # type: ignore[arg-type]
            else:
                out[k] = v_norm
    return out

# 全局状态（用于 WebUI 和统计）
_collector_stats: Dict[str, Any] = {
    "last_collect_time": 0,
    "total_collected": 0,
    "total_written": 0,
    "total_filtered": 0,
    "total_duplicate": 0,
    "last_result": None,
    "is_running": False,
    "recent_memories": [],  # 最近写入的记忆
}

# feed_id 缓存（避免重复查询数据库），存储格式: "{platform}:{feed_id}"
_feed_id_cache: set = set()
_feed_id_cache_loaded: bool = False


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class SNSContent:
    """社交平台内容（通用格式）"""
    feed_id: str           # 内容唯一标识
    platform: str          # 平台名称
    title: str             # 标题
    content: str           # 正文内容
    author: str            # 作者
    like_count: int = 0    # 点赞/喜欢数
    comment_count: int = 0 # 评论数
    image_urls: List[str] = field(default_factory=list)  # 图片列表
    url: str = ""          # 原文链接
    extra: Dict[str, Any] = field(default_factory=dict)  # 额外数据（平台特定）


@dataclass
class CollectResult:
    """采集结果"""
    success: bool
    fetched: int = 0
    written: int = 0
    filtered: int = 0
    duplicate: int = 0
    errors: List[str] = field(default_factory=list)
    preview_contents: List[Dict] = field(default_factory=list)  # 预览内容
    preview_items: List[SNSContent] = field(default_factory=list, repr=False)  # 预览条目（用于确认写入）
    
    def summary(self) -> str:
        status = "✅" if self.success else "❌"
        return f"{status} 获取:{self.fetched} 写入:{self.written} 过滤:{self.filtered} 重复:{self.duplicate}"


# ============================================================================
# 平台适配器（支持多平台扩展）
# ============================================================================

class PlatformAdapter:
    """平台适配器基类 - 定义如何解析不同平台的数据"""
    
    platform_name: str = "generic"
    
    # 工具名映射（可在配置中覆盖）
    default_tools = {
        "list": "list_feeds",      # 获取列表
        "search": "search_feeds",  # 搜索
        "detail": "get_feed_detail",  # 获取详情
    }
    
    # 字段映射（从 MCP 返回数据映射到 SNSContent）
    field_mapping = {
        "feed_id": ["id", "note_id", "feed_id", "item_id"],
        "title": ["title", "displayTitle", "name", "headline"],
        "content": ["content", "desc", "description", "text", "body"],
        "author": ["author", "nickname", "user.nickname", "user.name", "creator"],
        "like_count": ["likedCount", "like_count", "likes", "interactInfo.likedCount"],
        "comment_count": ["commentCount", "comment_count", "comments", "interactInfo.commentCount"],
        "images": ["images", "imageList", "image_list", "cover", "pics"],
        "url": ["url", "link", "webUrl", "share_url"],
    }
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        # 允许配置覆盖默认映射
        custom_mapping = config.get("field_mapping", {})
        if custom_mapping:
            self.field_mapping = {**self.field_mapping, **custom_mapping}
        custom_tools = config.get("tools", {})
        if custom_tools:
            self.default_tools = {**self.default_tools, **custom_tools}
    
    def _get_nested_value(self, data: Dict, path: str) -> Any:
        """获取嵌套字典的值，支持点号路径如 'user.nickname'"""
        keys = path.split(".")
        value = data
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return None
        return value
    
    def _extract_field(self, data: Dict, field_name: str) -> Any:
        """从数据中提取字段，尝试多个可能的键名"""
        paths = self.field_mapping.get(field_name, [field_name])
        for path in paths:
            value = self._get_nested_value(data, path)
            if value is not None:
                return value
        return None
    
    def parse_list_result(self, result: str) -> List[SNSContent]:
        """解析列表结果（通用实现）"""
        contents = []
        
        if not result or not result.strip():
            return contents
        
        try:
            data = json.loads(result)
            
            # 支持多种返回格式
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                # 尝试多种可能的列表字段
                for key in ["items", "feeds", "notes", "data", "list", "results"]:
                    if key in data and isinstance(data[key], list):
                        items = data[key]
                        break
                else:
                    items = [data]  # 单条数据
            else:
                return contents
            
            for item in items:
                if not isinstance(item, dict):
                    continue
                
                content = self._parse_item(item)
                if content and content.feed_id:
                    contents.append(content)
                    
        except json.JSONDecodeError:
            logger.debug(f"非JSON格式结果，长度={len(result)}")
        except Exception as e:
            logger.warning(f"解析列表结果失败: {e}")
        
        return contents
    
    def _parse_item(self, item: Dict) -> Optional[SNSContent]:
        """解析单个内容项（可被子类覆盖）"""
        feed_id = str(self._extract_field(item, "feed_id") or "")
        if not feed_id:
            return None
        
        # 提取点赞数
        like_count = self._extract_field(item, "like_count") or 0
        if isinstance(like_count, str):
            like_count = self._parse_count(like_count)
        
        # 提取评论数
        comment_count = self._extract_field(item, "comment_count") or 0
        if isinstance(comment_count, str):
            comment_count = self._parse_count(comment_count)
        
        # 提取图片
        images = []
        img_data = self._extract_field(item, "images")
        if img_data:
            images = self._extract_images(img_data)
        
        return SNSContent(
            feed_id=feed_id,
            platform=self.platform_name,
            title=str(self._extract_field(item, "title") or ""),
            content=str(self._extract_field(item, "content") or ""),
            author=str(self._extract_field(item, "author") or ""),
            like_count=int(like_count),
            comment_count=int(comment_count),
            image_urls=images,
            url=str(self._extract_field(item, "url") or ""),
            extra=item,  # 保留原始数据
        )
    
    def _parse_count(self, count_str: str) -> int:
        """解析数量字符串（处理 '1.5万' 等格式）"""
        try:
            count_str = count_str.replace(",", "").strip()
            if "万" in count_str:
                return int(float(count_str.replace("万", "")) * 10000)
            if "k" in count_str.lower():
                return int(float(count_str.lower().replace("k", "")) * 1000)
            return int(float(count_str))
        except (ValueError, AttributeError):
            return 0
    
    def _extract_images(self, img_data: Any) -> List[str]:
        """提取图片 URL 列表"""
        images = []
        
        if isinstance(img_data, str):
            images.append(img_data)
        elif isinstance(img_data, list):
            for img in img_data:
                if isinstance(img, str):
                    images.append(img)
                elif isinstance(img, dict):
                    # 尝试多种 URL 字段
                    for key in ["urlDefault", "url", "src", "originUrl", "url_default"]:
                        if img.get(key):
                            images.append(img[key])
                            break
        elif isinstance(img_data, dict):
            for key in ["urlDefault", "url", "src"]:
                if img_data.get(key):
                    images.append(img_data[key])
                    break
        
        return images
    
    def parse_detail_result(self, result: str, content: SNSContent) -> SNSContent:
        """解析详情结果，更新 content"""
        try:
            data = json.loads(result)
            
            # 尝试找到主要数据
            detail = None
            if isinstance(data, dict):
                for key in ["data", "note", "detail", "item"]:
                    if key in data:
                        detail = data[key]
                        if isinstance(detail, dict) and "note" in detail:
                            detail = detail["note"]
                        break
                if not detail:
                    detail = data
            
            if detail:
                # 更新正文
                new_content = self._extract_field(detail, "content")
                if new_content:
                    content.content = str(new_content)
                
                # 更新图片
                img_data = self._extract_field(detail, "images")
                if img_data:
                    content.image_urls = self._extract_images(img_data)
                
                # 更新 extra
                content.extra.update(detail)
                
        except Exception as e:
            logger.debug(f"解析详情失败: {e}")
        
        return content
    
    def get_content_url(self, content: SNSContent) -> str:
        """获取内容的原始链接"""
        if content.url:
            return content.url
        return ""


class XiaohongshuAdapter(PlatformAdapter):
    """小红书平台适配器"""
    
    platform_name = "xiaohongshu"
    
    field_mapping = {
        "feed_id": ["id", "note_id"],
        "title": ["noteCard.displayTitle", "displayTitle", "title"],
        "content": ["noteCard.desc", "desc", "content"],
        "author": ["noteCard.user.nickname", "user.nickname", "nickname"],
        "like_count": ["noteCard.interactInfo.likedCount", "interactInfo.likedCount", "likedCount"],
        "comment_count": ["noteCard.interactInfo.commentCount", "interactInfo.commentCount", "commentCount"],
        "images": ["noteCard.cover", "cover", "imageList", "images"],
        "url": [],
    }
    
    def _parse_item(self, item: Dict) -> Optional[SNSContent]:
        """小红书特定解析"""
        # 小红书的数据可能在 noteCard 中
        note_card = item.get("noteCard", {})
        if note_card:
            # 合并 noteCard 数据到 item
            merged = {**item, **note_card}
            merged["noteCard"] = note_card  # 保留原始结构
        else:
            merged = item
        
        content = super()._parse_item(merged)
        if content:
            # 保存 xsec_token 用于获取详情
            content.extra["xsec_token"] = item.get("xsecToken", "")
        return content
    
    def get_content_url(self, content: SNSContent) -> str:
        return f"https://xiaohongshu.com/explore/{content.feed_id}"


# 平台适配器注册表
PLATFORM_ADAPTERS: Dict[str, Type[PlatformAdapter]] = {
    "xiaohongshu": XiaohongshuAdapter,
    "generic": PlatformAdapter,
}

def get_platform_adapter(platform: str, config: Dict[str, Any]) -> PlatformAdapter:
    """获取平台适配器"""
    adapter_class = PLATFORM_ADAPTERS.get(platform, PlatformAdapter)
    return adapter_class(config)


# ============================================================================
# 核心功能
# ============================================================================

class SNSCollector:
    """SNS内容采集器 - 支持多平台"""
    
    # 并发控制
    MAX_CONCURRENT_DETAILS = 3  # 最大并发获取详情数
    
    def __init__(self, config: Dict[str, Any]):
        self.config = _normalize_config(config)
        self.platform_cfg = _normalize_config(self.config.get("platform", {}))
        self.filter_cfg = _normalize_config(self.config.get("filter", {}))
        self.memory_cfg = _normalize_config(self.config.get("memory", {}))
        self.processing_cfg = _normalize_config(self.config.get("processing", {}))
        self.debug = bool(_normalize_config(self.config.get("debug", {})).get("enabled", False))
        self._personality_cache: Optional[Dict[str, str]] = None
        self._semaphore_details = asyncio.Semaphore(self.MAX_CONCURRENT_DETAILS)
        self._adapters: Dict[str, PlatformAdapter] = {}
    
    def _get_adapter(self, platform: str) -> PlatformAdapter:
        """获取或创建平台适配器"""
        if platform not in self._adapters:
            platform_config = self.platform_cfg.get(platform, {})
            self._adapters[platform] = get_platform_adapter(platform, platform_config)
        return self._adapters[platform]
    
    @staticmethod
    def _make_cache_key(platform: str, feed_id: str) -> str:
        return f"{platform}:{feed_id}"

    @staticmethod
    def _extract_feed_id_from_key_point(key_point: Any) -> Optional[str]:
        """从 ChatHistory.key_point 中提取 feed_id（兼容 JSON list / 纯文本）"""
        if not key_point:
            return None

        if isinstance(key_point, list):
            for item in key_point:
                if isinstance(item, str) and item.startswith("feed_id:"):
                    value = item.split("feed_id:", 1)[1].strip()
                    return value or None
            return None

        if not isinstance(key_point, str):
            key_point = str(key_point)

        text = key_point.strip()
        if not text:
            return None

        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return SNSCollector._extract_feed_id_from_key_point(parsed)
            except Exception:
                pass

        import re
        match = re.search(r"feed_id:([A-Za-z0-9_-]+)", text)
        return match.group(1) if match else None

    async def _async_load_feed_id_cache(self) -> None:
        """异步加载 feed_id 缓存（按平台加载，避免全表扫描导致缓存缺失）"""
        global _feed_id_cache, _feed_id_cache_loaded
        if _feed_id_cache_loaded:
            return
        
        try:
            max_records = int(self.memory_cfg.get("max_records", 1000) or 1000)
            max_records = max(max_records, 0)

            platforms: List[str] = []
            for platform, cfg in (self.platform_cfg or {}).items():
                if isinstance(cfg, dict) and cfg.get("enabled", True):
                    platforms.append(platform)
            if not platforms:
                platforms = ["xiaohongshu"]

            for platform in platforms:
                try:
                    records = await database_api.db_get(
                        ChatHistory,
                        filters={"chat_id": f"sns_{platform}"},
                        order_by="-start_time",
                        limit=max_records + 300,
                    )
                except Exception as e:
                    logger.debug(f"[SNS] 加载 feed_id 缓存失败 platform={platform}: {e}")
                    continue

                for r in (records or []):
                    feed_id = SNSCollector._extract_feed_id_from_key_point(r.get("key_point", ""))
                    if feed_id:
                        _feed_id_cache.add(SNSCollector._make_cache_key(platform, feed_id))
            
            logger.info(f"[SNS] 加载 feed_id 缓存: {len(_feed_id_cache)} 条")
            _feed_id_cache_loaded = True
        except Exception as e:
            logger.warning(f"异步加载 feed_id 缓存失败: {e}")
    
    def _get_personality(self) -> Dict[str, str]:
        """获取 MaiBot 人格配置"""
        if self._personality_cache:
            return self._personality_cache
        
        try:
            from src.config.config import global_config
            # global_config.personality 是一个 PersonalityConfig 对象
            personality_cfg = global_config.personality
            bot_cfg = global_config.bot
            self._personality_cache = {
                "personality": getattr(personality_cfg, "personality", ""),
                "interest": getattr(personality_cfg, "interest", ""),
                "nickname": getattr(bot_cfg, "nickname", ""),
            }
        except Exception as e:
            logger.warning(f"获取人格配置失败: {e}")
            self._personality_cache = {"personality": "", "interest": "", "nickname": ""}
        
        return self._personality_cache
    
    async def collect(
        self, 
        platform: str = "xiaohongshu", 
        keyword: Optional[str] = None, 
        count: int = 10,
        preview_only: bool = False,  # 预览模式，不写入数据库
        provided_contents: Optional[List[SNSContent]] = None,  # 直接写入提供的内容（用于 preview->collect 确认写入）
    ) -> CollectResult:
        """执行采集任务
        
        Args:
            platform: 平台名称
            keyword: 搜索关键词
            count: 采集数量
            preview_only: 预览模式，只返回结果不写入
            provided_contents: 已完成筛选/详情的内容列表（跳过采集流程，直接进入写入阶段）
        """
        global _collector_stats
        
        result = CollectResult(success=False)

        platform = (platform or "").strip() or "xiaohongshu"
        keyword = keyword.strip() if isinstance(keyword, str) else keyword
        if keyword == "":
            keyword = None

        if provided_contents is not None:
            contents = [c for c in (provided_contents or []) if isinstance(c, SNSContent)]
            result.fetched = len(contents)
            if not contents:
                result.success = True
                return result
        else:
            count = max(int(count or 0), 0)
            if count == 0:
                result.errors.append("采集数量必须大于 0")
                return result

        platform_config = self.platform_cfg.get(platform, {})
        if platform_config and not platform_config.get("enabled", True):
            result.errors.append(f"平台未启用: {platform}")
            return result
        
        # 检查是否正在运行
        if _collector_stats["is_running"]:
            result.errors.append("采集任务正在运行中")
            return result
        
        _collector_stats["is_running"] = True
        
        if self.debug:
            logger.info("=" * 60)
            logger.info(f"[SNS] 🚀 开始采集流程")
            logger.info(f"[SNS]    平台: {platform}")
            logger.info(f"[SNS]    关键词: {keyword or '(推荐流)'}")
            logger.info(f"[SNS]    数量: {count}")
            logger.info(f"[SNS]    预览模式: {preview_only}")
            logger.info("=" * 60)
        
        try:
            # 加载 feed_id 缓存
            await self._async_load_feed_id_cache()
            if provided_contents is None:
                # 1. 获取内容
                if self.debug:
                    logger.info("[SNS] 📥 阶段1: 获取信息流...")

                contents = await self._fetch_contents(platform, keyword, count)
                result.fetched = len(contents)

                if self.debug:
                    logger.info(f"[SNS] ✓ 获取到 {len(contents)} 条内容")
                    for i, c in enumerate(contents):
                        logger.info(f"[SNS]    [{i+1}] {c.title[:50]}{'...' if len(c.title) > 50 else ''}")
                        logger.info(f"[SNS]        👍 {c.like_count} | 💬 {c.comment_count} | 📝 {len(c.content)}字 | @{c.author}")

                if not contents:
                    if self.debug:
                        logger.info("[SNS] ⚠️ 未获取到内容，结束")
                    result.success = True
                    return result

                # 2. 基础过滤（点赞数、黑白名单）
                if self.debug:
                    logger.info("-" * 60)
                    logger.info("[SNS] 🔍 阶段2: 基础过滤（点赞数/黑白名单）...")
                    logger.info(f"[SNS]    最小点赞数: {self.filter_cfg.get('min_like_count', 100)}")

                filtered = self._filter_contents(contents)
                result.filtered = result.fetched - len(filtered)

                if self.debug:
                    logger.info(f"[SNS] ✓ 基础过滤: {len(contents)} → {len(filtered)} 条（过滤 {result.filtered} 条）")

                # 3. 人格兴趣匹配（LLM 判断是否符合 MaiBot 兴趣）
                if self.debug:
                    logger.info("-" * 60)
                    logger.info("[SNS] 🧠 阶段3: 人格兴趣匹配...")
                    personality = self._get_personality()
                    logger.info(f"[SNS]    兴趣配置: {personality.get('interest', '(未配置)')[:80]}...")

                before_match = len(filtered)
                filtered = await self._match_personality_interest(filtered)
                result.filtered += before_match - len(filtered)

                if self.debug:
                    logger.info(f"[SNS] ✓ 人格匹配: {before_match} → {len(filtered)} 条")
                    if filtered:
                        logger.info("[SNS]    感兴趣的内容:")
                        for c in filtered:
                            logger.info(f"[SNS]      ✓ {c.title[:40]}...")

                if not filtered:
                    if self.debug:
                        logger.info("[SNS] ⚠️ 没有符合兴趣的内容，结束")
                    result.success = True
                    return result

                # 4. 获取详情（只对感兴趣的内容获取完整正文）
                fetch_detail = platform_config.get("fetch_detail", True)
                if fetch_detail:
                    if self.debug:
                        logger.info("-" * 60)
                        logger.info("[SNS] 📄 阶段4: 获取详情（正文+图片）...")
                    filtered = await self._fetch_details(filtered, platform)
            else:
                filtered = contents

            # 5. 写入记忆（或预览）
            if self.debug:
                logger.info("-" * 60)
                if preview_only:
                    logger.info("[SNS] 👁️ 阶段5: 预览模式（不写入）...")
                else:
                    logger.info("[SNS] 💾 阶段5: 写入记忆...")
            
            # 存储预览内容
            result.preview_contents = []  # type: ignore
            
            for content in filtered:
                try:
                    # 使用缓存检查重复
                    is_dup = self._check_duplicate_cached(content)
                    if is_dup:
                        if self.debug:
                            logger.info(f"[SNS]    ⏭️ 跳过重复: {content.title[:30]}...")
                        result.duplicate += 1
                        continue
                    
                    if preview_only:
                        # 预览模式：只收集内容，不写入
                        result.preview_contents.append({  # type: ignore
                            "feed_id": content.feed_id,
                            "title": content.title,
                            "content": content.content[:200],
                            "author": content.author,
                            "like_count": content.like_count,
                            "image_count": len(content.image_urls),
                        })
                        result.preview_items.append(content)
                        result.written += 1
                    else:
                        await self._write_to_memory(content, platform)
                        result.written += 1
                        # 添加到缓存
                        _feed_id_cache.add(self._make_cache_key(platform, content.feed_id))
                        # 记录最近写入的记忆
                        _collector_stats["recent_memories"].append({
                            "title": content.title[:50],
                            "author": content.author,
                            "time": time.time(),
                        })
                        # 只保留最近 20 条
                        _collector_stats["recent_memories"] = _collector_stats["recent_memories"][-20:]
                    
                    if self.debug:
                        logger.info(f"[SNS]    ✅ {'预览' if preview_only else '写入'}成功: {content.title[:30]}...")
                        logger.info(f"[SNS]       正文: {content.content[:80]}{'...' if len(content.content) > 80 else ''}")
                except Exception as e:
                    logger.error(f"[SNS]    ❌ 写入失败: {e}")
                    result.errors.append(f"写入失败: {e}")
            
            result.success = True

            # 自动清理（仅在写入模式执行）
            if not preview_only:
                auto_days = int(self.memory_cfg.get("auto_cleanup_days", 0) or 0)
                max_records = int(self.memory_cfg.get("max_records", 0) or 0)
                if auto_days > 0 or max_records > 0:
                    await self.cleanup(
                        days=auto_days if auto_days > 0 else 36500,
                        max_records=max_records if max_records > 0 else 1000,
                    )
            
            # 更新统计
            _collector_stats["last_collect_time"] = time.time()
            _collector_stats["total_collected"] += result.fetched
            _collector_stats["total_written"] += result.written
            _collector_stats["total_filtered"] += result.filtered
            _collector_stats["total_duplicate"] += result.duplicate
            _collector_stats["last_result"] = result.summary()
            
            if self.debug:
                logger.info("=" * 60)
                logger.info(f"[SNS] 🎉 采集完成!")
                logger.info(f"[SNS]    获取: {result.fetched} | 过滤: {result.filtered} | 重复: {result.duplicate} | 写入: {result.written}")
                logger.info("=" * 60)
            
        except Exception as e:
            logger.error(f"采集失败: {e}")
            result.errors.append(str(e))
        finally:
            _collector_stats["is_running"] = False
        
        return result
    
    def _check_duplicate_cached(self, content: SNSContent) -> bool:
        """使用缓存检查是否重复（快速）"""
        if not content.feed_id:
            return False
        return self._make_cache_key(content.platform, content.feed_id) in _feed_id_cache
    
    async def _fetch_contents(self, platform: str, keyword: Optional[str], count: int) -> List[SNSContent]:
        """通过MCP工具获取内容（使用平台适配器）"""
        contents = []
        
        # 获取平台适配器
        adapter = self._get_adapter(platform)
        
        # 获取MCP工具名前缀和工具名
        platform_config = self.platform_cfg.get(platform, {})
        mcp_prefix = platform_config.get("mcp_server_name", platform)
        
        # 获取工具名（支持自定义）
        tools_config = platform_config.get("tools", {})
        if keyword:
            tool_suffix = tools_config.get("search", adapter.default_tools.get("search", "search_feeds"))
        else:
            tool_suffix = tools_config.get("list", adapter.default_tools.get("list", "list_feeds"))
        
        tool_name = f"{mcp_prefix}_{tool_suffix}"
        
        if self.debug:
            logger.info(f"[SNS Debug] 调用工具: {tool_name}")
        
        tool = tool_api.get_tool_instance(tool_name)
        if not tool:
            logger.warning(f"MCP工具 {tool_name} 不存在，请检查MCP桥接插件配置")
            return contents
        
        try:
            if keyword:
                result = await tool.direct_execute(keyword=keyword)
            else:
                result = await tool.direct_execute()
        except Exception as e:
            logger.error(f"调用MCP工具 {tool_name} 失败: {e}")
            return contents
        
        # 解析结果
        content_str = result.get("content", "") if isinstance(result, dict) else str(result)
        
        if self.debug:
            # 打印原始返回内容（截取前2000字符）
            logger.info(f"[SNS Debug] MCP原始返回 (前2000字符):\n{content_str[:2000]}")
        
        # 检查是否是错误响应
        if content_str.startswith("❌") or content_str.startswith("⚠️") or content_str.startswith("⛔"):
            logger.warning(f"MCP工具返回错误: {content_str[:100]}")
            return contents
        
        # 使用适配器解析结果
        contents = adapter.parse_list_result(content_str)
        
        # 设置平台名
        for c in contents:
            c.platform = platform
        
        return contents[:count]
    
    async def _fetch_details(self, contents: List[SNSContent], platform: str) -> List[SNSContent]:
        """获取内容详情（补充正文）- 并发版本，使用适配器"""
        adapter = self._get_adapter(platform)
        platform_config = self.platform_cfg.get(platform, {})
        mcp_prefix = platform_config.get("mcp_server_name", platform)
        
        # 获取详情工具名
        tools_config = platform_config.get("tools", {})
        tool_suffix = tools_config.get("detail", adapter.default_tools.get("detail", "get_feed_detail"))
        tool_name = f"{mcp_prefix}_{tool_suffix}"
        
        tool = tool_api.get_tool_instance(tool_name)
        if not tool:
            if self.debug:
                logger.info(f"[SNS]    ⚠️ 详情工具 {tool_name} 不存在，跳过")
            return contents
        
        if self.debug:
            logger.info(f"[SNS]    使用工具: {tool_name}")
            logger.info(f"[SNS]    并发数: {self.MAX_CONCURRENT_DETAILS}")
        
        async def fetch_single_detail(idx: int, content: SNSContent) -> SNSContent:
            """获取单个内容的详情"""
            async with self._semaphore_details:
                try:
                    if self.debug:
                        logger.info(f"[SNS]    [{idx+1}/{len(contents)}] 获取: {content.title[:30]}...")
                    
                    # 构建参数（从 extra 中获取平台特定参数）
                    params = {"feed_id": content.feed_id}
                    if content.extra.get("xsec_token"):
                        params["xsec_token"] = content.extra["xsec_token"]
                    
                    result = await tool.direct_execute(**params)
                    
                    content_str = result.get("content", "") if isinstance(result, dict) else str(result)
                    
                    # 使用适配器解析详情
                    old_len = len(content.content)
                    content = adapter.parse_detail_result(content_str, content)
                    
                    if self.debug:
                        logger.info(f"[SNS]        ✓ 正文: {old_len} → {len(content.content)} 字")
                        logger.info(f"[SNS]        ✓ 图片: {len(content.image_urls)} 张")
                    
                    return content
                    
                except Exception as e:
                    if self.debug:
                        logger.warning(f"[SNS]        ❌ 获取失败: {e}")
                    return content  # 即使失败也保留原内容
        
        # 并发获取所有详情
        tasks = [fetch_single_detail(i, c) for i, c in enumerate(contents)]
        updated = await asyncio.gather(*tasks)
        
        return list(updated)
    
    def _filter_contents(self, contents: List[SNSContent]) -> List[SNSContent]:
        """过滤内容"""
        min_likes = self.filter_cfg.get("min_like_count", 100)
        blacklist = self.filter_cfg.get("keyword_blacklist", [])
        whitelist = self.filter_cfg.get("keyword_whitelist", [])
        
        if self.debug:
            logger.info(f"[SNS Debug] 过滤配置: min_likes={min_likes}, whitelist={whitelist}, blacklist={blacklist}")
        
        filtered = []
        for c in contents:
            text = f"{c.title} {c.content}"
            
            if self.debug:
                logger.info(f"[SNS Debug] 检查内容: title={c.title[:30]}..., likes={c.like_count}")
            
            # 白名单优先保留
            if whitelist and any(kw in text for kw in whitelist):
                if self.debug:
                    logger.info(f"[SNS Debug] ✓ 白名单命中，保留")
                filtered.append(c)
                continue
            
            # 点赞数过滤
            if c.like_count < min_likes:
                if self.debug:
                    logger.info(f"[SNS Debug] ✗ 点赞数不足: {c.like_count} < {min_likes}")
                continue
            
            # 黑名单过滤
            if any(kw in text for kw in blacklist):
                if self.debug:
                    logger.info(f"[SNS Debug] ✗ 黑名单命中")
                continue
            
            if self.debug:
                logger.info(f"[SNS Debug] ✓ 通过过滤")
            filtered.append(c)
        
        return filtered
    
    async def _match_personality_interest(self, contents: List[SNSContent]) -> List[SNSContent]:
        """使用 LLM 判断内容是否符合 MaiBot 人格兴趣"""
        if not contents:
            return []
        
        # 检查是否启用人格匹配
        if not self.processing_cfg.get("enable_personality_match", False):
            if self.debug:
                logger.info("[SNS Debug] 人格匹配未启用，跳过")
            return contents
        
        personality = self._get_personality()
        interest = personality.get("interest", "")
        
        if not interest:
            if self.debug:
                logger.info("[SNS Debug] 未配置兴趣，跳过人格匹配")
            return contents
        
        if self.debug:
            logger.info(f"[SNS Debug] 开始人格兴趣匹配，兴趣: {interest[:50]}...")
        
        # 构建内容列表供 LLM 判断
        content_list = []
        for i, c in enumerate(contents):
            content_list.append(f"{i+1}. 【{c.title}】{c.content[:100] if c.content else ''}")
        
        prompt = f"""你是一个内容筛选助手。根据以下人格兴趣描述，判断哪些内容值得深入了解。

人格兴趣：{interest}

待筛选内容：
{chr(10).join(content_list)}

请返回你认为符合该人格兴趣的内容编号，用逗号分隔。
只返回编号，例如：1,3,5
如果都不符合，返回：无"""

        try:
            models = llm_api.get_available_models()
            model_cfg = models.get("utils") or models.get("replyer")
            
            if not model_cfg:
                return contents
            
            success, response, _, _ = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_cfg,
                request_type="sns_personality_match",
            )
            
            if not success or not response:
                return contents
            
            response = response.strip()
            if self.debug:
                logger.info(f"[SNS Debug] LLM 兴趣匹配结果: {response}")
            
            if response == "无" or not response:
                return []
            
            # 解析编号
            matched_indices = set()
            for part in response.replace("，", ",").split(","):
                part = part.strip()
                if part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < len(contents):
                        matched_indices.add(idx)
            
            matched = [contents[i] for i in sorted(matched_indices)]
            
            if self.debug:
                logger.info(f"[SNS Debug] 人格匹配: {len(contents)} -> {len(matched)} 条")
                for c in matched:
                    logger.info(f"[SNS Debug]   ✓ {c.title[:40]}...")
            
            return matched
            
        except Exception as e:
            logger.warning(f"人格兴趣匹配失败: {e}")
            return contents
    
    async def _write_to_memory(self, content: SNSContent, platform: str) -> None:
        """写入ChatHistory"""
        # 生成摘要
        summary = await self._generate_summary(content)
        
        # 图片识图（如果启用）
        image_desc = ""
        enable_img_rec = self.processing_cfg.get("enable_image_recognition", False)
        if self.debug:
            logger.info(f"[SNS]    📝 写入记忆: {content.title[:30]}...")
            logger.info(f"[SNS]       图片数量: {len(content.image_urls)}")
            logger.info(f"[SNS]       识图开关: {enable_img_rec}")
        
        if content.image_urls and enable_img_rec:
            image_desc = await self._recognize_images(content.image_urls[:3])
            if self.debug:
                logger.info(f"[SNS]       识图结果: {image_desc[:80] if image_desc else '(无)'}")
        
        # 提取关键词
        keywords = await self._extract_keywords(content)
        
        # 构建记录
        chat_id = f"sns_{platform}"
        now = time.time()
        
        # 使用适配器获取 URL
        adapter = self._get_adapter(platform)
        url = adapter.get_content_url(content)
        full_summary = f"[来自{platform}] {summary}"
        if image_desc:
            full_summary += f"\n[图片内容] {image_desc}"
        full_summary += f"\n作者: @{content.author}\n原文: {url}"
        
        data = {
            "chat_id": chat_id,
            "start_time": now,
            "end_time": now,
            "original_text": content.content[:500],
            "participants": json.dumps([content.author]),
            "theme": content.title or summary[:50],
            "keywords": json.dumps(keywords),
            "summary": full_summary,
            "key_point": json.dumps([f"feed_id:{content.feed_id}", f"likes:{content.like_count}"]),
        }
        
        try:
            await database_api.db_query(ChatHistory, query_type="create", data=data)
            logger.info(f"写入SNS记忆: {content.title[:30]}...")
        except Exception as e:
            logger.error(f"写入失败，缓存到本地: {e}")
            self._cache_failed_write(data)
            raise
    
    async def _recognize_images(self, image_urls: List[str]) -> str:
        """识图（调用MaiBot的ImageManager）"""
        if self.debug:
            logger.info(f"[SNS] 🖼️ 开始识图，共 {len(image_urls)} 张图片")
        
        try:
            from src.chat.utils.utils_image import get_image_manager
            image_manager = get_image_manager()
            
            if self.debug:
                logger.info(f"[SNS]    ✓ ImageManager 加载成功")
            
            descriptions = []
            for i, url in enumerate(image_urls[:2]):  # 最多识别2张
                try:
                    if self.debug:
                        logger.info(f"[SNS]    [{i+1}] 下载图片: {url[:80]}...")
                    
                    # 下载图片并转换为 base64
                    image_base64 = await self._download_image_as_base64(url)
                    if not image_base64:
                        if self.debug:
                            logger.info(f"[SNS]        ⚠️ 图片下载失败")
                        continue
                    
                    if self.debug:
                        logger.info(f"[SNS]        ✓ 下载成功，开始识别...")
                    
                    desc = await asyncio.wait_for(
                        image_manager.get_image_description(image_base64),
                        timeout=self.processing_cfg.get("image_recognition_timeout", 30)
                    )
                    if desc:
                        descriptions.append(desc)
                        if self.debug:
                            logger.info(f"[SNS]        ✓ 识别结果: {desc[:100]}{'...' if len(desc) > 100 else ''}")
                    else:
                        if self.debug:
                            logger.info(f"[SNS]        ⚠️ 识别返回空结果")
                except asyncio.TimeoutError:
                    logger.warning(f"[SNS]    ❌ 识图超时: {url[:50]}...")
                except Exception as e:
                    logger.warning(f"[SNS]    ❌ 识图失败: {e}")
            
            result = "; ".join(descriptions) if descriptions else ""
            if self.debug:
                logger.info(f"[SNS]    识图完成: {len(descriptions)} 张成功")
            return result
        except ImportError as e:
            if self.debug:
                logger.warning(f"[SNS]    ❌ ImageManager 导入失败: {e}")
            return ""
    
    async def _download_image_as_base64(self, url: str) -> Optional[str]:
        """下载图片并转换为 base64"""
        import base64
        try:
            import aiohttp
            
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        image_bytes = await response.read()
                        return base64.b64encode(image_bytes).decode("utf-8")
                    else:
                        logger.warning(f"[SNS] 图片下载失败: HTTP {response.status}")
                        return None
        except Exception as e:
            logger.warning(f"[SNS] 图片下载异常: {e}")
            return None
    
    def _cache_failed_write(self, data: Dict) -> None:
        """缓存写入失败的数据"""
        try:
            _ensure_data_dir()
            cache = []
            if CACHE_FILE.exists():
                cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            cache.append({"data": data, "time": time.time()})
            CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"缓存失败: {e}")
    
    async def retry_cached_writes(self) -> int:
        """重试缓存的写入"""
        if not CACHE_FILE.exists():
            return 0
        
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            success = 0
            remaining = []
            
            for item in cache:
                try:
                    await database_api.db_query(ChatHistory, query_type="create", data=item["data"])
                    success += 1
                except Exception:
                    remaining.append(item)
            
            if remaining:
                CACHE_FILE.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                CACHE_FILE.unlink()
            
            logger.info(f"重试缓存写入: 成功{success}条, 剩余{len(remaining)}条")
            return success
        except Exception as e:
            logger.error(f"重试缓存失败: {e}")
            return 0
    
    async def _generate_summary(self, content: SNSContent) -> str:
        """生成摘要"""
        text = f"{content.title}\n{content.content}"
        
        threshold = int(self.processing_cfg.get("summary_threshold", 200) or 200)
        enable_summary = bool(self.processing_cfg.get("enable_summary", True))
        max_len = 200

        if len(text) <= threshold:
            return text

        if not enable_summary:
            return (text[:max_len] + "...") if len(text) > max_len else text
        
        # 使用LLM生成摘要
        try:
            models = llm_api.get_available_models()
            model_cfg = models.get("utils") or models.get("replyer")
            
            if model_cfg:
                prompt = (
                    "请用一两句话概括以下内容的核心信息，避免无关寒暄，不要超过 120 字：\n\n"
                    f"{text[:1500]}"
                )
                success, summary, _, _ = await llm_api.generate_with_model(
                    prompt=prompt,
                    model_config=model_cfg,
                    request_type="sns_summary",
                )
                if success and summary:
                    return summary.strip()[:200]
        except Exception as e:
            logger.warning(f"LLM摘要失败: {e}")
        
        # 降级：截断
        return (text[:max_len] + "...") if len(text) > max_len else text
    
    async def _extract_keywords(self, content: SNSContent) -> List[str]:
        """提取关键词"""
        keywords = []
        
        # 从标题提取（按空格和标点分词）
        if content.title:
            import re
            words = re.split(r'[\s,，。！？!?、]+', content.title)
            keywords.extend([w for w in words if len(w) >= 2][:3])
        
        # 添加作者名
        if content.author:
            keywords.append(content.author)
        
        # 添加平台标识
        keywords.append(content.platform)
        
        # 去重并限制数量
        seen = set()
        unique = []
        for kw in keywords:
            if kw and kw not in seen:
                seen.add(kw)
                unique.append(kw)
        
        return unique[:8]
    
    async def cleanup(self, days: int = 30, max_records: Optional[int] = None) -> Tuple[int, int]:
        """清理旧记忆（按平台分别清理，避免不同平台互相挤占配额）"""
        deleted = 0
        checked = 0

        max_records = int(
            max_records
            if max_records is not None
            else (self.memory_cfg.get("max_records", 1000) or 1000)
        )
        max_records = max(max_records, 0)

        platforms: List[str] = []
        for platform, cfg in (self.platform_cfg or {}).items():
            if isinstance(cfg, dict) and cfg.get("enabled", True):
                platforms.append(platform)
        if not platforms:
            platforms = ["xiaohongshu"]

        cutoff = time.time() - int(days) * 86400

        for platform in platforms:
            chat_id = f"sns_{platform}"
            try:
                records = await database_api.db_get(
                    ChatHistory,
                    filters={"chat_id": chat_id},
                    order_by="-start_time",
                    limit=max_records + 500,
                )
            except Exception as e:
                logger.warning(f"SNS记忆清理查询失败 platform={platform}: {e}")
                continue

            if not records:
                continue

            checked += len(records)

            ids_to_delete = set()

            # 按时间清理
            for r in records:
                if r.get("start_time", 0) < cutoff:
                    if r.get("id") is not None:
                        ids_to_delete.add(r["id"])

            # 按数量清理（每平台保留最新 max_records 条）
            if max_records > 0 and len(records) > max_records:
                for r in records[max_records:]:
                    if r.get("id") is not None:
                        ids_to_delete.add(r["id"])

            for record_id in ids_to_delete:
                await database_api.db_query(
                    ChatHistory,
                    query_type="delete",
                    filters={"id": record_id},
                )
                deleted += 1

        logger.info(f"SNS记忆清理: 检查{checked}条, 删除{deleted}条")
        return checked, deleted


# ============================================================================
# Dream工具 - 供做梦模块调用
# ============================================================================

class SNSCollectTool(BaseTool):
    """社交平台采集工具"""
    
    name = "collect_sns_content"
    description = "从社交平台（如小红书）采集热门内容并写入记忆。在做梦时可以调用此工具学习新知识。"
    parameters = [
        ("platform", ToolParamType.STRING, "平台名称，默认xiaohongshu", False, ["xiaohongshu"]),
        ("keyword", ToolParamType.STRING, "搜索关键词（可选）", False, None),
        ("count", ToolParamType.INTEGER, "采集数量，默认5", False, None),
    ]
    available_for_llm = False  # 不在回复时调用，只供做梦模块使用
    
    async def execute(self, function_args: dict) -> dict:
        platform = function_args.get("platform", "xiaohongshu")
        keyword = function_args.get("keyword")
        count = int(function_args.get("count", 5))
        
        collector = SNSCollector(_get_config())
        result = await collector.collect(platform, keyword, count)
        
        return {"name": self.name, "content": result.summary()}
    
    async def direct_execute(self, **kwargs) -> dict:
        return await self.execute(kwargs)


class SNSCleanupTool(BaseTool):
    """社交平台记忆清理工具"""
    
    name = "cleanup_sns_memory"
    description = "清理过期的社交平台记忆，保持记忆库整洁。"
    parameters = [
        ("days", ToolParamType.INTEGER, "清理多少天前的记忆，默认30", False, None),
    ]
    available_for_llm = False  # 不在回复时调用，只供做梦模块使用
    
    async def execute(self, function_args: dict) -> dict:
        days = int(function_args.get("days", 30))
        
        collector = SNSCollector(_get_config())
        checked, deleted = await collector.cleanup(days)
        
        return {"name": self.name, "content": f"检查{checked}条，删除{deleted}条过期记忆"}
    
    async def direct_execute(self, **kwargs) -> dict:
        return await self.execute(kwargs)


class SNSStatusTool(BaseTool):
    """SNS 状态查询工具（供 WebUI 调用）"""
    
    name = "sns_get_status"
    description = "获取 SNS 采集插件的运行状态和统计信息"
    parameters = [
        ("action", ToolParamType.STRING, "操作类型: stats/memories/trigger", False, ["stats", "memories", "trigger"]),
        ("keyword", ToolParamType.STRING, "触发采集时的搜索关键词（可选）", False, None),
    ]
    available_for_llm = False
    
    async def execute(self, function_args: dict) -> dict:
        action = function_args.get("action", "stats")
        config = _get_config()
        
        if action == "stats":
            # 返回统计信息
            stats = _collector_stats.copy()
            stats["feed_id_cache_size"] = len(_feed_id_cache)
            
            # 获取数据库中的记忆数量
            try:
                platform_cfg = config.get("platform", {}) if isinstance(config, dict) else {}
                platforms = [
                    p for p, cfg in platform_cfg.items()
                    if isinstance(cfg, dict) and cfg.get("enabled", True)
                ]
                if not platforms:
                    platforms = [p for p in platform_cfg.keys()] or ["xiaohongshu"]

                max_records = int(config.get("memory", {}).get("max_records", 1000) or 1000) if isinstance(config, dict) else 1000
                max_records = max(max_records, 0)

                by_platform: Dict[str, int] = {}
                total = 0
                for p in platforms:
                    records = await database_api.db_get(
                        ChatHistory,
                        filters={"chat_id": f"sns_{p}"},
                        limit=max_records + 500,
                    )
                    count = len(records or [])
                    by_platform[p] = count
                    total += count
                stats["total_memories"] = total
                stats["by_platform"] = by_platform
            except Exception:
                stats["total_memories"] = 0
                stats["by_platform"] = {}
            
            return {"name": self.name, "content": json.dumps(stats, ensure_ascii=False)}
        
        elif action == "memories":
            # 返回最近的记忆列表
            try:
                platform_cfg = config.get("platform", {}) if isinstance(config, dict) else {}
                platforms = [
                    p for p, cfg in platform_cfg.items()
                    if isinstance(cfg, dict) and cfg.get("enabled", True)
                ]
                if not platforms:
                    platforms = [p for p in platform_cfg.keys()] or ["xiaohongshu"]

                merged: List[Dict[str, Any]] = []
                for p in platforms:
                    records = await database_api.db_get(
                        ChatHistory,
                        filters={"chat_id": f"sns_{p}"},
                        order_by="-start_time",
                        limit=20,
                    )
                    for r in (records or []):
                        merged.append({
                            "id": r.get("id"),
                            "platform": p,
                            "theme": r.get("theme", ""),
                            "summary": (r.get("summary", "") or "")[:200],
                            "time": r.get("start_time", 0),
                        })

                merged.sort(key=lambda x: x.get("time", 0), reverse=True)
                return {"name": self.name, "content": json.dumps(merged[:20], ensure_ascii=False)}
            except Exception as e:
                return {"name": self.name, "content": json.dumps({"error": str(e)})}
        
        elif action == "trigger":
            # 触发采集
            keyword = function_args.get("keyword")
            collector = SNSCollector(_get_config())
            result = await collector.collect(keyword=keyword if keyword else None)
            return {"name": self.name, "content": result.summary()}
        
        return {"name": self.name, "content": "unknown action"}
    
    async def direct_execute(self, **kwargs) -> dict:
        return await self.execute(kwargs)


# ============================================================================
# 命令处理器
# ============================================================================

class SNSCommand(BaseCommand):
    """SNS命令"""
    
    command_name = "sns_command"
    command_description = "社交平台采集命令"
    command_pattern = r"^[/／]sns(?:\s+(?P<action>collect|search|status|cleanup|config|dream|preview|stats))?(?:\s+(?P<arg>.+))?$"
    
    async def execute(self) -> Tuple[bool, str, bool]:
        action = self.matched_groups.get("action", "collect")
        arg = self.matched_groups.get("arg", "")
        
        config = _get_config()
        collector = SNSCollector(config)
        stream_id = getattr(getattr(self.message, "chat_stream", None), "stream_id", "") or ""
        
        if action == "collect":
            keyword = arg.strip() if isinstance(arg, str) else ""

            if keyword:
                result = await collector.collect(keyword=keyword)
                await self.send_text(f"SNS采集完成\n{result.summary()}")
                return True, "命令执行完成", True

            # 无参数：优先作为 preview 的确认写入
            state = _load_state()
            preview = (state.get("preview") or {}).get(stream_id) if stream_id else None
            preview_ttl = 15 * 60  # 15 分钟内允许确认写入
            if isinstance(preview, dict) and preview.get("ts") and (time.time() - float(preview["ts"])) <= preview_ttl:
                items_data = preview.get("items") or []
                items: List[SNSContent] = []
                if isinstance(items_data, list):
                    for d in items_data:
                        if isinstance(d, dict):
                            try:
                                items.append(
                                    SNSContent(
                                        feed_id=str(d.get("feed_id", "")),
                                        platform=str(d.get("platform", "xiaohongshu")),
                                        title=str(d.get("title", "")),
                                        content=str(d.get("content", "")),
                                        author=str(d.get("author", "")),
                                        like_count=int(d.get("like_count", 0) or 0),
                                        comment_count=int(d.get("comment_count", 0) or 0),
                                        image_urls=list(d.get("image_urls") or []),
                                        url=str(d.get("url", "")),
                                        extra=dict(d.get("extra") or {}),
                                    )
                                )
                            except Exception:
                                continue

                if items:
                    result = await collector.collect(
                        platform=items[0].platform or "xiaohongshu",
                        provided_contents=items,
                        count=len(items),
                        preview_only=False,
                    )
                    # 确认后清空预览缓存
                    try:
                        if stream_id and isinstance(state.get("preview"), dict) and stream_id in state["preview"]:
                            del state["preview"][stream_id]
                            _save_state(state)
                    except Exception:
                        pass
                    await self.send_text(f"SNS写入（来自预览确认）完成\n{result.summary()}")
                    return True, "命令执行完成", True

            result = await collector.collect()
            await self.send_text(f"SNS采集完成\n{result.summary()}")
        
        elif action == "preview":
            # 预览模式：只获取内容，不写入数据库
            await self.send_text("👁️ 预览模式：获取内容中...")
            keyword = arg.strip() if isinstance(arg, str) else ""
            result = await collector.collect(keyword=keyword if keyword else None, preview_only=True)
            
            if hasattr(result, 'preview_contents') and result.preview_contents:  # type: ignore
                preview_text = f"📋 预览结果 ({len(result.preview_contents)} 条):\n\n"  # type: ignore
                for i, item in enumerate(result.preview_contents[:5]):  # type: ignore
                    preview_text += f"{i+1}. 【{item['title'][:30]}】\n"
                    preview_text += f"   👍 {item['like_count']} | @{item['author']} | 📷 {item['image_count']}张\n"
                    preview_text += f"   {item['content'][:50]}...\n\n"
                
                if len(result.preview_contents) > 5:  # type: ignore
                    preview_text += f"... 还有 {len(result.preview_contents) - 5} 条\n"  # type: ignore
                preview_text += "\n使用 /sns collect 确认写入"
                await self.send_text(preview_text)

                # 保存预览缓存，供 /sns collect 确认写入
                if stream_id and result.preview_items:
                    state = _load_state()
                    state.setdefault("preview", {})
                    state["preview"][stream_id] = {
                        "ts": time.time(),
                        "keyword": keyword,
                        "items": [asdict(c) for c in result.preview_items],
                    }
                    _save_state(state)
            else:
                await self.send_text(f"预览完成\n{result.summary()}\n（无符合条件的内容）")
        
        elif action == "stats":
            # 显示采集统计
            stats = _collector_stats
            last_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stats["last_collect_time"])) if stats["last_collect_time"] else "从未"
            
            stats_text = (
                f"📊 SNS 采集统计\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"上次采集: {last_time}\n"
                f"运行状态: {'🟢 运行中' if stats['is_running'] else '⚪ 空闲'}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"累计获取: {stats['total_collected']} 条\n"
                f"累计写入: {stats['total_written']} 条\n"
                f"累计过滤: {stats['total_filtered']} 条\n"
                f"累计重复: {stats['total_duplicate']} 条\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"缓存 feed_id: {len(_feed_id_cache)} 条\n"
            )
            
            if stats["recent_memories"]:
                stats_text += f"\n📝 最近写入:\n"
                for mem in stats["recent_memories"][-5:]:
                    mem_time = time.strftime("%H:%M", time.localtime(mem["time"]))
                    stats_text += f"  [{mem_time}] {mem['title'][:25]}...\n"
            
            await self.send_text(stats_text)
        
        elif action == "dream":
            # 模拟做梦式采集：带人格兴趣匹配的采集
            await self.send_text("🌙 开始做梦式采集（带人格兴趣匹配）...")
            
            # 强制开启人格匹配（避免修改原配置对象）
            import copy

            dream_config = copy.deepcopy(config)
            if "processing" not in dream_config:
                dream_config["processing"] = {}
            dream_config["processing"]["enable_personality_match"] = True
            
            dream_collector = SNSCollector(dream_config)
            result = await dream_collector.collect(count=15)  # 多获取一些，让 LLM 筛选
            
            await self.send_text(f"🌙 做梦采集完成\n{result.summary()}")
            
        elif action == "search":
            if not arg:
                await self.send_text("请提供搜索关键词，例如: /sns search 旅游攻略")
                return True, "缺少关键词", True
            result = await collector.collect(keyword=arg)
            await self.send_text(f"SNS搜索「{arg}」完成\n{result.summary()}")
            
        elif action == "cleanup":
            days = int(arg) if arg.isdigit() else 30
            checked, deleted = await collector.cleanup(days)
            await self.send_text(f"SNS清理完成: 检查{checked}条, 删除{deleted}条")
            
        elif action == "status":
            records = await database_api.db_get(ChatHistory, limit=1000)
            sns_records = [r for r in (records or []) if str(r.get("chat_id", "")).startswith("sns_")]
            
            # 按平台统计
            by_platform = {}
            for r in sns_records:
                p = r.get("chat_id", "").replace("sns_", "")
                by_platform[p] = by_platform.get(p, 0) + 1
            
            status = f"SNS记忆统计: 共{len(sns_records)}条\n"
            for p, c in by_platform.items():
                status += f"  - {p}: {c}条\n"
            await self.send_text(status)
            
        elif action == "config":
            cfg = config
            config_info = (
                f"SNS配置:\n"
                f"  最小点赞数: {cfg.get('filter', {}).get('min_like_count', 100)}\n"
                f"  最大记录数: {cfg.get('memory', {}).get('max_records', 1000)}\n"
                f"  自动清理: {cfg.get('memory', {}).get('auto_cleanup_days', 30)}天\n"
                f"  识图功能: {'开启' if cfg.get('processing', {}).get('enable_image_recognition') else '关闭'}"
            )
            await self.send_text(config_info)
            
        else:
            help_text = (
                "📱 SNS 采集命令\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "/sns collect     - 采集推荐内容\n"
                "/sns preview     - 预览内容（不写入）\n"
                "/sns search <词> - 搜索特定内容\n"
                "/sns dream       - 做梦式采集\n"
                "/sns stats       - 查看采集统计\n"
                "/sns status      - 查看记忆统计\n"
                "/sns cleanup [天] - 清理旧记忆\n"
                "/sns config      - 查看配置"
            )
            await self.send_text(help_text)
        
        return True, "命令执行完成", True


# ============================================================================
# 定时任务调度器
# ============================================================================

class SNSScheduler:
    """定时采集调度器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
    
    async def start(self) -> None:
        """启动调度器"""
        if self.running:
            return

        interval = float(self.config.get("scheduler", {}).get("interval_minutes", 60) or 0) * 60
        if interval <= 0:
            self.running = False
            logger.info("SNS定时任务已禁用")
            return

        self.running = True
        
        self._task = asyncio.create_task(self._run_loop(interval))
        logger.info(f"SNS定时任务启动，间隔{interval // 60}分钟")
    
    async def stop(self) -> None:
        """停止调度器"""
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("SNS定时任务已停止")
    
    async def _run_loop(self, interval: float) -> None:
        """定时执行循环"""
        # 首次延迟
        first_delay = self.config.get("scheduler", {}).get("first_delay_minutes", 5) * 60
        await asyncio.sleep(first_delay)
        
        while self.running:
            async with self._lock:  # 防止并发
                try:
                    collector = SNSCollector(self.config)
                    
                    # 重试缓存的写入
                    await collector.retry_cached_writes()
                    
                    # 执行采集
                    tasks = self.config.get("scheduler", {}).get("tasks", [])
                    if not tasks:
                        tasks = [{"platform": "xiaohongshu"}]
                    
                    for task in tasks:
                        if not task.get("enabled", True):
                            continue
                        result = await collector.collect(
                            platform=task.get("platform", "xiaohongshu"),
                            keyword=task.get("keyword"),
                            count=task.get("count", 10),
                        )
                        logger.info(f"定时采集完成: {result.summary()}")
                    
                except Exception as e:
                    logger.error(f"定时采集失败: {e}")
            
            await asyncio.sleep(interval)


# 全局实例
_scheduler: Optional[SNSScheduler] = None
_plugin_instance: Optional["MaiBotSNSPlugin"] = None


def _get_config() -> Dict[str, Any]:
    """获取插件配置"""
    global _plugin_instance
    if _plugin_instance and hasattr(_plugin_instance, "config"):
        return _normalize_config(_plugin_instance.config)
    return {}


def _register_dream_tools() -> None:
    """注册 SNS 工具到做梦模块"""
    try:
        from src.dream.dream_agent import get_dream_tool_registry, DreamTool
        from src.llm_models.payload_content.tool_option import ToolParamType
        
        registry = get_dream_tool_registry()
        
        # 创建 SNS 采集工具的执行函数
        async def collect_sns_content(platform: str = "xiaohongshu", keyword: str = "", count: int = 10) -> str:
            """执行 SNS 采集"""
            import copy

            config = copy.deepcopy(_get_config())
            if "processing" not in config:
                config["processing"] = {}
            config["processing"]["enable_personality_match"] = True
            
            collector = SNSCollector(config)
            result = await collector.collect(
                platform=platform,
                keyword=keyword if keyword else None,
                count=int(count)
            )
            return f"SNS采集完成: {result.summary()}"
        
        # 注册采集工具
        registry.register_tool(
            DreamTool(
                name="collect_sns_content",
                description="从社交平台（如小红书）采集内容并写入记忆。会根据你的兴趣自动筛选内容，只保留感兴趣的信息。适合在做梦时学习新知识、了解热门话题。",
                parameters=[
                    ("platform", ToolParamType.STRING, "平台名称，目前支持 xiaohongshu（小红书）", False, None),
                    ("keyword", ToolParamType.STRING, "搜索关键词（可选），留空则获取推荐内容", False, None),
                    ("count", ToolParamType.INTEGER, "获取数量，默认10，建议5-20", False, None),
                ],
                execute_func=collect_sns_content,
            )
        )
        
        logger.info("[SNS] ✓ 已注册 Dream 工具: collect_sns_content")
        
    except ImportError as e:
        logger.debug(f"[SNS] 做梦模块未加载，跳过 Dream 工具注册: {e}")
    except Exception as e:
        logger.warning(f"[SNS] 注册 Dream 工具失败: {e}")


def _register_memory_retrieval_tools() -> None:
    """注册 SNS 记忆搜索工具到记忆检索系统"""
    logger.info("[SNS] 开始注册记忆检索工具...")
    try:
        from src.memory_system.retrieval_tools import register_memory_retrieval_tool
        logger.info("[SNS] 成功导入 register_memory_retrieval_tool")
        
        async def search_sns_memory(chat_id: str, keyword: Optional[str] = None) -> str:
            """搜索 SNS 记忆（社交平台采集的内容）"""
            if not keyword:
                return "请提供搜索关键词"
            
            try:
                config = _get_config()
                platform_cfg = config.get("platform", {}) if isinstance(config, dict) else {}
                platforms = [
                    p for p, cfg in platform_cfg.items()
                    if isinstance(cfg, dict) and cfg.get("enabled", True)
                ]
                if not platforms:
                    platforms = [p for p in platform_cfg.keys()] or ["xiaohongshu"]

                # 关键词解析：复用 MaiBot 的统一规则（含空格/逗号/斜杠等）
                try:
                    from src.chat.utils.utils import parse_keywords_string

                    keywords = parse_keywords_string(keyword) or []
                except Exception:
                    keywords = []

                if not keywords:
                    keywords = [kw for kw in (keyword or "").split() if kw.strip()]
                keywords = [kw.strip() for kw in keywords if kw and kw.strip()]
                if not keywords:
                    return "请提供有效的搜索关键词"

                # Peewee 直接查询（避免全量拉取到内存）
                chat_ids = [f"sns_{p}" for p in platforms]
                query = ChatHistory.select(
                    ChatHistory.id,
                    ChatHistory.chat_id,
                    ChatHistory.theme,
                    ChatHistory.keywords,
                    ChatHistory.summary,
                    ChatHistory.start_time,
                ).where(ChatHistory.chat_id.in_(chat_ids))

                kw_cond = None
                for kw in keywords:
                    c = (
                        (ChatHistory.theme.contains(kw))
                        | (ChatHistory.summary.contains(kw))
                        | (ChatHistory.keywords.contains(kw))
                        | (ChatHistory.original_text.contains(kw))
                    )
                    kw_cond = c if kw_cond is None else (kw_cond | c)

                if kw_cond is not None:
                    query = query.where(kw_cond)

                records = list(query.order_by(ChatHistory.start_time.desc()).limit(50))
                if not records:
                    return f"未找到包含关键词「{keyword}」的 SNS 记忆"

                results = []
                for r in records[:10]:
                    platform = (getattr(r, "chat_id", "") or "").replace("sns_", "")
                    results.append(
                        f"记忆ID：{getattr(r, 'id', None)}\n"
                        f"来源：{platform}\n"
                        f"主题：{getattr(r, 'theme', '(无)') or '(无)'}\n"
                        f"关键词：{getattr(r, 'keywords', '(无)') or '(无)'}"
                    )

                return f"找到 {len(records)} 条 SNS 记忆（显示前{len(results)}条）：\n\n" + "\n\n---\n\n".join(results)
                
            except Exception as e:
                logger.error(f"搜索 SNS 记忆失败: {e}")
                return f"搜索失败: {e}"
        
        async def get_sns_memory_detail(chat_id: str, memory_ids: str) -> str:
            """获取 SNS 记忆详情"""
            try:
                # 解析 ID 列表
                id_list = [int(id_str.strip()) for id_str in memory_ids.split(",") if id_str.strip().isdigit()]
                if not id_list:
                    return "请提供有效的记忆ID"

                # 只允许读取 sns_* 记录，避免通过 ID 读取非 SNS 记忆
                query = (
                    ChatHistory.select(
                        ChatHistory.id,
                        ChatHistory.chat_id,
                        ChatHistory.theme,
                        ChatHistory.summary,
                        ChatHistory.keywords,
                        ChatHistory.start_time,
                    )
                    .where(ChatHistory.id.in_(id_list))
                    .where(ChatHistory.chat_id.startswith("sns_"))
                )
                matched = list(query.limit(len(id_list)))
                
                if not matched:
                    return f"未找到ID为 {id_list} 的记忆"
                
                # 构建详情
                results = []
                for r in matched:
                    parts = [
                        f"记忆ID：{getattr(r, 'id', None)}",
                        f"来源：{(getattr(r, 'chat_id', '') or '').replace('sns_', '')}",
                        f"主题：{getattr(r, 'theme', '(无)') or '(无)'}",
                    ]
                    if getattr(r, "summary", None):
                        parts.append(f"概括：{getattr(r, 'summary')}")
                    if getattr(r, "keywords", None):
                        parts.append(f"关键词：{getattr(r, 'keywords')}")
                    results.append("\n".join(parts))
                
                return "\n\n" + ("=" * 50) + "\n\n" + "\n\n".join(results)
                
            except Exception as e:
                logger.error(f"获取 SNS 记忆详情失败: {e}")
                return f"获取失败: {e}"
        
        # 注册搜索工具
        register_memory_retrieval_tool(
            name="search_sns_memory",
            description="搜索从小红书等社交平台采集的外部知识记忆。适用于查找：产品资讯（如XREAL、手机、相机）、科技数码、AI工具、游戏动漫、热门话题等。当search_chat_history找不到信息时，可以尝试此工具搜索外部来源的知识。",
            parameters=[
                {"name": "keyword", "type": "string", "description": "搜索关键词（如：XREAL、Tanvas、AI、手机、游戏等）", "required": True},
            ],
            execute_func=search_sns_memory,
        )
        
        # 注册详情工具
        register_memory_retrieval_tool(
            name="get_sns_memory_detail",
            description="获取社交平台记忆的详细内容。需要先使用 search_sns_memory 获取记忆ID。",
            parameters=[
                {"name": "memory_ids", "type": "string", "description": "记忆ID，可以是单个ID或多个ID（逗号分隔）", "required": True},
            ],
            execute_func=get_sns_memory_detail,
        )
        
        logger.info("[SNS] ✓ 已注册记忆检索工具: search_sns_memory, get_sns_memory_detail")
        
    except ImportError as e:
        logger.debug(f"[SNS] 记忆检索模块未加载，跳过工具注册: {e}")
    except Exception as e:
        logger.warning(f"[SNS] 注册记忆检索工具失败: {e}")


# ============================================================================
# 事件处理器
# ============================================================================

class SNSStartupHandler(BaseEventHandler):
    """插件启动事件处理"""
    
    event_type = EventType.ON_START
    handler_name = "sns_startup_handler"
    handler_description = "SNS插件启动处理器"
    weight = 0
    intercept_message = False
    
    async def execute(self, message: Optional[Any]) -> Tuple[bool, bool, Optional[str], None, None]:
        global _scheduler
        
        logger.info("MaiBot_SNS 插件启动")
        
        config = _get_config()
        
        # 注册 Dream 工具（如果启用）
        if config.get("dream", {}).get("enabled", True):
            _register_dream_tools()
        
        # 注册记忆检索工具（让 MaiBot 回忆时能搜索 SNS 记忆）
        _register_memory_retrieval_tools()
        
        # 启动定时调度器
        if config.get("scheduler", {}).get("enabled", False):
            _scheduler = SNSScheduler(config)
            await _scheduler.start()
        
        return (True, True, None, None, None)


class SNSShutdownHandler(BaseEventHandler):
    """插件停止事件处理"""
    
    event_type = EventType.ON_STOP
    handler_name = "sns_shutdown_handler"
    handler_description = "SNS插件停止处理器"
    weight = 0
    intercept_message = False
    
    async def execute(self, message: Optional[Any]) -> Tuple[bool, bool, Optional[str], None, None]:
        global _scheduler
        
        logger.info("MaiBot_SNS 插件停止")
        
        if _scheduler:
            await _scheduler.stop()
            _scheduler = None
        
        return (True, True, None, None, None)


# ============================================================================
# 插件主类
# ============================================================================

@register_plugin
class MaiBotSNSPlugin(BasePlugin):
    """MaiBot SNS插件"""
    
    plugin_name = "maibot_sns"
    enable_plugin = True
    dependencies = ["mcp_bridge_plugin"]
    python_dependencies = [
        PythonDependency(
            package_name="aiohttp",
            version="",
            optional=True,
            description="用于下载图片并转为 base64（图片识别功能需要）",
        ),
    ]
    config_file_name = "config.toml"
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        global _plugin_instance
        _plugin_instance = self
    
    # Section 描述（用于 WebUI 显示）
    config_section_descriptions = {
        "plugin": ConfigSection(
            title="插件设置",
            description="基础插件配置",
            icon="settings",
            order=0,
        ),
        "platform": ConfigSection(
            title="平台配置",
            description="配置要采集的社交平台",
            icon="globe",
            order=1,
        ),
        "filter": ConfigSection(
            title="内容过滤",
            description="设置过滤规则，只保存有价值的内容",
            icon="filter",
            order=2,
        ),
        "processing": ConfigSection(
            title="内容处理",
            description="LLM 摘要、图片识别等处理选项",
            icon="cpu",
            order=3,
        ),
        "memory": ConfigSection(
            title="记忆存储",
            description="记忆数量和清理设置",
            icon="database",
            order=4,
        ),
        "scheduler": ConfigSection(
            title="定时任务",
            description="自动采集任务配置",
            icon="clock",
            order=5,
            collapsed=True,
        ),
        "dream": ConfigSection(
            title="做梦集成",
            description="做梦模块集成配置",
            icon="moon",
            order=6,
            collapsed=True,
        ),
        "debug": ConfigSection(
            title="调试",
            description="调试日志配置",
            icon="bug",
            order=99,
            collapsed=True,
        ),
    }
    
    config_schema = {
        "plugin": {
            "name": ConfigField(
                type=str, default="maibot_sns",
                description="插件名称",
                hidden=True,
            ),
            "version": ConfigField(
                type=str, default="1.0.0",
                description="版本",
                hidden=True,
            ),
            "enabled": ConfigField(
                type=bool, default=True,
                description="启用插件",
                label="启用 SNS 采集插件",
                order=0,
            ),
        },
        "platform": {
            "xiaohongshu.enabled": ConfigField(
                type=bool, default=True,
                description="启用小红书采集",
                label="启用小红书",
                hint="需要先配置 MCP 桥接插件中的小红书 MCP 服务器",
                order=0,
            ),
            "xiaohongshu.mcp_server_name": ConfigField(
                type=str, default="mcp_xiaohongshu",
                description="MCP 服务器名称",
                label="MCP 服务器名",
                hint="与 MCP 桥接插件配置中的 name 对应",
                placeholder="mcp_xiaohongshu",
                order=1,
            ),
            "xiaohongshu.fetch_detail": ConfigField(
                type=bool, default=True,
                description="获取笔记详情",
                label="获取完整正文",
                hint="开启后会调用 get_feed_detail 获取完整正文和图片",
                order=2,
            ),
        },
        "filter": {
            "min_like_count": ConfigField(
                type=int, default=100,
                description="最小点赞数",
                label="最小点赞数",
                hint="低于此值的内容会被过滤，设为 0 则不过滤",
                min=0, max=100000, step=10,
                order=0,
            ),
            "keyword_whitelist": ConfigField(
                type=list, default=[],
                description="关键词白名单",
                label="白名单关键词",
                hint="包含这些关键词的内容会优先保留，即使点赞数不够",
                placeholder="教程, 攻略, 科普",
                order=1,
            ),
            "keyword_blacklist": ConfigField(
                type=list, default=[],
                description="关键词黑名单",
                label="黑名单关键词",
                hint="包含这些关键词的内容会被直接过滤",
                placeholder="广告, 推广, 代购",
                order=2,
            ),
        },
        "processing": {
            "enable_personality_match": ConfigField(
                type=bool, default=True,
                description="启用人格兴趣匹配",
                label="人格兴趣匹配",
                hint="使用 LLM 判断内容是否符合 MaiBot 的兴趣，只学习感兴趣的内容",
                order=0,
            ),
            "enable_summary": ConfigField(
                type=bool, default=True,
                description="启用 LLM 摘要",
                label="LLM 摘要生成",
                hint="对长文本生成摘要",
                order=1,
            ),
            "summary_threshold": ConfigField(
                type=int, default=200,
                description="摘要触发长度",
                label="摘要触发长度（字符）",
                hint="超过此长度的内容才会生成摘要",
                min=50, max=2000, step=50,
                depends_on="processing.enable_summary",
                depends_value=True,
                order=2,
            ),
            "enable_image_recognition": ConfigField(
                type=bool, default=False,
                description="启用图片识别",
                label="图片识别（VLM）",
                hint="使用视觉模型理解图片内容，会增加处理时间和 API 调用",
                order=3,
            ),
            "image_recognition_timeout": ConfigField(
                type=int, default=30,
                description="识图超时时间",
                label="识图超时（秒）",
                min=10, max=120, step=5,
                depends_on="processing.enable_image_recognition",
                depends_value=True,
                order=4,
            ),
        },
        "memory": {
            "max_records": ConfigField(
                type=int, default=1000,
                description="最大记录数",
                label="每平台最大记录数",
                hint="超过此数量会自动删除最旧的记录",
                min=100, max=10000, step=100,
                order=0,
            ),
            "auto_cleanup_days": ConfigField(
                type=int, default=30,
                description="自动清理天数",
                label="记忆保留天数",
                hint="超过此天数的记录会被自动清理",
                min=7, max=365, step=1,
                order=1,
            ),
        },
        "scheduler": {
            "enabled": ConfigField(
                type=bool, default=False,
                description="启用定时采集",
                label="启用定时采集",
                hint="建议先手动测试成功后再开启",
                order=0,
            ),
            "interval_minutes": ConfigField(
                type=int, default=60,
                description="采集间隔",
                label="采集间隔（分钟）",
                hint="建议不要设置太短，避免频繁请求",
                min=10, max=1440, step=10,
                depends_on="scheduler.enabled",
                depends_value=True,
                order=1,
            ),
            "first_delay_minutes": ConfigField(
                type=int, default=5,
                description="首次延迟",
                label="首次采集延迟（分钟）",
                hint="插件启动后等待多久开始第一次采集",
                min=1, max=60, step=1,
                depends_on="scheduler.enabled",
                depends_value=True,
                order=2,
            ),
        },
        "dream": {
            "enabled": ConfigField(
                type=bool, default=True,
                description="启用做梦模块集成",
                label="做梦模块集成",
                hint="开启后做梦 agent 可以调用 SNS 采集工具主动学习",
                order=0,
            ),
        },
        "debug": {
            "enabled": ConfigField(
                type=bool, default=False,
                description="启用调试模式",
                label="调试日志",
                hint="开启后会输出详细的采集过程日志",
                order=0,
            ),
        },
    }
    
    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """注册组件"""
        return [
            (SNSCollectTool.get_tool_info(), SNSCollectTool),
            (SNSCleanupTool.get_tool_info(), SNSCleanupTool),
            (SNSStatusTool.get_tool_info(), SNSStatusTool),
            (SNSCommand.get_command_info(), SNSCommand),
            (SNSStartupHandler.get_handler_info(), SNSStartupHandler),
            (SNSShutdownHandler.get_handler_info(), SNSShutdownHandler),
        ]
