"""
内容处理器

负责内容过滤、摘要生成、关键词提取等
"""

import json
from typing import List, Dict, Any, Optional

from src.common.logger import get_logger
from src.plugin_system.apis import llm_api

from ..models import RawFeed, ProcessedFeed, FilterConfig

logger = get_logger("sns_processor")


class ContentProcessor:
    """内容处理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.filter_config = FilterConfig.from_dict(config.get("filter", {}))
        self.processing_config = config.get("processing", {})
    
    def filter_feeds(self, feeds: List[RawFeed]) -> List[RawFeed]:
        """过滤内容"""
        filtered = []
        
        for feed in feeds:
            # 点赞数过滤
            if feed.like_count < self.filter_config.min_like_count:
                logger.debug(f"过滤(点赞不足): {feed.feed_id}, likes={feed.like_count}")
                continue
            
            # 评论数过滤
            if feed.comment_count < self.filter_config.min_comment_count:
                logger.debug(f"过滤(评论不足): {feed.feed_id}, comments={feed.comment_count}")
                continue
            
            # 内容长度过滤
            content = feed.title + feed.content
            if len(content) > self.filter_config.max_content_length:
                logger.debug(f"过滤(内容过长): {feed.feed_id}, len={len(content)}")
                continue
            
            # 黑名单关键词过滤
            if self._contains_blacklist(content):
                logger.debug(f"过滤(黑名单): {feed.feed_id}")
                continue
            
            # 图片过滤（可选）
            if self.filter_config.enable_image_filter and not feed.image_urls:
                logger.debug(f"过滤(无图): {feed.feed_id}")
                continue
            
            filtered.append(feed)
        
        logger.info(f"过滤完成: {len(feeds)} -> {len(filtered)}")
        return filtered
    
    def _contains_blacklist(self, text: str) -> bool:
        """检查是否包含黑名单关键词"""
        text_lower = text.lower()
        for keyword in self.filter_config.keyword_blacklist:
            if keyword.lower() in text_lower:
                return True
        return False
    
    def _contains_whitelist(self, text: str) -> bool:
        """检查是否包含白名单关键词"""
        if not self.filter_config.keyword_whitelist:
            return True
        text_lower = text.lower()
        for keyword in self.filter_config.keyword_whitelist:
            if keyword.lower() in text_lower:
                return True
        return False
    
    async def process_feed(self, feed: RawFeed, adapter) -> Optional[ProcessedFeed]:
        """处理单条内容"""
        try:
            content = f"{feed.title}\n\n{feed.content}".strip()
            
            # 生成摘要
            summary = await self._generate_summary(content)
            
            # 提取关键词
            keywords = await self._extract_keywords(content, summary)
            
            # 提取主题
            theme = await self._extract_theme(content, feed.title)
            
            # 提取关键信息点
            key_points = await self._extract_key_points(content)
            
            # 计算质量分数
            quality_score = self._calculate_quality_score(feed)
            
            # 获取原始URL
            original_url = adapter.get_feed_url(feed.feed_id) if adapter else ""
            
            return ProcessedFeed(
                feed_id=feed.feed_id,
                platform=feed.platform,
                theme=theme,
                summary=f"[来自{feed.platform}] {summary}\n原文链接: {original_url}",
                keywords=keywords,
                key_points=key_points,
                image_descriptions=[],  # 暂不实现识图
                author=feed.author,
                publish_time=feed.publish_time,
                quality_score=quality_score,
                original_url=original_url,
                raw_content=content,
                like_count=feed.like_count,
                comment_count=feed.comment_count,
            )
        except Exception as e:
            logger.error(f"处理内容失败 feed_id={feed.feed_id}: {e}")
            return None
    
    async def _generate_summary(self, content: str) -> str:
        """生成摘要"""
        threshold = self.processing_config.get("summary_threshold", 500)
        max_length = self.processing_config.get("summary_max_length", 200)
        
        if len(content) <= threshold:
            return content[:max_length]
        
        if not self.processing_config.get("enable_summary", True):
            return content[:max_length] + "..."
        
        try:
            prompt = f"""请为以下内容生成一个简洁的摘要（不超过{max_length}字）：

{content[:2000]}

摘要："""
            
            models = llm_api.get_available_models()
            model_config = models.get("utils") or models.get("replyer")
            
            if model_config:
                success, result, _, _ = await llm_api.generate_with_model(
                    prompt=prompt,
                    model_config=model_config,
                    request_type="sns_summary",
                )
                if success and result:
                    return result.strip()[:max_length]
        except Exception as e:
            logger.warning(f"LLM摘要生成失败: {e}")
        
        # 降级：截断
        return content[:max_length] + "..."
    
    async def _extract_keywords(self, content: str, summary: str) -> List[str]:
        """提取关键词"""
        try:
            prompt = f"""从以下内容中提取3-5个关键词（用逗号分隔）：

{summary or content[:500]}

关键词："""
            
            models = llm_api.get_available_models()
            model_config = models.get("utils") or models.get("replyer")
            
            if model_config:
                success, result, _, _ = await llm_api.generate_with_model(
                    prompt=prompt,
                    model_config=model_config,
                    request_type="sns_keywords",
                )
                if success and result:
                    keywords = [k.strip() for k in result.replace("，", ",").split(",")]
                    return [k for k in keywords if k and len(k) < 20][:10]
        except Exception as e:
            logger.warning(f"关键词提取失败: {e}")
        
        return []
    
    async def _extract_theme(self, content: str, title: str) -> str:
        """提取主题"""
        if title and len(title) <= 50:
            return title
        
        try:
            prompt = f"""为以下内容生成一个简短的主题标题（不超过30字）：

{content[:500]}

主题："""
            
            models = llm_api.get_available_models()
            model_config = models.get("utils") or models.get("replyer")
            
            if model_config:
                success, result, _, _ = await llm_api.generate_with_model(
                    prompt=prompt,
                    model_config=model_config,
                    request_type="sns_theme",
                )
                if success and result:
                    return result.strip()[:50]
        except Exception as e:
            logger.warning(f"主题提取失败: {e}")
        
        return title[:50] if title else content[:30]
    
    async def _extract_key_points(self, content: str) -> List[str]:
        """提取关键信息点"""
        try:
            prompt = f"""从以下内容中提取2-3个关键信息点（每点一行）：

{content[:1000]}

关键信息："""
            
            models = llm_api.get_available_models()
            model_config = models.get("utils") or models.get("replyer")
            
            if model_config:
                success, result, _, _ = await llm_api.generate_with_model(
                    prompt=prompt,
                    model_config=model_config,
                    request_type="sns_keypoints",
                )
                if success and result:
                    points = [p.strip().lstrip("0123456789.-、）)") for p in result.split("\n")]
                    return [p for p in points if p and len(p) < 200][:5]
        except Exception as e:
            logger.warning(f"关键信息提取失败: {e}")
        
        return []
    
    def _calculate_quality_score(self, feed: RawFeed) -> float:
        """计算质量分数 (0-1)"""
        score = 0.0
        
        # 点赞数贡献 (最高0.4)
        like_score = min(feed.like_count / 10000, 1.0) * 0.4
        score += like_score
        
        # 评论数贡献 (最高0.3)
        comment_score = min(feed.comment_count / 1000, 1.0) * 0.3
        score += comment_score
        
        # 内容长度贡献 (最高0.2)
        content_len = len(feed.title) + len(feed.content)
        if 100 <= content_len <= 2000:
            score += 0.2
        elif content_len > 50:
            score += 0.1
        
        # 有图片加分 (最高0.1)
        if feed.image_urls:
            score += 0.1
        
        return min(score, 1.0)
