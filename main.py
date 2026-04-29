import asyncio
import json
import os
import time
import traceback
import aiohttp
import random
from datetime import datetime
from pathlib import Path
from io import BytesIO

from PIL import Image as PILImage
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image
from astrbot.core.message.message_event_result import MessageChain

@register("gpt-image2", "Luochang", "能够让AstrBot调用GPT image 2 来生成图片并通过异步获取图片的插件。", "1.0.0")
class GPTImage2Plugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

        # 获取数据根目录（兼容不同环境）
        data_root = Path.cwd() / "data"  # AstrBot 默认工作目录即为项目根目录
        logger.info(f"数据根目录: {data_root}")

        # 配置文件路径：使用框架标准插件配置存储位置
        config_path = data_root / "plugin_config" / "gpt-image2.json"

        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8-sig") as f:
                    cfg = json.load(f)
                logger.info(f"✅ 配置文件加载成功: {config_path}")
            except Exception as e:
                logger.error(f"❌ 配置文件读取失败: {e}")
                cfg = {}
        else:
            logger.warning(f"⚠️ 配置文件不存在: {config_path}，使用默认配置")
            cfg = {}

        # 图片存储目录（绝对路径）
        self.img_dir = data_root / "plugin_data" / "gpt-image2" / "images"
        self.img_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"✅ 图片目录已准备: {self.img_dir}")

        # 配置字段
        self.api_url = cfg.get("api_url", "https://api.apimart.ai/v1").rstrip("/")
        self.api_key = cfg.get("api_key", "")
        self.whitelist = cfg.get("whitelist", [])
        self.daily_limit = cfg.get("daily_limit", 10)
        self.max_retries = cfg.get("max_retries", 10)          # 默认10次
        self.poll_interval = cfg.get("poll_interval", 2)       # 默认2秒
        self.default_preset = cfg.get("default_preset", "default")
        self.presets = cfg.get("presets", [])

        # 诊断输出
        safe_cfg = {k: v for k, v in cfg.items() if k != 'api_key'}
        logger.info(f"📋 配置摘要 (无api_key): {json.dumps(safe_cfg, ensure_ascii=False)}")
        logger.info(f"🔍 presets 类型: {type(self.presets).__name__}, 内容: {json.dumps(self.presets, ensure_ascii=False)}")

    # ---------- 工具函数 ----------
    async def _check_permission(self, event: AstrMessageEvent) -> tuple[bool, str]:
        try:
            sender_id = str(event.get_sender_id())
        except Exception as e:
            logger.error(f"获取发送者ID失败: {e}")
            return False, "内部错误：无法获取用户ID"

        if self.whitelist:
            if sender_id not in [str(uid) for uid in self.whitelist]:
                return False, "你没有使用该功能的权限（不在白名单中）"

        try:
            today = datetime.now().strftime("%Y-%m-%d")
            key = f"usage_{sender_id}_{today}"
            count = await self.get_kv_data(key, default=0)
            if self.daily_limit > 0 and count >= self.daily_limit:
                return False, f"你今天的使用次数已用完（{count}/{self.daily_limit}）"
        except Exception as e:
            logger.error(f"读取每日次数失败: {e}")
        return True, ""

    async def _inc_usage(self, sender_id: str):
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            key = f"usage_{sender_id}_{today}"
            count = await self.get_kv_data(key, default=0)
            await self.put_kv_data(key, count + 1)
        except Exception as e:
            logger.error(f"增加使用次数失败: {e}")

    def _find_preset(self, preset_id: str):
        for p in self.presets:
            if p.get("id") == preset_id:
                return p
        return None

    async def _submit_task(self, preset: dict, prompt: str) -> dict:
        """提交任务，返回 API 响应 JSON 或含 error 字段的 dict"""
        # 1. 处理图片尺寸（支持分类标题随机）
        size_raw = preset.get("size", "1:1")
        SIZE_CATEGORIES = {
            "---- 方形 ----": ["1:1"],
            "---- 横屏 ----": ["16:9", "3:2", "4:3", "2:1", "21:9", "5:4"],
            "---- 竖屏 ----": ["9:16", "2:3", "3:4", "1:2", "9:21", "4:5"]
        }
        if size_raw in SIZE_CATEGORIES:
            size = random.choice(SIZE_CATEGORIES[size_raw])
            logger.info(f"用户选择了分类 '{size_raw}'，随机抽取比例：{size}")
        else:
            size = size_raw

        # 2. 构建请求体
        payload = {
            "model": "gpt-image-2",
            "prompt": prompt,
            "size": size,
            "resolution": preset.get("resolution", "2k"),
            "n": preset.get("n", 1),
        }

        # 3. 合并自定义参数
        custom_str = preset.get("custom", "").strip()
        if custom_str:
            try:
                custom_obj = json.loads(custom_str)
                payload.update(custom_obj)
            except json.JSONDecodeError as e:
                return {"error": f"自定义参数 JSON 解析失败：{e}"}
            except Exception as e:
                return {"error": f"自定义参数解析异常：{e}"}

        # 4. 发送 API 请求
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.api_url}/images/generations",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        return {"error": f"API 返回 {resp.status}: {text[:200]}"}
                    response_json = await resp.json()
                    logger.info(f"=== API 提交返回 == {json.dumps(response_json, ensure_ascii=False)}")

                    # 提取 task_id
                    if response_json.get("code") == 200:
                        data_list = response_json.get("data", [])
                        if isinstance(data_list, list) and data_list:
                            task_id = data_list[0].get("task_id")
                            if task_id:
                                response_json["task_id"] = task_id
                    return response_json
        except Exception as e:
            logger.error(f"提交任务网络异常: {e}")
            return {"error": f"网络连接失败: {str(e)}"}

    async def _poll_task(self, task_id: str) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for i in range(self.max_retries):
            await asyncio.sleep(self.poll_interval)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{self.api_url}/tasks/{task_id}",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        if resp.status != 200:
                            text = await resp.text()
                            logger.error(f"轮询 {task_id} 状态码 {resp.status}: {text[:200]}")
                            return {"error": f"轮询失败：{resp.status}"}
                        full_data = await resp.json()
                        logger.debug(f"轮询返回: {json.dumps(full_data, ensure_ascii=False)}")

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
                continue
        return {"error": f"任务超时（已轮询 {self.max_retries} 次）"}

    async def _download_image(self, url: str, task_id: str) -> str:
        file_path = self.img_dir / f"{task_id}.png"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        raise Exception(f"下载失败，状态码 {resp.status}")
                    with open(file_path, "wb") as f:
                        f.write(await resp.read())
            return str(file_path.absolute())
        except Exception as e:
            logger.error(f"下载图片失败: {e}")
            raise

    def _clean_old_images(self, keep=20):
        try:
            files = sorted(self.img_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
            if len(files) > keep:
                for old_file in files[keep:]:
                    old_file.unlink()
                    logger.info(f"清理旧图片: {old_file}")
        except Exception as e:
            logger.error(f"清理图片缓存失败: {e}")

    async def _send_image(self, umo: str, text: str, image_path: str):
        """跨平台发送图片消息，自动格式适配（JPEG、大小限制）"""
        try:
            img = PILImage.open(image_path)
            if img.mode == 'RGBA':
                background = PILImage.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3])
                img = background
            else:
                img = img.convert('RGB')

            img.thumbnail((1024, 1024), PILImage.Resampling.LANCZOS)

            buf = BytesIO()
            img.save(buf, format='JPEG', quality=85)
            buf.seek(0)
            if len(buf.getvalue()) > 2 * 1024 * 1024:  # 2MB 限制
                buf = BytesIO()
                img.save(buf, format='JPEG', quality=50)
                buf.seek(0)

            temp_path = str(self.img_dir / f"{Path(image_path).stem}_send.jpg")
            with open(temp_path, 'wb') as f:
                f.write(buf.getvalue())

            chain = MessageChain().message(text).file_image(temp_path)
            await self.context.send_message(umo, chain)

            os.remove(temp_path)
        except Exception as e:
            logger.error(f"发送图片失败: {e}")
            await self.context.send_message(umo, MessageChain().message(f"图片发送失败：{str(e)}"))

    # ---------- 后台轮询 ----------
    async def _background_polling(self, task_id: str):
        await asyncio.sleep(1)

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
            await self.context.send_message(umo, MessageChain().message(f"图片生成失败：{poll_result['error']}"))
            return

        try:
            image_url = poll_result["result"]["images"][0]["url"][0]
        except (KeyError, IndexError, TypeError):
            logger.error(f"任务 {task_id} 图片 URL 解析失败")
            await self.context.send_message(umo, MessageChain().message("图片生成完成，但获取下载链接失败。"))
            return

        try:
            local_path = await self._download_image(image_url, task_id)
        except Exception as e:
            await self.context.send_message(umo, MessageChain().message(f"图片下载失败：{str(e)}"))
            return

        try:
            await self._send_image(umo, "生成完成！", local_path)
            task_info["status"] = "completed"
            await self.put_kv_data(f"task_{task_id}", json.dumps(task_info))
        except Exception as e:
            logger.error(f"发送图片失败: {e}")
            task_info["status"] = "failed"
            await self.put_kv_data(f"task_{task_id}", json.dumps(task_info))
            await self.context.send_message(umo, MessageChain().message("图片生成成功但发送失败，请联系管理员。"))
            return

        self._clean_old_images(keep=20)

    # ---------- 命令 ----------
    @filter.command("draw")
    async def draw(self, event: AstrMessageEvent):
        '''/draw [预设ID] <提示词>  生成图片'''
        logger.info(f"🔎 [DEBUG] self.presets = {json.dumps(self.presets, ensure_ascii=False)}, type = {type(self.presets).__name__}")

        # 权限检查
        try:
            allowed, reason = await self._check_permission(event)
            if not allowed:
                await event.send(MessageChain().message(reason))
                return
        except Exception as e:
            logger.error(f"权限检查异常: {e}")
            await event.send(MessageChain().message("内部错误：权限检查失败，请稍后重试。"))
            return

        # 参数解析
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            await event.send(MessageChain().message("格式：/draw [预设ID] <提示词>"))
            return

        if len(parts) >= 3:
            preset_id = parts[1]
            prompt = " ".join(parts[2:])
        else:
            preset_id = self.default_preset
            prompt = " ".join(parts[1:])

        preset = self._find_preset(preset_id)
        if not preset:
            preset_list = ', '.join([p.get('id', '?') for p in self.presets]) if self.presets else "无"
            await event.send(MessageChain().message(f"未找到预设 '{preset_id}'，当前可用预设：{preset_list}"))
            return

        sender_id = str(event.get_sender_id())

        # 提交任务
        try:
            submit_resp = await self._submit_task(preset, prompt)
        except Exception as e:
            logger.error(f"提交任务异常: {e}")
            await event.send(MessageChain().message("提交生成任务时发生内部错误。"))
            return

        if "error" in submit_resp:
            await event.send(MessageChain().message(f"提交任务失败：{submit_resp['error']}"))
            return

        task_id = submit_resp.get("task_id")
        if not task_id:
            await event.send(MessageChain().message("API 未返回 task_id，请检查 API 配置。"))
            return

        # 记录使用次数
        try:
            await self._inc_usage(sender_id)
        except Exception as e:
            logger.error(f"增加次数失败: {e}")

        # 保存任务信息
        task_info = {
            "task_id": task_id,
            "sender_id": sender_id,
            "umo": event.unified_msg_origin,
            "preset_id": preset_id,
            "prompt": prompt,
            "status": "submitted",
            "created_at": time.time()
        }
        try:
            await self.put_kv_data(f"task_{task_id}", json.dumps(task_info))
        except Exception as e:
            logger.error(f"保存任务信息失败: {e}")

        # 回复预设消息
        reply_template = preset.get("reply", "图库搜寻中… 任务 ID：{task_id}")
        try:
            reply_text = reply_template.format(task_id=task_id, prompt=prompt)
        except Exception:
            reply_text = f"任务已提交，ID: {task_id}"
        await event.send(MessageChain().message(reply_text))

        # 启动后台轮询
        asyncio.create_task(self._background_polling(task_id))

    @filter.command("check")
    async def check_task(self, event: AstrMessageEvent):
        try:
            parts = event.message_str.strip().split()
            if len(parts) < 2:
                yield event.plain_result("格式：/check <任务ID>")
                return
            task_id = parts[1]

            raw = await self.get_kv_data(f"task_{task_id}", default=None)
            if not raw:
                yield event.plain_result("任务不存在或已过期。")
                return

            task_info = json.loads(raw)
            if task_info.get("sender_id") != str(event.get_sender_id()):
                yield event.plain_result("你只能查询自己的任务。")
                return

            status = task_info.get("status", "unknown")
            if status == "completed":
                local_file = self.img_dir / f"{task_id}.png"
                if local_file.exists():
                    chain = [Plain("该任务已生成图片："), Image.fromFileSystem(str(local_file))]
                    yield event.chain_result(chain)
                else:
                    yield event.plain_result("任务已完成，但图片文件丢失。")
            elif status == "failed":
                yield event.plain_result("该任务生成失败。")
            else:
                headers = {"Authorization": f"Bearer {self.api_key}"}
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"{self.api_url}/tasks/{task_id}",
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=10)
                        ) as resp:
                            if resp.status == 200:
                                full_data = await resp.json()
                                if full_data.get("code") == 200 and "data" in full_data:
                                    task_data = full_data["data"]
                                else:
                                    task_data = full_data
                                progress = task_data.get("progress", 0)
                                status_text = task_data.get("status", "unknown")
                                yield event.plain_result(f"任务状态：{status_text}，进度：{progress}%")
                            else:
                                yield event.plain_result("查询任务状态失败，请稍后重试。")
                except Exception as e:
                    logger.error(f"查询任务状态网络错误: {e}")
                    yield event.plain_result(f"查询任务状态时网络错误: {str(e)}")
        except Exception as e:
            logger.error(f"check 命令异常: {e}")
            yield event.plain_result("查询任务时发生内部错误。")

    async def terminate(self):
        pass