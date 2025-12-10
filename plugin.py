"""
MaiBot_SNS - 社交平台信息采集与记忆写入插件

通过MCP桥接调用社交平台API（如小红书），采集内容并写入ChatHistory记忆系统。
支持做梦模块集成、定时任务和手动命令触发。
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
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
from src.plugin_system.base.base_events_handler import BaseEventHandler
from src.plugin_system.base.component_types import EventType
from src.plugin_system.apis import tool_api, llm_api, database_api
from src.common.database.database_model import ChatHistory

logger = get_logger("maibot_sns")

# 缓存文件路径
CACHE_FILE = Path(__file__).parent / "failed_writes.json"


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class SNSContent:
    """社交平台内容"""
    feed_id: str
    platform: str
    title: str
    content: str
    author: str
    like_count: int = 0
    comment_count: int = 0
    image_urls: List[str] = field(default_factory=list)
    xsec_token: str = ""


@dataclass
class CollectResult:
    """采集结果"""
    success: bool
    fetched: int = 0
    written: int = 0
    filtered: int = 0
    duplicate: int = 0
    errors: List[str] = field(default_factory=list)
    
    def summary(self) -> str:
        status = "✅" if self.success else "❌"
        return f"{status} 获取:{self.fetched} 写入:{self.written} 过滤:{self.filtered} 重复:{self.duplicate}"


# ============================================================================
# 核心功能
# ============================================================================

class SNSCollector:
    """SNS内容采集器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.platform = config.get("platform", {})
        self.filter_cfg = config.get("filter", {})
        self.memory_cfg = config.get("memory", {})
        self.debug = config.get("debug", {}).get("enabled", False)
        self.processing_cfg = config.get("processing", {})
        self._personality_cache: Optional[Dict[str, str]] = None
    
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
    
    async def collect(self, platform: str = "xiaohongshu", keyword: Optional[str] = None, count: int = 10) -> CollectResult:
        """执行采集任务"""
        result = CollectResult(success=False)
        
        if self.debug:
            logger.info("=" * 60)
            logger.info(f"[SNS] 🚀 开始采集流程")
            logger.info(f"[SNS]    平台: {platform}")
            logger.info(f"[SNS]    关键词: {keyword or '(推荐流)'}")
            logger.info(f"[SNS]    数量: {count}")
            logger.info("=" * 60)
        
        try:
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
            fetch_detail = self.platform.get(platform, {}).get("fetch_detail", True)
            if fetch_detail:
                if self.debug:
                    logger.info("-" * 60)
                    logger.info("[SNS] 📄 阶段4: 获取详情（正文+图片）...")
                filtered = await self._fetch_details(filtered, platform)
            
            # 5. 写入记忆
            if self.debug:
                logger.info("-" * 60)
                logger.info("[SNS] 💾 阶段5: 写入记忆...")
            
            for content in filtered:
                try:
                    is_dup = await self._check_duplicate(content)
                    if is_dup:
                        if self.debug:
                            logger.info(f"[SNS]    ⏭️ 跳过重复: {content.title[:30]}...")
                        result.duplicate += 1
                        continue
                    
                    await self._write_to_memory(content, platform)
                    result.written += 1
                    if self.debug:
                        logger.info(f"[SNS]    ✅ 写入成功: {content.title[:30]}...")
                        logger.info(f"[SNS]       正文: {content.content[:80]}{'...' if len(content.content) > 80 else ''}")
                except Exception as e:
                    logger.error(f"[SNS]    ❌ 写入失败: {e}")
                    result.errors.append(f"写入失败: {e}")
            
            result.success = True
            
            if self.debug:
                logger.info("=" * 60)
                logger.info(f"[SNS] 🎉 采集完成!")
                logger.info(f"[SNS]    获取: {result.fetched} | 过滤: {result.filtered} | 重复: {result.duplicate} | 写入: {result.written}")
                logger.info("=" * 60)
            
        except Exception as e:
            logger.error(f"采集失败: {e}")
            result.errors.append(str(e))
        
        return result
    
    async def _fetch_contents(self, platform: str, keyword: Optional[str], count: int) -> List[SNSContent]:
        """通过MCP工具获取内容"""
        contents = []
        result = None
        
        # 获取MCP工具名前缀
        mcp_prefix = self.platform.get(platform, {}).get("mcp_server_name", platform)
        
        # 调用MCP工具
        if keyword:
            tool_name = f"{mcp_prefix}_search_feeds"
        else:
            tool_name = f"{mcp_prefix}_list_feeds"
        
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
        
        contents = self._parse_mcp_result(content_str, platform)
        
        return contents[:count]
    
    def _parse_mcp_result(self, result: str, platform: str) -> List[SNSContent]:
        """解析MCP返回结果"""
        contents = []
        
        if not result or not result.strip():
            return contents
        
        try:
            data = json.loads(result)
            
            # 支持多种返回格式
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items", data.get("feeds", data.get("notes", data.get("data", []))))
                if not isinstance(items, list):
                    items = [data]  # 单条数据
            else:
                return contents
            
            for item in items:
                if not isinstance(item, dict):
                    continue
                
                # 小红书的数据结构: item.noteCard 包含详细信息
                note_card = item.get("noteCard", {})
                user_info = note_card.get("user", {})
                interact_info = note_card.get("interactInfo", {})
                cover_info = note_card.get("cover", {})
                
                # 提取feed_id
                feed_id = str(item.get("id", item.get("note_id", "")))
                if not feed_id:
                    continue
                
                # 提取点赞数（从 interactInfo.likedCount）
                like_count = interact_info.get("likedCount", item.get("likedCount", 0))
                if isinstance(like_count, str):
                    # 处理可能的小数格式如 "1.60000"
                    like_count = int(float(like_count.replace(",", "").replace("万", "0000") or 0))
                
                # 提取评论数
                comment_count = interact_info.get("commentCount", item.get("commentCount", 0))
                if isinstance(comment_count, str):
                    comment_count = int(float(comment_count.replace(",", "") or 0))
                
                # 提取标题（从 noteCard.displayTitle）
                title = note_card.get("displayTitle", item.get("title", ""))
                
                # 提取作者（从 noteCard.user）
                author = user_info.get("nickname", user_info.get("nickName", item.get("nickname", "")))
                
                # 提取封面图片
                images = []
                if cover_info.get("urlDefault"):
                    images.append(cover_info["urlDefault"])
                
                contents.append(SNSContent(
                    feed_id=feed_id,
                    platform=platform,
                    title=title,
                    content=note_card.get("desc", item.get("desc", "")),
                    author=author,
                    like_count=int(like_count),
                    comment_count=int(comment_count),
                    image_urls=images,
                    xsec_token=item.get("xsecToken", ""),
                ))
                
        except json.JSONDecodeError:
            logger.debug(f"非JSON格式结果，长度={len(result)}")
        except Exception as e:
            logger.warning(f"解析MCP结果失败: {e}")
        
        return contents
    
    async def _fetch_details(self, contents: List[SNSContent], platform: str) -> List[SNSContent]:
        """获取内容详情（补充正文）"""
        mcp_prefix = self.platform.get(platform, {}).get("mcp_server_name", platform)
        tool_name = f"{mcp_prefix}_get_feed_detail"
        
        tool = tool_api.get_tool_instance(tool_name)
        if not tool:
            if self.debug:
                logger.info(f"[SNS]    ⚠️ 详情工具 {tool_name} 不存在，跳过")
            return contents
        
        if self.debug:
            logger.info(f"[SNS]    使用工具: {tool_name}")
        
        updated = []
        for i, content in enumerate(contents):
            try:
                if self.debug:
                    logger.info(f"[SNS]    [{i+1}/{len(contents)}] 获取: {content.title[:30]}...")
                
                result = await tool.direct_execute(
                    feed_id=content.feed_id,
                    xsec_token=content.xsec_token
                )
                
                content_str = result.get("content", "") if isinstance(result, dict) else str(result)
                
                # 解析详情
                detail = self._parse_feed_detail(content_str)
                if detail:
                    old_len = len(content.content)
                    # 更新正文内容
                    if detail.get("desc"):
                        content.content = detail["desc"]
                    if detail.get("images"):
                        content.image_urls = detail["images"]
                    
                    if self.debug:
                        logger.info(f"[SNS]        ✓ 正文: {old_len} → {len(content.content)} 字")
                        logger.info(f"[SNS]        ✓ 图片: {len(content.image_urls)} 张")
                else:
                    if self.debug:
                        logger.info(f"[SNS]        ⚠️ 详情解析失败，保留原内容")
                
                updated.append(content)
                
            except Exception as e:
                if self.debug:
                    logger.warning(f"[SNS]        ❌ 获取失败: {e}")
                updated.append(content)  # 即使失败也保留原内容
        
        return updated
    
    def _parse_feed_detail(self, result: str) -> Optional[Dict]:
        """解析详情返回"""
        try:
            data = json.loads(result)
            
            # 小红书详情结构: { feed_id, data: { note: {...}, comments: [...] } }
            # 需要从 data.data.note 中获取内容
            note = None
            
            # 尝试多种可能的数据路径
            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], dict):
                    # 结构: { data: { note: {...} } }
                    note = data["data"].get("note", {})
                elif "note" in data:
                    # 结构: { note: {...} }
                    note = data["note"]
                elif "noteCard" in data:
                    # 结构: { noteCard: {...} }
                    note = data["noteCard"]
                else:
                    # 直接使用顶层
                    note = data
            
            if not note:
                if self.debug:
                    logger.warning(f"[SNS Debug] 无法找到 note 数据")
                return None
            
            if self.debug:
                logger.info(f"[SNS Debug] note keys: {list(note.keys())[:10]}")
            
            # 获取正文 - 尝试多种字段名
            desc = ""
            for field in ["desc", "description", "content", "text", "noteDesc"]:
                if note.get(field):
                    desc = note[field]
                    if self.debug:
                        logger.info(f"[SNS Debug] 找到正文字段: {field}, 长度: {len(desc)}")
                    break
            
            # 获取图片列表
            images = []
            image_list = note.get("imageList") or note.get("images") or note.get("image_list") or []
            
            if self.debug and image_list:
                logger.info(f"[SNS Debug] 图片列表: {len(image_list)} 张")
                if image_list and isinstance(image_list[0], dict):
                    logger.info(f"[SNS Debug] 图片项 keys: {list(image_list[0].keys())[:5]}")
            
            for img in image_list:
                if isinstance(img, dict):
                    # 尝试多种 URL 字段（优先使用 urlDefault）
                    url = ""
                    for url_field in ["urlDefault", "url_default", "url", "originUrl", "original_url", "urlPre"]:
                        if img.get(url_field):
                            url = img[url_field]
                            break
                    # 尝试从 infoList 获取
                    if not url and img.get("infoList"):
                        info_list = img["infoList"]
                        if info_list and isinstance(info_list[0], dict):
                            url = info_list[0].get("url", "")
                    if url:
                        images.append(url)
                elif isinstance(img, str):
                    images.append(img)
            
            if self.debug:
                logger.info(f"[SNS Debug] 解析结果: desc长度={len(desc)}, images数量={len(images)}")
            
            return {
                "desc": desc,
                "images": images,
            }
        except json.JSONDecodeError as e:
            if self.debug:
                logger.warning(f"[SNS Debug] JSON解析失败: {e}, 原始内容前200字: {result[:200]}")
            return None
        except Exception as e:
            if self.debug:
                logger.warning(f"[SNS Debug] 详情解析异常: {e}")
            return None
    
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
    
    async def _check_duplicate(self, content: SNSContent) -> bool:
        """检查是否重复"""
        if not content.feed_id:
            return False
        
        # 通过feed_id检查（在key_point中存储了feed_id）
        try:
            records = await database_api.db_get(
                ChatHistory,
                filters={"chat_id": f"sns_{content.platform}"},
                limit=200,
            )
            
            if records:
                for r in records:
                    key_point = r.get("key_point", "") or ""
                    if f"feed_id:{content.feed_id}" in key_point:
                        return True
            
            return False
        except Exception as e:
            logger.warning(f"检查重复失败: {e}")
            return False
    
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
        
        url = self._get_content_url(content)
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
            cache = []
            if CACHE_FILE.exists():
                cache = json.loads(CACHE_FILE.read_text())
            cache.append({"data": data, "time": time.time()})
            CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"缓存失败: {e}")
    
    async def retry_cached_writes(self) -> int:
        """重试缓存的写入"""
        if not CACHE_FILE.exists():
            return 0
        
        try:
            cache = json.loads(CACHE_FILE.read_text())
            success = 0
            remaining = []
            
            for item in cache:
                try:
                    await database_api.db_query(ChatHistory, query_type="create", data=item["data"])
                    success += 1
                except Exception:
                    remaining.append(item)
            
            if remaining:
                CACHE_FILE.write_text(json.dumps(remaining, ensure_ascii=False, indent=2))
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
        
        if len(text) < 200:
            return text
        
        # 使用LLM生成摘要
        try:
            models = llm_api.get_available_models()
            model_cfg = models.get("utils") or models.get("replyer")
            
            if model_cfg:
                prompt = f"请用一两句话概括以下内容的核心信息：\n\n{text[:1000]}"
                success, summary, _, _ = await llm_api.generate_with_model(
                    prompt=prompt,
                    model_config=model_cfg,
                    request_type="sns_summary",
                )
                if success and summary:
                    return summary.strip()
        except Exception as e:
            logger.warning(f"LLM摘要失败: {e}")
        
        # 降级：截断
        return text[:200] + "..."
    
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
    
    def _get_content_url(self, content: SNSContent) -> str:
        """获取内容URL"""
        if content.platform == "xiaohongshu":
            return f"https://xiaohongshu.com/explore/{content.feed_id}"
        return ""
    
    async def cleanup(self, days: int = 30, max_records: int = 1000) -> Tuple[int, int]:
        """清理旧记忆"""
        deleted = 0
        checked = 0
        
        # 获取SNS记忆
        records = await database_api.db_get(
            ChatHistory,
            filters={},
            order_by="-start_time",
            limit=max_records + 100,
        )
        
        if not records:
            return checked, deleted
        
        # 筛选SNS记忆
        sns_records = [r for r in records if str(r.get("chat_id", "")).startswith("sns_")]
        checked = len(sns_records)
        
        # 按时间清理
        cutoff = time.time() - days * 86400
        for r in sns_records:
            if r.get("start_time", 0) < cutoff:
                await database_api.db_query(
                    ChatHistory,
                    query_type="delete",
                    filters={"id": r["id"]},
                )
                deleted += 1
        
        # 按数量清理
        if len(sns_records) - deleted > max_records:
            to_delete = sns_records[max_records:]
            for r in to_delete:
                if r["id"] not in [x["id"] for x in sns_records[:max_records]]:
                    await database_api.db_query(
                        ChatHistory,
                        query_type="delete",
                        filters={"id": r["id"]},
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


# ============================================================================
# 命令处理器
# ============================================================================

class SNSCommand(BaseCommand):
    """SNS命令"""
    
    command_name = "sns_command"
    command_description = "社交平台采集命令"
    command_pattern = r"^[/／]sns(?:\s+(?P<action>collect|search|status|cleanup|config|dream))?(?:\s+(?P<arg>.+))?$"
    
    async def execute(self) -> Tuple[bool, str, bool]:
        action = self.matched_groups.get("action", "collect")
        arg = self.matched_groups.get("arg", "")
        
        config = _get_config()
        collector = SNSCollector(config)
        
        if action == "collect":
            result = await collector.collect()
            await self.send_text(f"SNS采集完成\n{result.summary()}")
        
        elif action == "dream":
            # 模拟做梦式采集：带人格兴趣匹配的采集
            await self.send_text("🌙 开始做梦式采集（带人格兴趣匹配）...")
            
            # 强制开启人格匹配
            dream_config = dict(config)
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
            await self.send_text("用法: /sns [collect|search <关键词>|status|cleanup|config|dream]")
        
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
        
        self.running = True
        interval = self.config.get("scheduler", {}).get("interval_minutes", 60) * 60
        
        if interval <= 0:
            logger.info("SNS定时任务已禁用")
            return
        
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
        return _plugin_instance.config
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
            config = _get_config()
            # 强制开启人格匹配
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
                # 直接查询数据库中的 SNS 记录
                records = await database_api.db_get(
                    ChatHistory,
                    filters={},
                    order_by="-start_time",
                    limit=100,
                )
                
                if not records:
                    return "未找到任何 SNS 记忆"
                
                # 筛选 SNS 记录并匹配关键词
                keywords_lower = [kw.lower().strip() for kw in keyword.split() if kw.strip()]
                matched = []
                
                for r in records:
                    # 只搜索 SNS 记录
                    if not str(r.get("chat_id", "")).startswith("sns_"):
                        continue
                    
                    # 在 theme、summary、keywords 中搜索
                    theme = (r.get("theme") or "").lower()
                    summary = (r.get("summary") or "").lower()
                    record_keywords = (r.get("keywords") or "").lower()
                    
                    # 检查是否匹配任一关键词
                    for kw in keywords_lower:
                        if kw in theme or kw in summary or kw in record_keywords:
                            matched.append(r)
                            break
                
                if not matched:
                    return f"未找到包含关键词「{keyword}」的 SNS 记忆"
                
                # 构建结果
                results = []
                for r in matched[:10]:  # 最多返回10条
                    platform = r.get("chat_id", "").replace("sns_", "")
                    results.append(
                        f"记忆ID：{r.get('id')}\n"
                        f"来源：{platform}\n"
                        f"主题：{r.get('theme', '(无)')}\n"
                        f"关键词：{r.get('keywords', '(无)')}"
                    )
                
                return f"找到 {len(matched)} 条 SNS 记忆（显示前{len(results)}条）：\n\n" + "\n\n---\n\n".join(results)
                
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
                
                # 查询记录（不限制 chat_id，支持跨聊天流获取 SNS 记忆）
                records = await database_api.db_get(
                    ChatHistory,
                    filters={},
                    limit=500,
                )
                
                # 筛选匹配的记录
                matched = [r for r in (records or []) if r.get("id") in id_list]
                
                if not matched:
                    return f"未找到ID为 {id_list} 的记忆"
                
                # 构建详情
                results = []
                for r in matched:
                    parts = [
                        f"记忆ID：{r.get('id')}",
                        f"来源：{r.get('chat_id', '').replace('sns_', '')}",
                        f"主题：{r.get('theme', '(无)')}",
                    ]
                    if r.get("summary"):
                        parts.append(f"概括：{r.get('summary')}")
                    if r.get("keywords"):
                        parts.append(f"关键词：{r.get('keywords')}")
                    results.append("\n".join(parts))
                
                return "\n\n" + "=" * 50 + "\n\n".join(results)
                
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
    python_dependencies = []
    config_file_name = "config.toml"
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        global _plugin_instance
        _plugin_instance = self
    
    config_schema = {
        "plugin": {
            "name": ConfigField(type=str, default="maibot_sns", description="插件名称"),
            "version": ConfigField(type=str, default="1.0.0", description="版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用"),
        },
        "platform": {
            "xiaohongshu": {
                "enabled": ConfigField(type=bool, default=True, description="启用小红书"),
                "mcp_server_name": ConfigField(type=str, default="xiaohongshu", description="MCP服务器名"),
            },
        },
        "filter": {
            "min_like_count": ConfigField(type=int, default=100, description="最小点赞数"),
            "keyword_whitelist": ConfigField(type=list, default=[], description="关键词白名单（优先保留）"),
            "keyword_blacklist": ConfigField(type=list, default=[], description="关键词黑名单"),
        },
        "processing": {
            "enable_summary": ConfigField(type=bool, default=True, description="启用LLM摘要"),
            "summary_threshold": ConfigField(type=int, default=200, description="摘要触发长度"),
            "enable_image_recognition": ConfigField(type=bool, default=False, description="启用图片识别"),
            "image_recognition_timeout": ConfigField(type=int, default=30, description="识图超时(秒)"),
        },
        "memory": {
            "max_records": ConfigField(type=int, default=1000, description="最大记录数"),
            "auto_cleanup_days": ConfigField(type=int, default=30, description="自动清理天数"),
        },
        "scheduler": {
            "enabled": ConfigField(type=bool, default=False, description="启用定时采集"),
            "interval_minutes": ConfigField(type=int, default=60, description="采集间隔(分钟)"),
            "first_delay_minutes": ConfigField(type=int, default=5, description="首次延迟(分钟)"),
        },
    }
    
    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """注册组件"""
        return [
            (SNSCollectTool.get_tool_info(), SNSCollectTool),
            (SNSCleanupTool.get_tool_info(), SNSCleanupTool),
            (SNSCommand.get_command_info(), SNSCommand),
            (SNSStartupHandler.get_handler_info(), SNSStartupHandler),
            (SNSShutdownHandler.get_handler_info(), SNSShutdownHandler),
        ]
