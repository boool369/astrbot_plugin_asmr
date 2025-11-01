import re
import os
import random
import aiohttp
import asyncio
import aiofiles
from typing import Dict, Any, List, Tuple
from tqdm import tqdm
from pathlib import Path
from math import ceil

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.session_waiter import (
    session_waiter,
    SessionController,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.api import logger

# --- 全局常量（不变） ---

BASE_URLS = [
    "https://api.asmr.one",
    "https://api.asmr-100.com",
    "https://api.asmr-200.com",
    "https://api.asmr-300.com"
]
RJ_RE = re.compile(r"(?:RJ)?(?P<id>\d+)", re.IGNORECASE)

# --- 辅助函数：文件处理和格式化 ---

def format_size(size_bytes: int) -> str:
    """将字节数格式化为可读的字符串"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"

def recursively_transform_data(data: List[Dict[str, Any]], all_files: List[Dict[str, Any]], current_folder_path: List[str]):
    """
    【优化 1.1】递归遍历 API 返回的 JSON 结构，
    只收集音频文件，并将所有文件的 full_folder_path 设置为空，忽略原始文件夹结构。
    """
    for item in data:
        item_type = item.get("type")
        item_title = item.get("title")

        if item_type == "folder":
            new_path = current_folder_path + [item_title]
            if "children" in item and item["children"] is not None:
                recursively_transform_data(item["children"], all_files, new_path)
        elif item_type == "audio": # 【优化 3.1】只收集音频文件
            file_info = {
                "title": item_title,
                "url": item.get("mediaDownloadUrl"),
                "type": item_type,
                "size": item.get("size", 0),
                "full_folder_path": "", # 【优化 1.1】强制置空，忽略子文件夹路径
            }
            all_files.append(file_info)
        # 忽略 "text" 和 "image" 类型的文件

# --- ASMR 机器人插件类 ---

@register(
    "astrbot_plugin_asmr",
    "boool369",
    "ASMR音声搜索、播放与下载",
    "3.4", # 最终配置兼容版本
    "https://github.com/boool369/astrbot_plugin_asmr"
)
class AsmrPlugin(Star):
    
    # 核心修正：添加 get_plugin_config_template 方法
    @staticmethod
    def get_plugin_config_template() -> Dict[str, Any]:
        """定义插件的配置模板，让 astrbot 框架知道插件支持哪些配置项"""
        return {
            "enable_nsfw": {
                "description": "是否启用nsfw搜索结果",
                "hint": "开启后在搜索时显示R18/NSFW结果",
                "type": "bool",
                "default": True
            },
            "download_base_dir": {
                "description": "下载文件的根目录相对路径",
                "hint": "文件将保存在此路径下。此路径相对于astrbot根目录。",
                "type": "str",
                "default": "Downloads/ASMR_Files"
            },
            "max_concurrent_downloads": {
                "description": "最大并发下载线程数",
                "hint": "限制同时进行的文件下载数量，避免网络拥堵或资源耗尽。",
                "type": "int",
                "default": 3
            }
        }
        
    def __init__(self, context: Context, config: AstrBotConfig=None):
        super().__init__(context)
        self.timeout = 30
        self.base_urls = BASE_URLS
        self.current_api_index = 0
        self.plugin_dir = Path(__file__).parent
        self.template_path = self.plugin_dir / "md.html"
        
        # --- 读取配置项（现在配置一定会被正确加载或使用默认值）---
        # config 对象现在是经过框架处理的，包含了模板中定义的所有键。
        self.nsfw = config.get("enable_nsfw", True)
        self.download_base_dir = Path(config.get("download_base_dir", "Downloads/ASMR_Files"))
        self.max_concurrent_downloads = config.get("max_concurrent_downloads", 3)
        # ------------------
        
        logger.info(f"[ASMR Plugin V3.4] 初始化成功。NSFW:{self.nsfw}, 下载路径:{self.download_base_dir}, 并发:{self.max_concurrent_downloads}")

    async def rotate_api(self):
        """切换到下一个API端点"""
        self.current_api_index = (self.current_api_index + 1) % len(self.base_urls)
        logger.info(f"[ASMR API] 切换到API: {self.base_urls[self.current_api_index]}")

    def get_current_api(self):
        """获取当前API端点"""
        return self.base_urls[self.current_api_index]

    async def fetch_with_retry(self, url_path: str, params=None, max_retries=4):
        """带重试机制的API请求"""
        errors = []
        api_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
            "Origin": "https://asmr.one",
            "Referer": "https://asmr.one/",
            "Accept": "application/json"
        }
        
        async with aiohttp.ClientSession(headers=api_headers) as session:
            for attempt in range(max_retries):
                current_api = self.get_current_api()
                url = f"{current_api}{url_path}"
                try:
                    async with session.get(url, params=params, timeout=10) as response:
                        if response.status == 200:
                            return await response.json()
                        else:
                            errors.append(f"API {current_api} 返回状态码: {response.status}")
                            await self.rotate_api()
                except Exception as e:
                    errors.append(f"API {current_api} 请求失败: {type(e).__name__}: {str(e)}")
                    await self.rotate_api()
        
        error_msg = "[ASMR API Error] 所有API请求均失败:\n" + "\n".join(errors)
        logger.error(error_msg)
        return None

    # --- 命令：ASMR 帮助 (纯文本输出，确保兼容性) ---
    
    @filter.command("asmr帮助")
    async def asmr_help(self, event: AstrMessageEvent):
        """显示本ASMR插件的所有功能和用法示例。"""
        
        help_message = (
            "### 🎧 ASMR 音声插件功能 (V3.4 最终配置版)\n"
            "---"
            "**1. 🔍 搜索功能**\n"
            "   - **命令**: `搜音声 <关键词>/<标签> [页数]`\n"
            "   - **示例**: `搜音声 催眠/耳语 1`\n\n"
            "**2. ⏯️ 播放功能**\n"
            "   - **命令**: `听音声 <RJ号> [节目编号]`\n"
            "   - **示例**: `听音声 RJ0123456`\n"
            "**3. 🎲 随机播放**\n"
            "   - **命令**: `随机音声`\n\n"
            "**4. 💾 下载功能 (优化)**\n"
            "   - **命令**: `asmr下载 <RJ号>`\n"
            "   - **功能**: 启动交互式音频文件选择下载。**所有音频将保存在 RJ 文件夹根目录**。\n"
            "---"
            "当前配置:\n"
            f"   - NSFW 启用: {self.nsfw}\n"
            f"   - 下载根目录: {self.download_base_dir.as_posix()}\n"
            f"   - 并发数: {self.max_concurrent_downloads}\n"
        )
        
        yield event.plain_result(help_message)
            
    # --- 命令：搜音声 (未修改) ---
    
    @filter.command("搜音声")
    async def search_asmr(self, event: AstrMessageEvent):
        args = event.message_str.replace("搜音声", "").split()
        if not args:
            yield event.plain_result("请输入搜索关键词(用'/'分割不同tag)和搜索页数(可选)！比如'搜音声 伪娘/催眠 1'")
            return
        
        y = 1
        keyword = ""
        if len(args) == 1:
            keyword = args[0].replace("/", "%20")
        elif len(args) == 2:
            keyword = args[0].replace("/", "%20")
            try:
                y = int(args[1])
            except ValueError:
                yield event.plain_result("页数必须是数字")
                return
        else:
            yield event.plain_result("请正确输入搜索关键词(用'/'分割不同tag)和搜索页数(可选)！比如'搜音声 伪娘/催眠 1'")
            return

        yield event.plain_result(f"正在搜索音声`{keyword.replace('%20', ' / ')}`，第{y}页！")
        if not self.nsfw:
            keyword = keyword + "%20%24-age%3Aadult%24"
        try:
            r = await self.fetch_with_retry(
                f"/api/search/{keyword}",
                params={
                    "order": "dl_count",
                    "sort": "desc",
                    "page": y,
                    "subtitle": 0,
                    "includeTranslationWorks": "true"
                }
            )
            
            if r is None:
                yield event.plain_result("搜索音声失败，请稍后再试")
                return
            
            if len(r["works"]) == 0:
                if r["pagination"]["totalCount"] == 0:
                    yield event.plain_result("搜索结果为空")
                    return
                elif r["pagination"]["currentPage"] > 1:
                    count = int(r["pagination"]["totalCount"])
                    max_pages = ceil(count / 20)
                    yield event.plain_result(f"此搜索结果最多{max_pages}页")
                    return
            
            title, ars, imgs, rid = [], [], [], []
            for result2 in r["works"]:
                title.append(result2["title"])
                ars.append(result2["name"])
                imgs.append(result2["mainCoverUrl"])
                ids = str(result2["id"])
                ids = f"RJ{ids}" if not ids.startswith("RJ") else ids
                rid.append(ids)
            
            msg = ""
            for i in range(len(title)):
                msg += f"**{i + 1}.** 【{rid[i]}】 **{title[i]}** - {ars[i]}\n"
            
            msg += "\n请发送 `听音声+RJ号+节目编号（可选）` 来获取要听的资源"
            
            yield event.plain_result(f"### 🔍 搜索结果 (第 {r['pagination']['currentPage']} 页)\n" + msg)
            yield event.image_result(imgs[0])
            
        except Exception as e:
            logger.error(f"[Search Error] 搜索音声失败: {str(e)}")
            yield event.plain_result("搜索音声失败，请稍后再试")

    # --- 命令：听音声 (未修改) ---
    
    @filter.command("听音声")
    async def play_asmr(self, event: AstrMessageEvent):
        args = event.message_str.replace("听音声", "").split()
        
        if not args:
            yield event.plain_result("请输入RJ号！")
            return
        
        rj_match = RJ_RE.search(args[0])
        if not rj_match:
            yield event.plain_result("请输入正确的RJ号！")
            return
            
        rid = rj_match.group("id")
        selected_index = int(args[1]) - 1 if len(args) > 1 and args[1].isdigit() else None
        
        yield event.plain_result(f"正在查询音声信息！RJ{rid}")
        
        try:
            r = await self.fetch_with_retry(f"/api/workInfo/{rid}")
            
            if r is None or "title" not in r:
                yield event.plain_result("没有此音声信息或还没有资源")
                return
            if not self.nsfw and r["nsfw"]==True:
                yield event.plain_result("此音声为r18音声，管理员已禁止")
                return
            
            msg1,url,state=await self.get_asmr(event=event,rid=rid,r=r,selected_index=selected_index)
            
            if state == None:
                return
            
            yield event.image_result(url)
            yield event.plain_result(msg1)
            
            id = event.get_sender_id()
            @session_waiter(timeout=self.timeout, record_history_chains=False)
            async def track_waiter(controller: SessionController, ev: AstrMessageEvent):
                if ev.get_sender_id() != id:
                    return
                reply = ev.message_str.strip()
                if not reply.isdigit():
                    await event.send(event.plain_result("请发送正确的数字~"))
                    return
                
                index = int(reply) - 1
                if index < 0 or index >= len(state["keywords"]):
                    await event.send(event.plain_result("序号超出范围，请重新输入"))
                    return
                
                await self._play_track(ev, index, state["keywords"], state["urls"], 
                                       state["name"], state["ar"], state["iurl"], state["rid"])
                controller.stop()
            
            try:
                await track_waiter(event)
            except TimeoutError:
                yield event.plain_result("选择超时！")
        except Exception as e:
            logger.error(f"[Play Error] 播放音声失败: {str(e)}")
            yield event.plain_result("播放音声失败，请稍后再试")

    @filter.command("随机音声")
    async def play_Random_asmr(self, event: AstrMessageEvent):
        # ... (未修改) ...
        yield event.plain_result(f"正在随机抽取音声！")
        
        try:
            r = (await self.fetch_with_retry(f"/api/works?order=betterRandom"))["works"][0]
            
            if r is None or "title" not in r:
                yield event.plain_result("没有此音声信息或还没有资源")
                return
            if not self.nsfw:
                yield event.plain_result("管理员已开启禁止nsfw，此功能已禁止")
                return
            
            rid = str(r["id"])
            r_full = await self.fetch_with_retry(f"/api/workInfo/{rid}")
            if r_full is None:
                yield event.plain_result("获取随机音声详细信息失败")
                return
            r = r_full
            
            ids = str(r["id"])
            ids = f"RJ{ids}" if not ids.startswith("RJ") else ids
            rid = ids.replace("RJ", "")
            
            yield event.plain_result(f"抽取成功！**RJ号：{ids}**")
            
            msg1,url,state=await self.get_asmr(event=event,rid=rid,r=r)
            if state == None:
                return
            yield event.image_result(url)
            yield event.plain_result(msg1)
            
            id = event.get_sender_id()
            @session_waiter(timeout=self.timeout, record_history_chains=False)
            async def track_waiter(controller: SessionController, ev: AstrMessageEvent):
                if ev.get_sender_id() != id:
                    return
                reply = ev.message_str.strip()
                if not reply.isdigit():
                    await event.send(event.plain_result("请发送正确的数字~"))
                    return
                
                index = int(reply) - 1
                if index < 0 or index >= len(state["keywords"]):
                    await event.send(event.plain_result("序号超出范围，请重新输入"))
                    return
                
                await self._play_track(ev, index, state["keywords"], state["urls"], 
                                       state["name"], state["ar"], state["iurl"], state["rid"])
                controller.stop()
            
            try:
                await track_waiter(event)
            except TimeoutError:
                yield event.plain_result("选择超时！")
        except Exception as e:
            logger.error(f"[Random Error] 播放随机音声失败: {str(e)}")
            yield event.plain_result("播放随机音声失败，请稍后再试")

    async def get_asmr(self, event: AstrMessageEvent, rid: str, r, selected_index: int = None):
        # ... (未修改) ...
        name = r["title"]
        ar = r["name"]
        img = r["mainCoverUrl"]
        
        result = await self.fetch_with_retry(f"/api/tracks/{rid}")
        
        if result is None:
            await event.send(event.plain_result("获取音轨信息失败"))
            return None,None,None
        
        keywords, urls = [], []
        
        async def process_item(item):
            if item["type"] == "audio":
                keywords.append(item["title"])
                urls.append(item["mediaDownloadUrl"])
            elif item["type"] == "folder":
                if "children" in item and item["children"] is not None:
                    for child in item["children"]:
                        if isinstance(child, dict):
                            await process_item(child)
        
        for result2 in result:
            await process_item(result2)
        
        if not keywords:
            await event.send(event.plain_result("此音声没有可播放的音轨"))
            return None,None,None
        
        if selected_index is not None:
            if 0 <= selected_index < len(keywords):
                await self._play_track(event, selected_index, keywords, urls, name, ar, img, rid)
                return None,None,None
            else:
                await event.send(event.plain_result(f"节目编号 {selected_index + 1} 超出范围 (1 - {len(keywords)})"))
        
        # 使用 HTML 渲染创建表格和图片
        msg = f'### <div align="center">选择编号: RJ{rid}</div>\n' \
            f'|<img width="250" src="{img}"/> |**{name}** \n社团名：{ar}|\n' \
            '| :---: | --- |\n'
        
        for i in range(len(keywords)):
            msg += f'|{str(i+1)}. | {keywords[i]}|\n'
        
        msg1 = "请发送序号来获取要听的资源"
            
        template_data = {
            "text": msg
        }
        with open(self.template_path, 'r', encoding='utf-8') as f:
            meme_help_tmpl = f.read()
        url = await self.html_render(meme_help_tmpl, template_data)

        state = {
            "keywords": keywords,
            "urls": urls,
            "ar": ar,
            "url": f"https://asmr.one/work/RJ{rid}",
            "iurl": img,
            "name": name,
            "rid": rid
        }
        return msg1,url,state

    async def _play_track(self, event: AstrMessageEvent, index: int, keywords: list, 
                          urls: list, name: str, ar: str, img: str, rid: str):
        # ... (未修改) ...
        if index < 0:
            index = 0
        elif index >= len(urls):
            index = len(urls) - 1
        
        track_name = keywords[index]
        audio_url = urls[index]
        asmr_url = f"https://asmr.one/work/RJ{rid}"
        
        platform_name = event.get_platform_name()
        
        if platform_name == "aiocqhttp":
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            is_private = event.is_private_chat()
    
            headers2 = {
                "Content-Type":"application/json"
            }
            data={
                "url": audio_url,
                "song": track_name,
                "singer": ar,
                "cover": img,
                "jump": asmr_url,
                "format": "163",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post("https://oiapi.net/API/QQMusicJSONArk", json=data, headers=headers2, timeout=10) as response:
                    if response.status == 200:
                        js = (await response.json()).get("message")        
                        payloads = {
                            "message": [
                                {
                                    "type": "json",
                                    "data": {
                                        "data": js,
                                    },
                                }
                            ],
                        }
                        
                        if is_private:
                            payloads["user_id"] = event.get_sender_id()
                            await client.api.call_action("send_private_msg", **payloads)
                        else:
                            payloads["group_id"] = event.get_group_id()
                            await client.api.call_action("send_group_msg", **payloads)
                    else:
                        audio_info = (
                            f"🎧 **{track_name}** (Track {index+1})\n"
                            f"📻 **{name}** - {ar} (RJ{rid})\n"
                            f"🔗 **音频链接**: {audio_url}\n"
                            f"🌐 **作品页面**: {asmr_url}"
                        )
                        await event.send(event.plain_result(audio_info))
        
        else:
            audio_info = (
                f"--- 🎧 播放信息 ---\n"
                f"**曲目**: {track_name} (Track {index+1})\n"
                f"**作品**: {name}\n"
                f"**作者**: {ar} (RJ{rid})\n"
                f"\n"
                f"**🔗 音频链接**: {audio_url}\n"
                f"**🌐 作品页面**: <{asmr_url}>"
            )
            await event.send(event.image_result(img))
            await event.send(event.plain_result(audio_info))


    # --- 下载功能 (asmr下载) ---

    async def download_worker(self, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, 
                              file_info: Dict[str, Any], base_dir: Path, event: AstrMessageEvent) -> bool:
        """处理单个文件的下载，支持断点续传"""
        # ... (下载逻辑未修改) ...
        file_url = file_info.get('url')
        file_name = file_info['title']
        expected_size = file_info.get('size', 0)
        
        # 【优化 1.2】file_info["full_folder_path"] 此时已经是 ""，folder_path 为 ""
        folder_path = file_info.get("full_folder_path", "").replace(":", "：").replace("?", "？")
        file_name = file_name.replace(":", "：").replace("?", "？")
        # full_path 现在是: base_dir / "" / file_name，即 base_dir / file_name
        full_path = base_dir / folder_path / file_name
        
        mode = 'wb'
        headers_range = {}
        downloaded_size = 0
        
        full_path.parent.mkdir(parents=True, exist_ok=True)

        if full_path.exists():
            downloaded_size = full_path.stat().st_size
            if downloaded_size == expected_size and expected_size > 0:
                logger.info(f"[Download] 文件已完整存在: {file_name}")
                return True
            elif downloaded_size < expected_size:
                mode = 'ab'
                headers_range['Range'] = f'bytes={downloaded_size}-'
                logger.info(f"[Download] 续传: {file_name}, 从 {format_size(downloaded_size)} 开始")
            else:
                full_path.unlink(missing_ok=True)

        async with semaphore:
            try:
                download_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
                    "Referer": "https://asmr.one/"
                }
                if headers_range:
                    download_headers.update(headers_range)

                async with session.get(file_url, headers=download_headers) as response:
                    response.raise_for_status()
                    
                    total_size = int(response.headers.get('content-length', 0)) + downloaded_size
                    
                    logger.info(f"[Download] 开始下载: {file_name} (总大小 {format_size(total_size)})")
                    
                    async with aiofiles.open(full_path, mode) as f:
                        pbar_iter = response.content.iter_chunked(8192)
                        async for chunk in pbar_iter:
                            await f.write(chunk)

                logger.info(f"[Download] 🎉 下载成功: {file_name}")
                return True

            except aiohttp.ClientResponseError as e:
                logger.error(f"[Download Error] ❌ 下载失败 (HTTP {e.status}): {file_name}")
                return False
            except Exception as e:
                logger.error(f"[Download Error] ❌ 下载失败 (未知错误): {file_name}, {e}")
                return False

    async def _send_download_summary(self, event: AstrMessageEvent, rj_id: str, final_files: List[Dict[str, Any]], success_count: int, output_dir: Path):
        """发送下载总结消息"""
        summary_msg = f"### 📦 RJ{rj_id} 下载总结\n"
        summary_msg += f"- **总音频数**: {len(final_files)}\n"
        summary_msg += f"- **成功下载/跳过**: {success_count}\n"
        summary_msg += f"- **失败数**: {len(final_files) - success_count}\n"
        summary_msg += f"文件已保存在机器人服务器的: `{self.download_base_dir.as_posix()}/{output_dir.name}/` 目录下。"
        
        await event.send(event.plain_result(summary_msg))

    @filter.command("asmr下载")
    async def download_asmr(self, event: AstrMessageEvent):
        """交互式选择并下载音声文件"""
        
        args = event.message_str.replace("asmr下载", "").split()
        if not args:
            yield event.plain_result("请输入 RJ ID (例如: RJ0123456)!")
            return

        rj_match = RJ_RE.search(args[0])
        
        if not rj_match:
            yield event.plain_result("输入格式错误，请输入有效的 RJ ID。")
            return
            
        rj_id = rj_match.group("id")
        url_path = f"/api/tracks/{rj_id}?v=2"
        
        yield event.plain_result(f"🔍 正在查询 **RJ{rj_id}** 的可下载音频列表...")
        
        try:
            result = await self.fetch_with_retry(url_path)
        except Exception as e:
            logger.error(f"[Download Init Error] 获取文件列表失败: {e}")
            yield event.plain_result("获取文件列表失败，请稍后再试。")
            return
            
        if result is None:
            yield event.plain_result("获取文件列表失败，可能是 RJ ID 错误或 API 暂时不可用。")
            return

        all_audio_files: List[Dict[str, Any]] = []
        # 使用优化后的递归函数，只收集音频文件，并忽略子文件夹路径
        recursively_transform_data(result, all_audio_files, [])

        if not all_audio_files:
            yield event.plain_result(f"⚠️ 未找到 RJ{rj_id} 的可下载音频文件。")
            return
            
        # 【优化 3.2】精简选择逻辑，只列出所有音频文件
        selectable_items: Dict[str, List[Dict[str, Any]]] = {}
        msg = f"### 📦 RJ{rj_id} 找到 {len(all_audio_files)} 个音频文件。\n"
        msg += "**[音频文件选项]**\n"
        
        total_size_bytes = sum(f['size'] for f in all_audio_files)
        msg += f"**总大小**: {format_size(total_size_bytes)}\n"
        msg += "---"
        
        for i, file_info in enumerate(all_audio_files):
            key = f"I{i+1}"
            selectable_items[key] = [file_info] # 每个选项对应一个文件列表
            file_size = format_size(file_info.get('size', 0))
            # 显示序号和文件名
            msg += f"**{key}**: 🎵 `{file_info['title']}` ({file_size})\n"
            
        msg += "\n**提示**: 请回复选项编号 (例如: `I1`, `I2,I3`) 或 `*` (全部下载) 或 `q` (退出)。"
        
        yield event.plain_result(msg)
        
        id = event.get_sender_id()
        
        @session_waiter(timeout=self.timeout, record_history_chains=False)
        async def selection_waiter(controller: SessionController, ev: AstrMessageEvent):
            # 【优化 2.1】修复超时 Bug：确保在任何情况下 controller.stop() 都能被执行
            try:
                if ev.get_sender_id() != id:
                    return

                choice = ev.message_str.strip().upper()
                
                if choice == 'Q':
                    await ev.send(ev.plain_result("下载已取消。"))
                    return # 让 finally 块停止 controller

                final_files = []
                
                if choice == '*':
                    for files in selectable_items.values():
                        final_files.extend(files)
                else:
                    chosen_keys = [k.strip() for k in choice.split(',') if k.strip()]
                    valid_selection = True
                    for key in chosen_keys:
                        if key in selectable_items:
                            final_files.extend(selectable_items[key])
                        else:
                            await ev.send(ev.plain_result(f"⚠️ 无效的编号或键值: **{key}**，请重新输入。"))
                            valid_selection = False
                            break
                    if not valid_selection:
                        return # 让 finally 块停止 controller
                
                unique_files = {}
                for f in final_files:
                    unique_key = f.get("url") + f.get("title", "") # 使用 url+title 作为唯一键
                    if unique_key not in unique_files:
                        unique_files[unique_key] = f

                final_files = list(unique_files.values())
                
                if not final_files:
                    await ev.send(ev.plain_result("没有有效的文件被选中，请重新输入。"))
                    return # 让 finally 块停止 controller
                
                await ev.send(event.plain_result(f"✅ 您已选择下载 **{len(final_files)}** 个文件，正在启动异步下载..."))
                
                # 使用配置中的下载根目录
                # rj_output_dir 现在是 self.download_base_dir / "RJxxxxxx"
                rj_output_dir = self.download_base_dir / f"RJ{rj_id}"
                
                async with aiohttp.ClientSession() as session:
                    # 使用配置中的最大并发数
                    semaphore = asyncio.Semaphore(self.max_concurrent_downloads)
                    
                    download_tasks = [
                        self.download_worker(session, semaphore, f, rj_output_dir, ev)
                        for f in final_files
                    ]
                    
                    results = await asyncio.gather(*download_tasks)
                    success_count = sum(results)
                    
                    await self._send_download_summary(ev, rj_id, final_files, success_count, rj_output_dir)

            except Exception as e:
                logger.error(f"[Download Process Error] 下载过程出现致命错误: {e}")
                await ev.send(ev.plain_result(f"❌ 下载过程出现致命错误: {type(e).__name__}"))
            finally:
                # 无论结果如何，都停止会话控制器，避免超时。
                controller.stop()


        try:
            await selection_waiter(event)
        except TimeoutError:
            yield event.plain_result("选择超时，下载已取消。")
