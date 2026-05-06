# astrbot_plugin_gpt-image-2

[![AstrBot](https://img.shields.io/badge/AstrBot-v4.16.0+-blue)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-yellow)](https://www.python.org/)

基于 GPT-Image-2 API 的 AstrBot 文生图/图生图插件。支持异步任务提交与轮询、引用图片生成、参数动态配置。

## ✨ 核心特性

- **文生图**：通过自然语言描述生成高质量图片
- **图生图**：引用一张图片后发送命令，基于参考图生成新图
- **异步轮询**：提交任务后立即返回，后台自动轮询获取结果
- **参数灵活**：支持 13 种宽高比、3 档分辨率、4 档质量
- **权限控制**：白名单机制 + 每日每人使用次数限制
- **本地缓存**：自动下载并缓存图片，定期清理旧文件

## 🚀 快速开始

### 前置条件

- Python >= 3.10
- 已部署的 AstrBot 实例 (v4.16.0+)
- 有效的 APIMart API Key（获取地址: https://apimart.ai/keys）

### 安装插件

```bash
# 进入 AstrBot 插件目录
cd AstrBot/data/plugins

# 克隆插件仓库
git clone https://github.com/swt2665048148-arch/astrbot_plugin_gpt-image-2.git

# 安装依赖
cd astrbot_plugin_gpt-image-2
pip install -r requirements.txt
```

### 配置插件

1. 打开 AstrBot WebUI → **插件管理** → 找到「GPT-Image-2 文生图/图生图」插件，点击启用。
2. 进入 **插件配置**，填写：
   - `API Key`：你的 APIMart API 密钥
   - `默认宽高比`：默认 `1:1`
   - `默认分辨率档位`：默认 `2k`
   - `默认图片质量`：默认 `auto`
3. 点击 **保存**，重启 AstrBot。

## 📖 使用指南

### 指令格式

```
/draw [参数] <提示词>
```

### 参数格式

参数由三部分组成：`宽高比@分辨率 质量`

| 部分 | 说明 | 可选值 |
|------|------|--------|
| 宽高比 | 画面比例 | `1:1`, `3:4`, `4:3`, `4:5`, `5:4`, `16:9`, `9:16`, `2:3`, `3:2`, `21:9`, `9:21`, `1:2`, `2:1`, `auto` |
| 分辨率 | 清晰度档位 | `1k`, `2k`, `4k`（4K 仅支持部分比例） |
| 质量 | 生成质量 | `auto`, `low`, `medium`, `high` |

参数可以省略，使用默认值：

```
/draw 一只在月球漫步的柴犬                    # 使用全部默认参数
/draw low 一只在月球漫步的柴犬                # 仅指定质量
/draw 16:9 一只在月球漫步的柴犬               # 仅指定比例
/draw 16:9@2k low 一只在月球漫步的柴犬        # 指定全部参数
```

### 图生图

引用一张图片后发送命令：

```
/draw 将这张图转换为动漫风格
/draw 16:9@4k high 基于这张图生成宽屏壁纸
```

### 使用示例

**文生图**
```
/draw 一只在月球漫步的柴犬，数字艺术风格
/draw 16:9@2k high 赛博朋克夜景
/draw 3:4 medium 一位穿和服的少女，浮世绘风格
```

**图生图**
```
[引用图片] /draw 将这张图转换为油画风格
[引用图片] /draw 21:9@4k high 基于这张图生成电影海报
```

## ⚙️ 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `api_url` | API 基础地址 | `https://api.apimart.ai/v1` |
| `api_key` | API Key | - |
| `whitelist` | 用户白名单（留空则所有人可用） | `[]` |
| `daily_limit` | 每日每人使用次数上限（0 不限制） | `10` |
| `default_size` | 默认宽高比 | `1:1` |
| `default_resolution` | 默认分辨率档位 | `2k` |
| `default_quality` | 默认图片质量 | `auto` |
| `max_retries` | 最大轮询次数 | `30` |
| `poll_interval` | 轮询间隔（秒） | `5` |

## 🛠️ 技术架构

### 文件结构

```
astrbot_plugin_gpt-image-2/
├── main.py              # 插件主代码
├── _conf_schema.json    # 配置面板定义
├── metadata.yaml        # 插件元数据
├── requirements.txt     # 依赖列表
├── LICENSE              # 开源许可证
└── README.md            # 你正在阅读的文档
```

### 核心工作流

1. 用户发送 `/draw` 命令（可选引用图片）
2. 插件检查权限与次数，解析参数
3. 向 GPT-Image-2 API 提交生成任务（POST `/v1/images/generations`）
4. API 返回 `task_id`，插件即刻回复用户任务信息
5. 后台启动异步轮询（GET `/v1/tasks/{task_id}`），每隔 N 秒查询一次任务状态
6. 任务完成后，自动下载图片到本地，清理旧缓存（保留最新 20 张），发送图片到原会话

### API 参考

- 提交任务：`POST /v1/images/generations`
- 查询任务：`GET /v1/tasks/{task_id}`

详细参数与返回格式参见 [APIMart GPT Image 2 文档](https://docs.apimart.ai/cn/api-reference/images/gpt-image-2/official)。

### 4K 分辨率限制

仅以下比例支持 4K 分辨率：

| 比例 | 4K 像素 |
|------|---------|
| `16:9` | 3840×2160 |
| `9:16` | 2160×3840 |
| `2:1` | 3840×1920 |
| `1:2` | 1920×3840 |
| `21:9` | 3840×1648 |
| `9:21` | 1648×3840 |

其他比例使用 4K 会返回错误，建议使用 `2k` 分辨率。

## 🧩 依赖

```
aiohttp>=3.8
```

## 📄 许可证

本项目基于 MIT 许可证开源。详见 [LICENSE](./LICENSE) 文件。

## 🙏 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 松耦合、多平台、易扩展的聊天机器人开发框架。
- [APIMart](https://apimart.ai) — AI 模型聚合与 API 平台，提供 GPT-Image-2 图片生成服务。
