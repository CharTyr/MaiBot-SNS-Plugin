# MaiBot_SNS 开发文档

> 本文档供 AI 助手在后续开发时参考，包含插件架构、核心模块、扩展方式等关键信息。

## 项目概述

MaiBot_SNS 是一个社交平台内容采集插件，通过 MCP 桥接从小红书等平台获取内容，经过过滤和处理后写入 MaiBot 的 ChatHistory 记忆系统。

**核心能力：**
- 多平台适配器架构（目前支持小红书）
- 人格兴趣匹配（LLM 判断内容是否符合 MaiBot 兴趣）
- 图片识别（VLM）
- 做梦模块集成
- 记忆检索工具注册

## 目录结构

```
MaiBot_SNS/
├── plugin.py              # 主文件，包含所有核心逻辑
├── __init__.py            # 导出 MaiBotSNSPlugin
├── _manifest.json         # 插件元信息
├── config.toml            # 运行时配置
├── config.example.toml    # 配置模板
├── README.md              # 用户文档
├── Agent.md               # 本文档（开发文档）
├── processors/
│   └── content_processor.py  # 内容处理器（备用，主逻辑在 plugin.py）
└── tests/
    └── test_plugin.py     # 单元测试
```

## 核心架构

### 1. 数据模型

```python
@dataclass
class SNSContent:
    """社交平台内容（通用格式）"""
    feed_id: str           # 内容唯一标识
    platform: str          # 平台名称
    title: str             # 标题
    content: str           # 正文内容
    author: str            # 作者
    like_count: int        # 点赞数
    comment_count: int     # 评论数
    image_urls: List[str]  # 图片列表
    url: str               # 原文链接
    extra: Dict[str, Any]  # 额外数据（平台特定，如 xsec_token）

@dataclass
class CollectResult:
    """采集结果"""
    success: bool
    fetched: int           # 获取数量
    written: int           # 写入数量
    filtered: int          # 过滤数量
    duplicate: int         # 重复数量
    errors: List[str]
    preview_contents: List[Dict]  # 预览模式内容
```

### 2. 平台适配器

适配器负责解析不同平台的 API 返回数据，将其转换为统一的 `SNSContent` 格式。

```python
class PlatformAdapter:
    """平台适配器基类"""
    platform_name: str = "generic"
    
    # 工具名映射
    default_tools = {
        "list": "list_feeds",
        "search": "search_feeds",
        "detail": "get_feed_detail",
    }
    
    # 字段映射（从 MCP 返回数据映射到 SNSContent）
    field_mapping = {
        "feed_id": ["id", "note_id", "feed_id"],
        "title": ["title", "displayTitle"],
        "content": ["content", "desc"],
        # ...
    }
    
    def parse_list_result(self, result: str) -> List[SNSContent]: ...
    def parse_detail_result(self, result: str, content: SNSContent) -> SNSContent: ...
    def get_content_url(self, content: SNSContent) -> str: ...

class XiaohongshuAdapter(PlatformAdapter):
    """小红书适配器"""
    platform_name = "xiaohongshu"
    # 覆盖 field_mapping 以适配小红书 API
```

**添加新平台：**
1. 创建新的适配器类继承 `PlatformAdapter`
2. 覆盖 `field_mapping` 和必要的解析方法
3. 注册到 `PLATFORM_ADAPTERS` 字典
4. 在 `config.toml` 中添加平台配置

### 3. 采集器 (SNSCollector)

核心采集逻辑，包含完整的采集流程：

```python
class SNSCollector:
    async def collect(self, platform, keyword, count, preview_only) -> CollectResult:
        # 1. 获取信息流 (_fetch_contents)
        # 2. 基础过滤 (_filter_contents) - 点赞数、黑白名单
        # 3. 人格兴趣匹配 (_match_personality_interest) - LLM 判断
        # 4. 获取详情 (_fetch_details) - 并发获取完整正文
        # 5. 写入记忆 (_write_to_memory) - 或预览
```

**关键方法：**

| 方法 | 功能 |
|------|------|
| `_fetch_contents()` | 调用 MCP 工具获取列表 |
| `_filter_contents()` | 点赞数、黑白名单过滤 |
| `_match_personality_interest()` | LLM 判断是否符合兴趣 |
| `_fetch_details()` | 并发获取详情（正文+图片） |
| `_recognize_images()` | 调用 VLM 识图 |
| `_write_to_memory()` | 写入 ChatHistory |
| `_check_duplicate_cached()` | 使用内存缓存检查重复 |

### 4. 全局状态

```python
# 采集统计（用于 WebUI 和 /sns stats 命令）
_collector_stats: Dict[str, Any] = {
    "last_collect_time": 0,
    "total_collected": 0,
    "total_written": 0,
    "total_filtered": 0,
    "total_duplicate": 0,
    "is_running": False,
    "recent_memories": [],
}

# feed_id 缓存（避免重复查询数据库）
_feed_id_cache: set = set()
```

## 组件注册

### 工具 (Tools)

| 工具名 | 用途 | LLM 可用 |
|--------|------|----------|
| `collect_sns_content` | 做梦模块调用采集 | ❌ |
| `cleanup_sns_memory` | 清理过期记忆 | ❌ |
| `sns_get_status` | WebUI 状态查询 | ❌ |

### 命令 (Commands)

```
/sns                    # 帮助
/sns collect            # 采集推荐内容
/sns preview            # 预览模式
/sns search <关键词>    # 搜索采集
/sns dream              # 做梦式采集（强制人格匹配）
/sns stats              # 采集统计
/sns status             # 记忆统计
/sns cleanup [天数]     # 清理旧记忆
/sns config             # 查看配置
```

### 事件处理器

| 处理器 | 事件 | 功能 |
|--------|------|------|
| `SNSStartupHandler` | `ON_START` | 注册 Dream 工具、记忆检索工具、启动定时任务 |
| `SNSShutdownHandler` | `ON_STOP` | 停止定时任务 |

## 外部集成

### 1. MCP 桥接调用

通过 `tool_api` 调用 MCP 工具：

```python
from src.plugin_system.apis import tool_api

tool = tool_api.get_tool_instance("mcp_xiaohongshu_list_feeds")
result = await tool.direct_execute()  # 或 direct_execute(keyword="xxx")
```

### 2. 做梦模块集成

在 `_register_dream_tools()` 中注册：

```python
from src.dream.dream_agent import get_dream_tool_registry, DreamTool

registry = get_dream_tool_registry()
registry.register_tool(DreamTool(
    name="collect_sns_content",
    description="...",
    parameters=[...],
    execute_func=collect_sns_content,
))
```

### 3. 记忆检索集成

在 `_register_memory_retrieval_tools()` 中注册：

```python
from src.memory_system.retrieval_tools import register_memory_retrieval_tool

register_memory_retrieval_tool(
    name="search_sns_memory",
    description="搜索社交平台采集的记忆...",
    parameters=[...],
    execute_func=search_sns_memory,
)
```

**重要：** SNS 记忆使用 `chat_id = "sns_{platform}"` 格式存储，不依赖主程序的 `global_memory` 配置。

### 4. 数据库操作

使用 `database_api` 操作 ChatHistory：

```python
from src.plugin_system.apis import database_api
from src.common.database.database_model import ChatHistory

# 查询
records = await database_api.db_get(ChatHistory, filters={}, limit=100)

# 写入
await database_api.db_query(ChatHistory, query_type="create", data={
    "chat_id": "sns_xiaohongshu",
    "theme": "...",
    "summary": "...",
    "keywords": json.dumps([...]),
    "key_point": json.dumps(["feed_id:xxx", "likes:123"]),
    # ...
})

# 删除
await database_api.db_query(ChatHistory, query_type="delete", filters={"id": xxx})
```

### 5. LLM 调用

```python
from src.plugin_system.apis import llm_api

models = llm_api.get_available_models()
model_cfg = models.get("utils") or models.get("replyer")

success, response, _, _ = await llm_api.generate_with_model(
    prompt="...",
    model_config=model_cfg,
    request_type="sns_xxx",
)
```

### 6. 图片识别

```python
from src.chat.utils.utils_image import get_image_manager

image_manager = get_image_manager()
desc = await image_manager.get_image_description(image_base64)
```

## 配置结构

配置文件 `config.toml` 的主要 section：

| Section | 说明 |
|---------|------|
| `[plugin]` | 插件开关 |
| `[platform.xiaohongshu]` | 小红书平台配置 |
| `[filter]` | 过滤规则（点赞数、黑白名单） |
| `[processing]` | 处理选项（人格匹配、摘要、识图） |
| `[memory]` | 记忆存储（最大数量、清理天数） |
| `[scheduler]` | 定时任务 |
| `[dream]` | 做梦模块集成 |
| `[debug]` | 调试日志 |

## 扩展指南

### 添加新平台

1. **创建适配器**

```python
class WeiboAdapter(PlatformAdapter):
    platform_name = "weibo"
    
    field_mapping = {
        "feed_id": ["id", "mid"],
        "title": ["text"],
        "author": ["user.screen_name"],
        "like_count": ["attitudes_count"],
        # ...
    }
    
    def _parse_item(self, item: Dict) -> Optional[SNSContent]:
        # 自定义解析逻辑（如果需要）
        return super()._parse_item(item)

# 注册
PLATFORM_ADAPTERS["weibo"] = WeiboAdapter
```

2. **添加配置**

```toml
[platform.weibo]
enabled = true
mcp_server_name = "mcp_weibo"
fetch_detail = true

# 可选：自定义工具名
[platform.weibo.tools]
list = "get_timeline"
search = "search_posts"
detail = "get_post_detail"
```

3. **配置 MCP 桥接**

在 `MaiBot_MCPBridgePlugin/config.toml` 中添加对应的 MCP 服务器。

### 添加新的处理步骤

在 `SNSCollector.collect()` 方法中添加新的处理阶段：

```python
async def collect(self, ...):
    # ... 现有步骤 ...
    
    # 新步骤：情感分析
    if self.processing_cfg.get("enable_sentiment_analysis"):
        filtered = await self._analyze_sentiment(filtered)
    
    # ... 继续 ...
```

### 添加新命令

在 `SNSCommand.execute()` 中添加新的 action：

```python
elif action == "export":
    # 导出记忆
    records = await database_api.db_get(...)
    # ...
    await self.send_text("导出完成")
```

## 调试技巧

1. **开启调试日志**

```toml
[debug]
enabled = true
```

2. **使用预览模式**

```
/sns preview
```

3. **查看采集统计**

```
/sns stats
```

4. **检查 MCP 连接**

```
/mcp status
```

## 常见问题

### Q: 采集不到内容？

1. 检查 MCP 服务器是否运行：`/mcp status`
2. 检查小红书登录状态（cookies 是否过期）
3. 降低 `min_like_count` 配置
4. 开启 debug 查看详细日志

### Q: 人格匹配过滤太多？

1. 检查 MaiBot 的 `personality.interest` 配置是否合理
2. 临时关闭人格匹配：`processing.enable_personality_match = false`

### Q: 图片识别失败？

1. 检查 VLM 模型配置
2. 检查网络是否能访问图片 URL
3. 增加 `image_recognition_timeout`

## API 参考

### MaiBot 插件系统

```python
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
from src.plugin_system.apis import tool_api, llm_api, database_api
```

### 数据库模型

```python
from src.common.database.database_model import ChatHistory

# ChatHistory 字段
# - id: int
# - chat_id: str (SNS 使用 "sns_{platform}")
# - start_time: float
# - end_time: float
# - original_text: str
# - participants: str (JSON)
# - theme: str
# - keywords: str (JSON)
# - summary: str
# - key_point: str (JSON, 包含 "feed_id:xxx")
```

## 版本历史

- **1.0.0** - 初始版本
  - 小红书采集支持
  - 人格兴趣匹配
  - 做梦模块集成
  - 记忆检索工具
