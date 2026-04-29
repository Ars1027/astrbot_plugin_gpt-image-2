# astrbot_plugin_gpt-image-2

[![AstrBot](https://img.shields.io/badge/AstrBot-v4.23.6+-blue)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-yellow)](https://www.python.org/)

基于 GPT-Image-2 API 的 AstrBot 文生图插件。支持异步任务提交与轮询、多套预设参数、权限控制与每日次数限制，彻底解放你的想象力。

## ✨ 核心特性

- **文生图**：通过自然语言描述即可生成高质量图片。
- **异步轮询**：提交任务后立即返回，后台自动轮询获取结果，不阻塞聊天。
- **多预设切换**：支持在插件配置面板中定义多套生成参数（分辨率、比例、回复模板等），一键切换。
- **权限与次数控制**：白名单机制 + 每日每人使用次数限制，防止滥用。
- **跨平台图片发送**：自动适配微信 OC、OneBot 等主流适配器的图片上传与发送规范。
- **本地缓存管理**：自动下载并缓存图片，支持设置最大保留数量，定期清理。
- **任务状态查询**：通过 `/check` 命令随时查询生成进度。

## 🚀 快速开始

### 前置条件

- Python >= 3.10
- 已部署的 AstrBot 实例 (v4.23.6+)
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

### 快速配置

1. 打开 AstrBot WebUI → **插件管理** → 找到「调用GPT image 2」插件，点击启用。
2. 进入 **插件配置**，填写：
   - `API Key`：你的 APIMart API 密钥。
   - `默认预设`：保持 `default` 即可。
   - `预设配置列表`：点击「添加条目」，填写至少一个预设（见下方示例）。
3. 点击 **保存**，重启 AstrBot。

### 添加预设示例

在「预设配置列表」中添加条目，填写以下信息：

| 字段 | 值 | 说明 |
|------|-----|------|
| 预设 ID | `default` | 唯一标识，用于命令切换 |
| 预设名称 | `默认` | 面板显示名称 |
| 回复模板 | `图库搜寻中… 任务 ID：{task_id}` | 提交任务后的快速回复 |
| 图片比例 | `1:1` | 可选 16:9、9:16 等 13 种 |
| 分辨率 | `2k` | 可选 1k、2k、4k |
| 生成张数 | `1` | 建议保持 1 |
| 附加 JSON 参数 | 留空 | 需要额外参数时填写 |

## 📖 使用指南

### 指令列表

| 指令 | 说明 |
|------|------|
| `/draw <提示词>` | 使用默认预设置生成一张图片 |
| `/draw <预设ID> <提示词>` | 使用指定预设生成图片 |
| `/check <任务ID>` | 查询任务状态与进度 |

### 使用示例

```
/draw 一只在月球漫步的柴犬，数字艺术风格
```

机器人将立即回复预设模板中的提示语（如“图库搜寻中… 任务 ID：task_xxx”），并在图片生成后自动将结果发送到当前会话。

使用指定预设：

```
/draw anime 一位穿和服的少女，浮世绘风格
```

### 轮询参数调整

在插件配置中可自定义轮询行为：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 轮询间隔 | 2 秒 | 查询任务状态的间隔 |
| 最大重试次数 | 10 次 | 超此次数后任务视为失败 |

默认情况下，最多等待 20 秒（2 秒 × 10 次）。API 的 `estimated_time` 字段可作为轮询间隔参考。

## 🛠️ 技术架构

### 文件结构

```
astrbot_plugin_gpt-image-2/
├── main.py                # 插件主代码
├── _conf_schema.json      # 配置面板定义
├── metadata.yaml          # 插件元数据
├── requirements.txt       # 依赖列表
├── logo.png               # 插件图标（可选）
├── LICENSE                # 开源许可证
└── README.md              # 你正在阅读的文档
```

### 核心工作流

1. 用户发送 `/draw` 指令。
2. 插件检查权限与次数后，向 GPT-Image-2 API 提交生成任务（POST `/v1/images/generations`）。
3. API 返回 `task_id`，插件即刻回复用户预设的提示语。
4. 后台启动异步轮询（GET `/v1/tasks/{task_id}`），每隔 N 秒查询一次任务状态。
5. 任务完成后，自动下载图片到本地，清理旧缓存（保留最新 20 张），并通过适配器发送图片到原会话。
6. 整个过程不阻塞主线程，不影响其他插件正常响应。

### API 参考

- 提交任务：`POST https://api.apimart.ai/v1/images/generations`
- 查询任务：`GET https://api.apimart.ai/v1/tasks/{task_id}`

详细参数与返回格式参见 [APIMart GPT Image 2 文档](https://docs.apimart.ai/en/api-reference/images/gpt-image-2/generation)。

### 跨平台适配

插件使用 `MessageChain().file_image()` 方法发送图片，已针对微信 OC 和 OneBot 进行适配：
- 自动将 RGBA/PNG 图片转为 RGB/JPEG 格式。
- 压缩到 1024×1024 以内，文件大小控制在 2MB 以下。
- 微信 OC 适配器将自动完成图片上传与 media_id 获取。

### 配置存储

- 插件配置文件 `data/plugin_config/gpt-image2.json`（由 WebUI 自动管理）。
- 图片缓存目录 `data/plugin_data/gpt-image2/images/`（自动创建与清理）。
- 权限与次数数据通过 AstrBot KV 存储持久化。

所有数据目录均基于 AstrBot 框架提供的 `context.astrbot_data_path` 动态获取，确保跨环境移植时路径正确。

## 🧩 依赖

```
aiohttp>=3.8
pillow
```

## 📄 许可证

本项目基于 MIT 许可证开源。详见 [LICENSE](./LICENSE) 文件。

## 🙏 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 松耦合、多平台、易扩展的聊天机器人开发框架。
- [APIMart](https://apimart.ai) — AI 模型聚合与 API 平台，提供 GPT-Image-2 图片生成服务。
```
