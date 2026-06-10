import asyncio
import base64
import io
import json
import re
import time
import traceback
from typing import Any

import aiohttp
from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.tool import FunctionTool, ToolExecResult

# 支持的宽高比
VALID_SIZES = {
    "auto",
    "1:1",
    "3:2",
    "2:3",
    "4:3",
    "3:4",
    "5:4",
    "4:5",
    "16:9",
    "9:16",
    "2:1",
    "1:2",
    "21:9",
    "9:21",
}

# 支持 4K 的宽高比
SIZES_SUPPORT_4K = {"16:9", "9:16", "2:1", "1:2", "21:9", "9:21"}

# 支持的分辨率
VALID_RESOLUTIONS = {"1k", "2k", "4k"}

# 支持的质量
VALID_QUALITIES = {"auto", "low", "medium", "high"}

# APIMart 上传接口限制：单张图片最大 20MB。
MAX_REFERENCE_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@dataclass
class GenerateImageTool(FunctionTool):
    plugin: Any = Field(default=None, exclude=True, repr=False)
    name: str = "generate_image"
    description: str = (
        "调用 GPT-Image-2 真正提交图片生成任务。当用户要求画图、生成图片、"
        "出图、改图、基于引用图片重绘或转换风格时，必须调用此工具。不要回复 "
        "/draw 命令文本，不要使用 shell 查看插件文件。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "最终生图提示词，描述要生成或修改成什么画面。",
                },
                "size": {
                    "type": "string",
                    "description": "可选宽高比。",
                    "enum": sorted(VALID_SIZES),
                },
                "resolution": {
                    "type": "string",
                    "description": "可选分辨率档位。",
                    "enum": sorted(VALID_RESOLUTIONS),
                },
                "quality": {
                    "type": "string",
                    "description": "可选图片质量。",
                    "enum": sorted(VALID_QUALITIES),
                },
                "use_quoted_image": {
                    "type": "boolean",
                    "description": "是否使用当前消息引用的图片做图生图，默认 true。",
                },
            },
            "required": ["prompt"],
        }
    )

    async def call(self, context, **kwargs) -> ToolExecResult:
        if not self.plugin:
            return "生成图片失败：插件实例未绑定。"

        event = getattr(getattr(context, "context", None), "event", None)
        if event is None:
            event = getattr(context, "event", None)
        if event is None:
            return "生成图片失败：无法获取当前消息事件。"

        return await self.plugin._invoke_generate_image_tool(event, **kwargs)


@register(
    "gpt-image-2",
    "Luochang",
    "调用 GPT-Image-2 API 生成图片，支持文生图和图生图",
    "v2.0.0",
)
class GPTImage2Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # API 配置
        self.api_url = str(
            config.get("api_url", "https://api.apimart.ai/v1") or ""
        ).rstrip("/")
        self.api_key = str(config.get("api_key", "") or "")
        self.whitelist = config.get("whitelist", [])
        self.daily_limit = int(config.get("daily_limit", 10) or 10)

        # 默认生成参数
        self.default_size = str(config.get("default_size", "1:1") or "1:1")
        self.default_resolution = str(config.get("default_resolution", "2k") or "2k")
        self.default_quality = str(config.get("default_quality", "auto") or "auto")

        # 轮询配置
        self.max_retries = int(config.get("max_retries", 30) or 30)
        self.poll_interval = int(config.get("poll_interval", 5) or 5)

        # HTTP 会话（在 initialize 中创建）
        self.session: aiohttp.ClientSession | None = None

    async def initialize(self):
        """插件启动后调用"""
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        try:
            self.context.add_llm_tools(GenerateImageTool(plugin=self))
            logger.info("GPT-Image-2 generate_image LLM Tool 已显式注册")
        except Exception as e:
            logger.error(f"注册 generate_image LLM Tool 失败: {e}")
        logger.info("GPT-Image-2 插件已初始化")

    async def terminate(self):
        """插件卸载时调用"""
        if self.session:
            await self.session.close()
            self.session = None
        logger.info("GPT-Image-2 插件已卸载")

    # ========== 参数解析 ==========

    def _parse_draw_args(self, arg_str: str) -> tuple[dict, str, str]:
        """
        解析 /draw 后的参数和提示词，返回 (params, prompt, error_msg)。

        参数必须是空格分隔的前置 token：
        - 16:9 2k high
        - 3:4 medium
        - low
        - 2k
        """
        tokens = arg_str.strip().split()
        params = {}
        labels = {
            "size": "宽高比",
            "resolution": "分辨率",
            "quality": "质量",
        }

        for index, token in enumerate(tokens):
            value = token.lower()

            if "@" in value:
                return (
                    {},
                    "",
                    "参数格式错误：不再支持 @ 写法，请使用空格分隔，"
                    "例如：/draw 4:3 2k medium 一只狗",
                )

            param_name = ""
            if value == "auto":
                # auto 既可以是宽高比也可以是质量；优先作为宽高比解析。
                param_name = "size" if "size" not in params else "quality"
            elif value in VALID_SIZES or re.fullmatch(r"\d+:\d+", value):
                param_name = "size"
            elif value in VALID_RESOLUTIONS or re.fullmatch(r"\d+k", value):
                param_name = "resolution"
            elif value in VALID_QUALITIES:
                param_name = "quality"
            else:
                return params, " ".join(tokens[index:]).strip(), ""

            if param_name in params:
                return {}, "", f"参数重复：{labels[param_name]}"
            params[param_name] = value

        return params, "", "请提供提示词，例如：/draw 4:3 2k medium 一只狗"

    def _parse_params(self, param_str: str) -> dict | None:
        """兼容旧内部调用：只解析空格分隔参数，不包含提示词。"""
        params, prompt, error = self._parse_draw_args(f"{param_str} __prompt__")
        if error or prompt != "__prompt__":
            return None
        return params

    def _looks_like_draw_param_token(self, token: str) -> bool:
        value = token.strip().lower()
        return (
            value == "auto"
            or value in VALID_SIZES
            or re.fullmatch(r"\d+:\d+", value) is not None
            or value in VALID_RESOLUTIONS
            or re.fullmatch(r"\d+k", value) is not None
            or value in VALID_QUALITIES
        )

    def _parse_llm_draw_response(self, text: str) -> tuple[dict, str, str]:
        """解析 LLM 错误输出的 /draw 文本；仅兜底兼容旧 @ 参数写法。"""
        stripped = (text or "").strip()
        if not re.match(r"^/draw(?:\s+|$)", stripped, flags=re.IGNORECASE):
            return {}, "", ""

        parts = stripped.split(None, 1)
        if len(parts) < 2:
            return {}, "", "请提供提示词，例如：/draw 4:3 2k medium 一只狗"

        tokens = parts[1].strip().split()
        normalized_tokens = []
        reading_params = True
        for token in tokens:
            value = token.lower()
            if reading_params and "@" in value:
                pieces = [piece for piece in value.split("@") if piece]
                if len(pieces) > 1 and all(
                    self._looks_like_draw_param_token(piece) for piece in pieces
                ):
                    normalized_tokens.extend(pieces)
                    continue

            if reading_params and self._looks_like_draw_param_token(value):
                normalized_tokens.append(value)
                continue

            reading_params = False
            normalized_tokens.append(token)

        return self._parse_draw_args(" ".join(normalized_tokens))

    def _validate_params(self, params: dict) -> tuple[bool, str]:
        """验证参数是否合法，返回 (valid, error_msg)"""
        size = params.get("size", self.default_size)
        resolution = params.get("resolution", self.default_resolution)
        quality = params.get("quality", self.default_quality)

        if size not in VALID_SIZES:
            return (
                False,
                f"无效的宽高比：{size}，支持：{', '.join(sorted(VALID_SIZES))}",
            )

        if resolution not in VALID_RESOLUTIONS:
            return (
                False,
                f"无效的分辨率：{resolution}，支持：{', '.join(VALID_RESOLUTIONS)}",
            )

        if quality not in VALID_QUALITIES:
            return False, f"无效的质量：{quality}，支持：{', '.join(VALID_QUALITIES)}"

        if resolution == "4k" and size not in SIZES_SUPPORT_4K:
            supported = ", ".join(sorted(SIZES_SUPPORT_4K))
            return False, f"4K 仅支持以下比例：{supported}"

        return True, ""

    # ========== 权限检查 ==========

    async def _check_permission(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """检查用户权限，返回 (allowed, reason)"""
        try:
            sender_id = str(event.get_sender_id())
        except Exception as e:
            logger.error(f"获取发送者ID失败: {e}")
            return False, "内部错误：无法获取用户ID"

        # 白名单检查
        if self.whitelist:
            if sender_id not in [str(uid) for uid in self.whitelist]:
                return False, "你没有使用该功能的权限（不在白名单中）"

        # 每日次数检查
        if self.daily_limit > 0:
            try:
                from datetime import datetime

                today = datetime.now().strftime("%Y-%m-%d")
                key = f"usage_{sender_id}_{today}"
                count = await self.get_kv_data(key, default=0)
                if count >= self.daily_limit:
                    return (
                        False,
                        f"你今天的使用次数已用完（{count}/{self.daily_limit}）",
                    )
            except Exception as e:
                logger.error(f"读取每日次数失败: {e}")

        return True, ""

    async def _inc_usage(self, sender_id: str):
        """增加使用次数"""
        try:
            from datetime import datetime

            today = datetime.now().strftime("%Y-%m-%d")
            key = f"usage_{sender_id}_{today}"
            count = await self.get_kv_data(key, default=0)
            await self.put_kv_data(key, count + 1)
        except Exception as e:
            logger.error(f"增加使用次数失败: {e}")

    # ========== API 调用 ==========

    async def _submit_task(
        self,
        prompt: str,
        params: dict,
        image_urls: list[str] | None = None,
    ) -> dict:
        """提交图片生成任务，返回 API 响应"""
        payload = {
            "model": "gpt-image-2",
            "prompt": prompt,
            "size": params.get("size", self.default_size),
            "resolution": params.get("resolution", self.default_resolution),
            "quality": params.get("quality", self.default_quality),
            "n": 1,
        }

        if image_urls:
            payload["image_urls"] = image_urls

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with self.session.post(
                f"{self.api_url}/images/generations",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return {"error": f"API 返回 {resp.status}: {text[:200]}"}
                response_json = await resp.json()
                logger.info(
                    f"API 提交返回: {json.dumps(response_json, ensure_ascii=False)}"
                )
                return response_json
        except Exception as e:
            logger.error(f"提交任务网络异常: {e}")
            return {"error": f"网络连接失败: {e}"}

    async def _download_reference_image(
        self, url: str, index: int
    ) -> tuple[bytes, str, str]:
        """下载引用图片，返回 (content, filename, content_type)。"""
        try:
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ValueError(f"下载引用图片失败：HTTP {resp.status}: {body[:200]}")

                content = await resp.read()
                if not content:
                    raise ValueError("下载引用图片失败：响应内容为空")
                if len(content) > MAX_REFERENCE_IMAGE_BYTES:
                    size_mb = len(content) / 1024 / 1024
                    raise ValueError(f"引用图片过大：{size_mb:.1f}MB，最大支持 20MB")

                content_type = self._detect_image_content_type(
                    resp.headers.get("Content-Type", ""),
                    content,
                )
                if content_type not in IMAGE_CONTENT_TYPE_EXTENSIONS:
                    raise ValueError(
                        f"引用图片格式不支持：{content_type or '未知'}，"
                        "请使用 JPEG、PNG、WebP 或 GIF"
                    )

                return self._normalize_reference_image(content, content_type, index)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"下载引用图片失败：{e}") from e

    def _detect_image_content_type(self, header: str, content: bytes) -> str:
        """优先使用响应头，缺失或不规范时按文件头识别图片格式。"""
        content_type = header.split(";")[0].strip().lower()
        if content_type in IMAGE_CONTENT_TYPE_EXTENSIONS:
            return "image/jpeg" if content_type == "image/jpg" else content_type

        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"

        return content_type

    def _normalize_reference_image(
        self, content: bytes, content_type: str, index: int
    ) -> tuple[bytes, str, str]:
        """将上游不支持的 GIF 转成首帧 PNG，其余格式保持原样。"""
        if content_type != "image/gif":
            filename = f"reference_{index}{IMAGE_CONTENT_TYPE_EXTENSIONS[content_type]}"
            return content, filename, content_type

        try:
            from PIL import Image

            with Image.open(io.BytesIO(content)) as image:
                image.seek(0)
                frame = image.copy()

            if frame.mode not in ("RGB", "RGBA"):
                frame = frame.convert("RGBA")

            output = io.BytesIO()
            frame.save(output, format="PNG")
            converted = output.getvalue()
        except Exception as e:
            raise ValueError(f"GIF 首帧转换失败：{e}") from e

        if len(converted) > MAX_REFERENCE_IMAGE_BYTES:
            size_mb = len(converted) / 1024 / 1024
            raise ValueError(f"GIF 首帧转换后过大：{size_mb:.1f}MB，最大支持 20MB")

        logger.info("检测到 GIF 引用图片，已截取第一帧并转换为 PNG")
        return converted, f"reference_{index}.png", "image/png"

    def _to_data_uri(self, content: bytes, content_type: str) -> str:
        """将引用图片编码成 JSON 可传递的 data URI。"""
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"

    async def _prepare_reference_images(self, image_urls: list[str]) -> list[str]:
        """将平台引用图转换成 data URI，避免生成接口收到 multipart 请求。"""
        data_urls = []
        for index, url in enumerate(image_urls, start=1):
            content, filename, content_type = await self._download_reference_image(
                url, index
            )
            data_urls.append(self._to_data_uri(content, content_type))
            logger.info(f"引用图片已转换为 data URI: {filename}")

        return data_urls

    async def _create_generation_task(
        self,
        event: AstrMessageEvent,
        prompt: str,
        params: dict | None = None,
        use_quoted_image: bool = True,
    ) -> str:
        """创建生成任务并启动后台轮询，返回给用户展示的提交结果。"""
        prompt = (prompt or "").strip()
        if not prompt:
            return "请提供提示词，例如：/draw 4:3 2k medium 一只狗"

        params = params or {}

        allowed, reason = await self._check_permission(event)
        if not allowed:
            return reason

        valid, err_msg = self._validate_params(params)
        if not valid:
            return err_msg

        final_params = {
            "size": params.get("size", self.default_size),
            "resolution": params.get("resolution", self.default_resolution),
            "quality": params.get("quality", self.default_quality),
        }

        image_urls = []
        if use_quoted_image:
            image_urls = await self._get_quoted_image_urls(event)
            if image_urls:
                try:
                    image_urls = await self._prepare_reference_images(image_urls)
                except ValueError as e:
                    logger.error(f"处理引用图片失败: {e}")
                    return str(e)

        sender_id = str(event.get_sender_id())

        try:
            submit_resp = await self._submit_task(prompt, final_params, image_urls)
        except Exception as e:
            logger.error(f"提交任务异常: {e}")
            return "提交生成任务时发生内部错误。"

        if "error" in submit_resp:
            return f"提交任务失败：{submit_resp['error']}"

        task_id = None
        if submit_resp.get("code") == 200:
            data_list = submit_resp.get("data", [])
            if isinstance(data_list, list) and data_list:
                task_id = data_list[0].get("task_id")

        if not task_id:
            return "API 未返回 task_id，请检查 API 配置。"

        await self._inc_usage(sender_id)

        task_info = {
            "task_id": task_id,
            "sender_id": sender_id,
            "umo": event.unified_msg_origin,
            "prompt": prompt,
            "params": final_params,
            "has_reference": bool(image_urls),
            "status": "submitted",
            "created_at": time.time(),
        }
        await self.put_kv_data(f"task_{task_id}", json.dumps(task_info))

        mode_text = "图生图" if image_urls else "文生图"
        asyncio.create_task(self._background_polling(task_id))
        return (
            f"🎨 {mode_text}任务已提交\n"
            f"任务 ID：{task_id}\n"
            f"参数：{final_params['size']} "
            f"{final_params['resolution']} {final_params['quality']}"
        )

    async def _poll_task(self, task_id: str) -> dict:
        """轮询任务状态，返回任务结果或错误"""
        headers = {"Authorization": f"Bearer {self.api_key}"}

        # 首次查询延迟
        await asyncio.sleep(10)

        for i in range(self.max_retries):
            try:
                async with self.session.get(
                    f"{self.api_url}/tasks/{task_id}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(
                            f"轮询 {task_id} 状态码 {resp.status}: {text[:200]}"
                        )
                        return {"error": f"轮询失败：{resp.status}"}

                    full_data = await resp.json()
                    logger.debug(
                        f"轮询返回: {json.dumps(full_data, ensure_ascii=False)}"
                    )

                    if full_data.get("code") == 200 and "data" in full_data:
                        task_data = full_data["data"]
                    else:
                        task_data = full_data

                    status = task_data.get("status")
                    progress = task_data.get("progress", 0)
                    logger.info(f"任务 {task_id} 状态: {status}, 进度: {progress}%")

                    if status == "completed" and progress == 100:
                        return task_data
                    elif status == "failed":
                        return {"error": "任务生成失败"}
            except Exception as e:
                logger.error(f"轮询异常: {e}")

            if i < self.max_retries - 1:
                await asyncio.sleep(self.poll_interval)

        return {"error": f"任务超时（已轮询 {self.max_retries} 次）"}

    async def _download_image(self, url: str, task_id: str) -> str:
        """下载图片到本地，返回文件路径"""
        from pathlib import Path
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        img_dir = (
            Path(get_astrbot_data_path()) / "plugin_data" / "gpt-image-2" / "images"
        )
        img_dir.mkdir(parents=True, exist_ok=True)
        file_path = img_dir / f"{task_id}.png"

        try:
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {body[:200]}")
                content = await resp.read()
                if not content:
                    raise Exception("响应内容为空")
                with open(file_path, "wb") as f:
                    f.write(content)

            logger.info(f"图片下载成功: {file_path}，大小: {len(content)} bytes")
            return str(file_path)
        except Exception:
            logger.error(f"下载失败: {url}\n{traceback.format_exc()}")
            raise

    async def _send_image(self, umo: str, text: str, image_path: str):
        """发送本地图片"""
        try:
            chain = MessageChain().message(text).file_image(image_path)
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.error(f"发送图片失败: {e}")
            await self.context.send_message(
                umo, MessageChain().message(f"图片发送失败：{e}")
            )

    def _clean_old_images(self, keep: int = 20):
        """清理旧图片缓存"""
        try:
            from pathlib import Path
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            img_dir = (
                Path(get_astrbot_data_path()) / "plugin_data" / "gpt-image-2" / "images"
            )
            if not img_dir.exists():
                return

            files = sorted(
                img_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            for old_file in files[keep:]:
                old_file.unlink()
                logger.info(f"清理旧图片: {old_file}")
        except Exception as e:
            logger.error(f"清理图片缓存失败: {e}")

    # ========== 后台轮询 ==========

    async def _background_polling(self, task_id: str):
        """后台轮询任务状态，完成后发送图片"""
        raw = await self.get_kv_data(f"task_{task_id}", default=None)
        if not raw:
            logger.error(f"后台轮询找不到任务 {task_id}")
            return

        try:
            task_info = json.loads(raw)
        except Exception as e:
            logger.error(f"任务信息 JSON 解析失败: {e}")
            return

        umo = task_info.get("umo")
        if not umo:
            logger.error("任务信息缺少统一会话标识")
            return

        task_info["status"] = "processing"
        await self.put_kv_data(f"task_{task_id}", json.dumps(task_info))

        poll_result = await self._poll_task(task_id)

        if "error" in poll_result:
            task_info["status"] = "failed"
            await self.put_kv_data(f"task_{task_id}", json.dumps(task_info))
            await self.context.send_message(
                umo, MessageChain().message(f"图片生成失败：{poll_result['error']}")
            )
            return

        try:
            images = poll_result.get("result", {}).get("images", [])
            if not images:
                raise ValueError("轮询结果中没有 images 字段")
            first_image = images[0]
            url_list = first_image.get("url", [])
            if not url_list:
                raise ValueError("图片信息中没有 url 字段")
            image_url = url_list[0]
            if not image_url:
                raise ValueError("图片 URL 为空")
            logger.info(f"准备下载图片，URL: {image_url}")
        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.error(
                f"任务 {task_id} 图片 URL 解析失败: {e}，"
                f"完整轮询结果: {json.dumps(poll_result, ensure_ascii=False)}"
            )
            await self.context.send_message(
                umo, MessageChain().message("图片生成完成，但获取下载链接失败。")
            )
            return

        try:
            local_path = await self._download_image(image_url, task_id)
        except Exception as e:
            await self.context.send_message(
                umo, MessageChain().message(f"图片下载失败：{e}")
            )
            return

        try:
            await self._send_image(umo, "生成完成！", local_path)
            task_info["status"] = "completed"
            await self.put_kv_data(f"task_{task_id}", json.dumps(task_info))
        except Exception as e:
            logger.error(f"发送图片失败: {e}")
            task_info["status"] = "failed"
            await self.put_kv_data(f"task_{task_id}", json.dumps(task_info))
            await self.context.send_message(
                umo, MessageChain().message("图片生成成功但发送失败，请联系管理员。")
            )
            return

        self._clean_old_images(keep=20)

    # ========== 获取引用图片 ==========

    async def _get_quoted_image_urls(self, event: AstrMessageEvent) -> list[str]:
        """从被引用的消息中提取图片 URL 列表"""
        try:
            chain = event.get_messages()
            if not chain:
                return []

            from astrbot.core.message.components import Reply, Image as MsgImage

            reply = chain[0] if isinstance(chain[0], Reply) else None
            if not reply or not reply.chain:
                return []

            image_urls = []
            for seg in reply.chain:
                if isinstance(seg, MsgImage) and seg.url:
                    image_urls.append(seg.url)

            return image_urls
        except Exception as e:
            logger.error(f"获取引用图片失败: {e}")
            return []

    # ========== LLM Tool 与兜底 ==========

    def _coerce_bool(self, value) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in {
                "false",
                "0",
                "no",
                "off",
                "否",
                "不",
            }
        return bool(value)

    async def _invoke_generate_image_tool(
        self,
        event: AstrMessageEvent,
        prompt: str,
        size: str = "",
        resolution: str = "",
        quality: str = "",
        use_quoted_image: bool = True,
    ) -> str:
        params = {}
        if size:
            params["size"] = str(size).strip().lower()
        if resolution:
            params["resolution"] = str(resolution).strip().lower()
        if quality:
            params["quality"] = str(quality).strip().lower()

        use_quoted_image = self._coerce_bool(use_quoted_image)
        logger.info(
            "generate_image LLM Tool 被调用: "
            f"size={params.get('size', self.default_size)}, "
            f"resolution={params.get('resolution', self.default_resolution)}, "
            f"quality={params.get('quality', self.default_quality)}, "
            f"use_quoted_image={use_quoted_image}"
        )

        return await self._create_generation_task(
            event,
            prompt,
            params,
            use_quoted_image=use_quoted_image,
        )

    def _get_llm_response_text(self, response) -> str:
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            return str(response.get("completion_text") or response.get("content") or "")

        for attr in ("completion_text", "content", "text"):
            value = getattr(response, attr, None)
            if isinstance(value, str):
                return value

        return ""

    def _set_llm_response_text(self, response, text: str):
        if isinstance(response, dict):
            response["completion_text"] = text
            response["content"] = text
            return

        for attr in ("completion_text", "content", "text"):
            if hasattr(response, attr):
                try:
                    setattr(response, attr, text)
                except Exception as e:
                    logger.debug(f"更新 LLMResponse.{attr} 失败: {e}")

    def _resolve_llm_response_hook_args(self, args: tuple, kwargs: dict):
        event = kwargs.get("event")
        response = kwargs.get("response") or kwargs.get("llm_response")

        for arg in args:
            if event is None and hasattr(arg, "get_sender_id"):
                event = arg
                continue
            if response is None and (
                isinstance(arg, dict)
                or isinstance(arg, str)
                or any(hasattr(arg, attr) for attr in ("completion_text", "content", "text"))
            ):
                response = arg

        return event, response

    @filter.on_llm_response()
    async def on_llm_response(self, *args, **kwargs):
        event, response = self._resolve_llm_response_hook_args(args, kwargs)
        if event is None or response is None:
            logger.debug("LLM /draw 兜底跳过：无法解析 hook 参数")
            return

        text = self._get_llm_response_text(response).strip()
        if not re.match(r"^/draw(?:\s+|$)", text, flags=re.IGNORECASE):
            return

        params, prompt, parse_error = self._parse_llm_draw_response(text)
        if parse_error:
            self._set_llm_response_text(response, parse_error)
            return

        logger.info("检测到 LLM 输出 /draw 文本，已拦截并转为内部生图任务")
        result = await self._create_generation_task(event, prompt, params)
        self._set_llm_response_text(response, result)

    # ========== 命令 ==========

    @filter.command("draw", priority=1)
    async def draw(self, event: AstrMessageEvent):
        """
        /draw [参数] <提示词>  生成图片（支持图生图）

        参数格式：
        - 16:9 4k low
        - 3:4 medium
        - 1:1 high
        - 2k low
        - low（仅质量）
        - 16:9（仅比例）

        图生图：引用一张图片后发送 /draw <提示词>
        """
        event.stop_event()

        parts = event.message_str.strip().split(None, 1)
        if len(parts) < 2:
            yield event.plain_result(
                "格式：/draw [参数] <提示词>\n"
                "参数示例：16:9 4k low、3:4 medium、1:1 high\n"
                "图生图：引用一张图片后发送 /draw <提示词>"
            )
            return

        remaining = parts[1]

        # 解析空格分隔的前置参数；第一个非参数 token 开始作为提示词。
        params, prompt, parse_error = self._parse_draw_args(remaining)
        if parse_error:
            yield event.plain_result(parse_error)
            return

        result = await self._create_generation_task(event, prompt, params)
        yield event.plain_result(result)

    @filter.command("drawhelp", priority=1)
    async def drawhelp(self, event: AstrMessageEvent):
        """显示 GPT-Image-2 使用教程"""
        event.stop_event()
        yield event.plain_result(
            "🎨 GPT-Image-2 使用教程\n"
            "\n"
            "━━━ 基本用法 ━━━\n"
            "/draw <提示词>\n"
            "  例：/draw 一只在月球漫步的柴犬\n"
            "\n"
            "━━━ 参数说明 ━━━\n"
            "格式：/draw [参数] <提示词>\n"
            "\n"
            "宽高比：1:1 3:4 4:3 4:5 5:4 16:9 9:16 2:3 3:2 21:9 9:21 1:2 2:1\n"
            "分辨率：1k 2k 4k（4K 仅支持 16:9 等部分比例）\n"
            "质量：auto low medium high\n"
            "\n"
            "参数组合示例：\n"
            "  /draw 16:9 赛博朋克夜景\n"
            "  /draw 3:4 2k medium 一位少女\n"
            "  /draw 21:9 4k high 电影海报\n"
            "\n"
            "━━━ 图生图 ━━━\n"
            "引用一张图片后发送：\n"
            "  /draw <提示词>\n"
            "  例：[引用图片] /draw 转换为动漫风格"
        )
