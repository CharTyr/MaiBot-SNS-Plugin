# MaiBot SNS Plugin

社交平台内容采集与记忆写入插件，让 MaiBot 能够从小红书等平台获取信息并学习。

## 功能特性

- 🔗 通过 MCP 桥接采集小红书内容
- 🧠 人格兴趣匹配 - 只学习 MaiBot 感兴趣的内容
- 🖼️ 图片识别 - 使用 VLM 理解图片内容
- 💾 写入 ChatHistory 记忆系统
- 🌙 做梦模块集成 - 在"梦境"中主动学习
- 🔍 记忆检索 - 回忆时可搜索 SNS 记忆

## 前置依赖

### 1. MCP 桥接插件

本插件依赖 [MaiBot-MCPBridgePlugin](https://github.com/CharTyr/MaiBot-MCPBridgePlugin)，请先安装。

### 2. 小红书 MCP Server

从 [GitHub Releases](https://github.com/xpzouying/xiaohongshu-mcp/releases) 下载对应平台的二进制文件：

**主程序（MCP 服务）：**
- macOS Apple Silicon: `xiaohongshu-mcp-darwin-arm64`
- macOS Intel: `xiaohongshu-mcp-darwin-amd64`
- Windows x64: `xiaohongshu-mcp-windows-amd64.exe`
- Linux x64: `xiaohongshu-mcp-linux-amd64`

**登录工具：**
- macOS Apple Silicon: `xiaohongshu-login-darwin-arm64`
- macOS Intel: `xiaohongshu-login-darwin-amd64`
- Windows x64: `xiaohongshu-login-windows-amd64.exe`
- Linux x64: `xiaohongshu-login-linux-amd64`

**使用步骤：**

```bash
# 1. 首先运行登录工具（扫码登录小红书）
#如果你运行在纯命令行环境，需要在有头环境进行扫码登录后获取到cookies.json放在此mcp的文件夹内
chmod +x xiaohongshu-login-linux-amd64
./xiaohongshu-login-linux-amd64

# 2. 然后启动 MCP 服务
chmod +x xiaohongshu-mcp-linux-amd64
./xiaohongshu-mcp-linux-amd64
```

默认运行在 `http://localhost:3000`

## 安装

将本插件放入 MaiBot 的 `plugins` 目录：

```bash
cd MaiBot/plugins
git clone https://github.com/CharTyr/MaiBot-SNS-Plugin.git MaiBot_SNS
```

## 配置

### 1. 配置 MCP 桥接插件

在 `MaiBot/plugins/MaiBot_MCPBridgePlugin/config.toml` 中添加小红书 MCP 服务器：

```toml
[[mcp_servers]]
name = "mcp_xiaohongshu"
url = "http://localhost:3000"
enabled = true
description = "小红书 MCP 服务"

# 禁用这些工具，以禁止 LLM 回复时被调用（只供 SNS 插件内部使用）
disabled_tools = """mcp_xiaohongshu_check_login_status
mcp_xiaohongshu_delete_cookies
mcp_xiaohongshu_favorite_feed
mcp_xiaohongshu_get_feed_detail
mcp_xiaohongshu_get_login_qrcode
mcp_xiaohongshu_like_feed
mcp_xiaohongshu_list_feeds
mcp_xiaohongshu_post_comment_to_feed
mcp_xiaohongshu_publish_content
mcp_xiaohongshu_publish_with_video
mcp_xiaohongshu_reply_comment_in_feed
mcp_xiaohongshu_search_feeds
mcp_xiaohongshu_user_profile"""
```

### 2. 配置 SNS 插件

复制示例配置并修改：

```bash
cp MaiBot/plugins/MaiBot_SNS/config.example.toml MaiBot/plugins/MaiBot_SNS/config.toml
```

主要配置项：

```toml
[plugin]
enabled = true

[platform.xiaohongshu]
enabled = true
mcp_server_name = "mcp_xiaohongshu"  # 与 MCP 桥接配置中的 name 对应
fetch_detail = true                   # 获取完整正文

[filter]
min_like_count = 20                   # 最小点赞数过滤

[processing]
enable_personality_match = true       # 启用人格兴趣匹配
enable_image_recognition = true       # 启用图片识别（需要 VLM 模型）

[scheduler]
enabled = false                       # 定时采集（建议先手动测试）
interval_minutes = 60

[dream]
enabled = true                        # 做梦模块集成

[debug]
enabled = true                        # 调试日志
```

## 使用

### 手动命令

```
/sns collect              # 采集推荐内容
/sns search <关键词>      # 搜索特定内容
/sns dream                # 做梦式采集（带人格匹配）
/sns status               # 查看记忆统计
/sns cleanup [天数]       # 清理旧记忆
/sns config               # 查看当前配置
```

### 做梦模块

启用 `[dream] enabled = true` 后，做梦 agent 可以调用 `collect_sns_content` 工具主动采集内容。

### 记忆检索

采集的内容会写入 ChatHistory，MaiBot 在回忆时可以通过 `search_sns_memory` 工具搜索这些记忆。

## 工作流程

```
1. 获取信息流 (list_feeds / search_feeds)
      ↓
2. 基础过滤 (点赞数、黑白名单)
      ↓
3. 人格兴趣匹配 (LLM 判断是否感兴趣)
      ↓
4. 获取详情 (get_feed_detail)
      ↓
5. 图片识别 (VLM 理解图片)
      ↓
6. 写入记忆 (ChatHistory)
```

## 日志示例

启用 debug 后可以看到详细的采集过程：

```
[SNS] 🚀 开始采集流程
[SNS]    平台: xiaohongshu
[SNS] 📥 阶段1: 获取信息流...
[SNS] ✓ 获取到 10 条内容
[SNS] 🔍 阶段2: 基础过滤...
[SNS] ✓ 基础过滤: 10 → 8 条
[SNS] 🧠 阶段3: 人格兴趣匹配...
[SNS] ✓ 人格匹配: 8 → 3 条
[SNS] 📄 阶段4: 获取详情...
[SNS] 🖼️ 开始识图，共 2 张图片
[SNS]    ✓ 识别结果: [图片：科技产品展示...]
[SNS] 💾 阶段5: 写入记忆...
[SNS] 🎉 采集完成!
```

## 注意事项

1. 小红书 MCP Server 需要登录才能获取完整内容，请按照其文档完成登录
2. 图片识别需要配置 VLM 模型（在 MaiBot 的 model_config.toml 中）
3. 建议先手动测试成功后再开启定时任务

