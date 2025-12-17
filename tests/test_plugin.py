"""
MaiBot_SNS 插件单元测试
"""

import pytest
from unittest.mock import AsyncMock, patch
import json

# 测试数据模型
from .. import plugin as plugin_module
from ..plugin import SNSContent, CollectResult, SNSCollector, XiaohongshuAdapter


@pytest.fixture(autouse=True)
def _reset_plugin_globals():
    plugin_module._feed_id_cache.clear()
    plugin_module._collector_stats["is_running"] = False
    plugin_module._collector_stats["recent_memories"] = []
    yield
    plugin_module._feed_id_cache.clear()


class TestSNSContent:
    """测试SNSContent数据类"""
    
    def test_create_content(self):
        content = SNSContent(
            feed_id="123",
            platform="xiaohongshu",
            title="测试标题",
            content="测试内容",
            author="测试作者",
            like_count=500,
        )
        assert content.feed_id == "123"
        assert content.platform == "xiaohongshu"
        assert content.like_count == 500
        assert content.image_urls == []


class TestCollectResult:
    """测试CollectResult数据类"""
    
    def test_summary_success(self):
        result = CollectResult(
            success=True,
            fetched=10,
            written=5,
            filtered=3,
            duplicate=2,
        )
        summary = result.summary()
        assert "✅" in summary
        assert "获取:10" in summary
        assert "写入:5" in summary
    
    def test_summary_failure(self):
        result = CollectResult(success=False, errors=["测试错误"])
        summary = result.summary()
        assert "❌" in summary


class TestSNSCollector:
    """测试SNSCollector"""
    
    @pytest.fixture
    def config(self):
        return {
            "platform": {
                "xiaohongshu": {
                    "enabled": True,
                    "mcp_server_name": "xiaohongshu",
                }
            },
            "filter": {
                "min_like_count": 100,
                "keyword_blacklist": ["广告", "推广"],
                "keyword_whitelist": ["教程"],
            },
            "processing": {
                "enable_summary": True,
                "enable_image_recognition": False,
            },
            "memory": {
                "max_records": 1000,
            },
        }
    
    @pytest.fixture
    def collector(self, config):
        return SNSCollector(config)
    
    def test_filter_by_likes(self, collector):
        """测试点赞数过滤"""
        contents = [
            SNSContent("1", "xhs", "标题1", "内容1", "作者1", like_count=50),
            SNSContent("2", "xhs", "标题2", "内容2", "作者2", like_count=200),
            SNSContent("3", "xhs", "标题3", "内容3", "作者3", like_count=150),
        ]
        filtered = collector._filter_contents(contents)
        assert len(filtered) == 2
        assert all(c.like_count >= 100 for c in filtered)
    
    def test_filter_by_blacklist(self, collector):
        """测试黑名单过滤"""
        contents = [
            SNSContent("1", "xhs", "正常标题", "正常内容", "作者1", like_count=200),
            SNSContent("2", "xhs", "广告标题", "推广内容", "作者2", like_count=200),
        ]
        filtered = collector._filter_contents(contents)
        assert len(filtered) == 1
        assert filtered[0].feed_id == "1"
    
    def test_filter_whitelist_priority(self, collector):
        """测试白名单优先保留"""
        contents = [
            SNSContent("1", "xhs", "教程分享", "内容", "作者1", like_count=50),  # 点赞低但在白名单
        ]
        filtered = collector._filter_contents(contents)
        assert len(filtered) == 1
        assert filtered[0].feed_id == "1"
    
    def test_parse_mcp_result_json_list(self, collector):
        """测试解析JSON列表格式（适配器解析）"""
        result = json.dumps([
            {"id": "123", "title": "标题", "desc": "内容", "nickname": "作者", "likedCount": 100},
        ])
        adapter = collector._get_adapter("xiaohongshu")
        contents = adapter.parse_list_result(result)
        assert len(contents) == 1
        assert contents[0].feed_id == "123"
        assert contents[0].title == "标题"
    
    def test_parse_mcp_result_json_object(self, collector):
        """测试解析JSON对象格式（items 列表）"""
        result = json.dumps({
            "items": [
                {"id": "456", "title": "标题2", "desc": "内容2", "nickname": "作者2", "likedCount": 200},
            ]
        })
        adapter = collector._get_adapter("xiaohongshu")
        contents = adapter.parse_list_result(result)
        assert len(contents) == 1
        assert contents[0].feed_id == "456"
    
    def test_parse_mcp_result_invalid_json(self, collector):
        """测试解析无效JSON（适配器容错）"""
        result = "这不是JSON"
        adapter = collector._get_adapter("xiaohongshu")
        contents = adapter.parse_list_result(result)
        assert len(contents) == 0
    
    def test_get_content_url(self, collector):
        """测试获取内容URL"""
        content = SNSContent("abc123", "xiaohongshu", "", "", "")
        adapter = collector._get_adapter("xiaohongshu")
        assert isinstance(adapter, XiaohongshuAdapter)
        url = adapter.get_content_url(content)
        assert url == "https://xiaohongshu.com/explore/abc123"
    
    @pytest.mark.asyncio
    async def test_generate_summary_short_text(self, collector):
        """测试短文本不生成摘要"""
        content = SNSContent("1", "xhs", "短标题", "短内容", "作者")
        summary = await collector._generate_summary(content)
        assert "短标题" in summary
        assert "短内容" in summary
    
    @pytest.mark.asyncio
    async def test_extract_keywords(self, collector):
        """测试关键词提取"""
        content = SNSContent("1", "xhs", "Python 教程 入门", "内容", "作者")
        keywords = await collector._extract_keywords(content)
        assert len(keywords) <= 8
        assert "Python" in keywords

    def test_extract_feed_id_from_key_point(self, collector):
        """测试从 key_point 提取 feed_id（兼容 JSON 字符串）"""
        key_point = json.dumps(["feed_id:abc_123", "likes:10"], ensure_ascii=False)
        assert collector._extract_feed_id_from_key_point(key_point) == "abc_123"


class TestCollectorIntegration:
    """集成测试（需要Mock外部依赖）"""
    
    @pytest.fixture
    def config(self):
        return {
            "platform": {"xiaohongshu": {"mcp_server_name": "xiaohongshu"}},
            "filter": {"min_like_count": 0},
            "processing": {},
            "memory": {},
        }
    
    @pytest.mark.asyncio
    async def test_collect_with_mock_tool(self, config):
        """测试采集流程（Mock MCP工具）"""
        mock_tool = AsyncMock()
        mock_tool.direct_execute.return_value = {
            "content": json.dumps([
                {"id": "1", "title": "测试", "desc": "内容", "nickname": "作者", "likedCount": 100}
            ])
        }
        
        with patch("plugins.MaiBot_SNS.plugin.tool_api") as mock_api:
            mock_api.get_tool_instance.return_value = mock_tool
            
            with patch("plugins.MaiBot_SNS.plugin.database_api") as mock_db:
                mock_db.db_get = AsyncMock(return_value=[])
                mock_db.db_query = AsyncMock(return_value=None)
                
                collector = SNSCollector(config)
                result = await collector.collect(count=1)
                
                assert result.success
                assert result.fetched == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
