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
# 导入所需的组件类
from astrbot.api.message_components import Node, Plain, Button, ActionButton, ButtonRow
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.session_waiter import (
    session_waiter,
    SessionController,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot import logger

# --- 全局配置和常量 ---
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0'
}
BASE_URLS = [
    "https://api.asmr.one",
    "https://api.asmr-100.com",
    "https://api.asmr-200.com",
    "https://api.asmr-300.com"
]
RJ_RE = re.compile(r"(?:RJ)?(?P<id>[\d]+)")
MAX_CONCURRENT_DOWNLOADS = 3  # 下载并发限制


# --- 辅助函数：文件处理和格式化 ---

def format_size(size_bytes: int) -> str:
    """将字节数格式化为可读的字符串"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def recursively_transform_data(data: List[Dict[str, Any]], all_files: List[Dict[str, Any]],
                               current_folder_path: List[str]):
    """递归遍历 API 返回的 JSON 结构，将所有文件扁平化，记录文件夹路径"""
    for item in data:
        item_type = item.get("type")
        item_title = item.get("title")

        if item_type == "folder":
            new_path = current_folder_path + [item_title]
            if "children" in item:
                recursively_transform_data(item["children"], all_files, new_path)
        elif item_type in ["text", "image", "audio"]:
            file_info = {
                "title": item_title,
                "url": item.get("mediaDownloadUrl"),
                "type": item_type,
                "size": item.get("size", 0),
                "full_folder_path": "/".join(current_folder_path),
            }
            all_files.append(file_info)


# --- ASMR 机器人插件类 ---

@register(
    "astrbot_plugin_asmr",
    "CCYellowStar2",
    "ASMR音声搜索、播放与下载",
    "2.1",  # 版本号更新
    "https://github.com/CCYellowStar2/astrbot_plugin_asmr"
)
class AsmrPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.timeout = 30
        self.base_urls = BASE_URLS
        self.current_api_index = 0
        self.plugin_dir = Path(__file__).parent
        self.template_path = self.plugin_dir / "md.html"
        self.nsfw = config.get("enable_nsfw", True)

    async def rotate_api(self):
        """切换到下一个API端点"""
        self.current_api_index = (self.current_api_index + 1) % len(self.base_urls)
        logger.info(f"切换到API: {self.base_urls[self.current_api_index]}")

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
                    errors.append(f"API {current_api} 请求失败: {str(e)}")
                    await self.rotate_api()

        error_msg = "所有API请求均失败:\n" + "\n".join(errors)
        logger.error(error_msg)
        return None

    # --- 命令：音声帮助 (新增交互式卡片菜单) ---

    @filter.command("音声帮助")
    async def asmr_help(self, event: AstrMessageEvent):
        """显示本ASMR插件的所有功能和用法示例，使用交互式卡片。"""

        title_node = Node(
            Plain("### 🎧 ASMR 音声插件功能\n"),
            Plain("本插件提供 ASMR 音声的搜索、播放与下载服务。\n")
        )

        features_node = Node(
            Plain("--- **主要功能** ---\n"),
            Plain("1. 🔍 **搜音声**: 通过关键词/标签查找作品。\n"),
            Plain("2. ⏯️ **听音声**: 通过 RJ 号和音轨编号播放作品。\n"),
            Plain("3. 🎲 **随机音声**: 快速获取并播放一个随机作品。\n"),
            Plain("4. 💾 **下载音声**: 交互式选择文件或文件夹下载到服务器。\n"),
            Plain("---")
        )

        button_row = ButtonRow(
            ActionButton(
                id="search_help",
                label="🔍 搜索示例",
                style=Button.Style.PRIMARY,
                text="搜音声 治愈/耳语 1"
            ),
            ActionButton(
                id="play_help",
                label="⏯️ 播放帮助",
                style=Button.Style.PRIMARY,
                text="听音声 RJ123456 1"
            ),
            ActionButton(
                id="download_info",
                label="💾 下载说明",
                style=Button.Style.DANGER,
                text="下载音声 RJ"  # 引导用户输入RJ号
            )
        )

        yield event.node_result(
            title_node,
            features_node,
            Plain("\n💡 **点击下方按钮可快速了解功能或开始使用!**"),
            button_row
        )

        # 兼容不支持组件的平台，提供纯文本提示
        if event.get_platform_name() not in ["discord", "kaiheila"]:  # 假设这些平台支持组件
            yield event.plain_result(
                "您也可以直接输入命令，例如: `搜音声 伪娘/催眠 1` 或 `下载音声 RJ0123456`"
            )

    # --- 命令：搜音声 (搜索功能) ---

    @filter.command("搜音声")
    async def search_asmr(self, event: AstrMessageEvent):
        args = event.message_str.replace("搜音声", "").split()
        if not args:
            yield event.plain_result("请输入搜索关键词(用'/'分割不同tag)和搜索页数(可选)！比如'搜音声 伪娘/催眠 1'")
            return
        # ... (搜索逻辑与原代码相同) ...
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
                if len(ids) == 7 or len(ids) == 5:
                    ids = "RJ0" + ids
                else:
                    ids = "RJ" + ids
                rid.append(ids)

            msg = ""
            for i in range(len(title)):
                msg += f"**{i + 1}.** 【{rid[i]}】 **{title[i]}** - {ars[i]}\n"

            msg += "\n请发送 `听音声+RJ号+节目编号（可选）` 来获取要听的资源"

            yield event.plain_result(f"### 🔍 搜索结果 (第 {r['pagination']['currentPage']} 页)\n" + msg)
            yield event.image_result(imgs[0])

        except Exception as e:
            logger.error(f"搜索音声失败: {str(e)}")
            yield event.plain_result("搜索音声失败，请稍后再试")

    # --- 命令：听音声 (播放功能) ---

    @filter.command("听音声")
    async def play_asmr(self, event: AstrMessageEvent):
        # ... (播放逻辑与原代码相同) ...
        args = event.message_str.replace("听音声", "").split()
        substrings = ["RJ", "rj", "Rj", "rJ"]

        if not args:
            yield event.plain_result("请输入RJ号！")
            return

        rid = args[0]

        for sub in substrings:
            if sub in args[0]:
                rid = args[0].replace(sub, "")
                break

        try:
            y = int(rid)
        except ValueError:
            yield event.plain_result("请输入正确的RJ号！")
            return
        selected_index = int(args[1]) - 1 if len(args) > 1 and args[1].isdigit() else None

        yield event.plain_result(f"正在查询音声信息！RJ{rid}")

        try:
            r = await self.fetch_with_retry(f"/api/workInfo/{rid}")

            if r is None or "title" not in r:
                yield event.plain_result("没有此音声信息或还没有资源")
                return
            if not self.nsfw and r["nsfw"] == True:
                yield event.plain_result("此音声为r18音声，管理员已禁止")
                return

            msg1, url, state = await self.get_asmr(event=event, rid=rid, r=r, selected_index=selected_index)

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
            logger.error(f"播放音声失败: {str(e)}")
            yield event.plain_result("播放音声失败，请稍后再试")

    @filter.command("随机音声")
    async def play_Random_asmr(self, event: AstrMessageEvent):
        # ... (随机播放逻辑与原代码相同) ...
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
            if len(ids) == 7 or len(ids) == 5:
                ids = "RJ0" + ids
            else:
                ids = "RJ" + ids
            rid = ids.replace("RJ", "")

            yield event.plain_result(f"抽取成功！**RJ号：{ids}**")

            msg1, url, state = await self.get_asmr(event=event, rid=rid, r=r)
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
            logger.error(f"播放随机音声失败: {str(e)}")
            yield event.plain_result("播放随机音声失败，请稍后再试")

    async def get_asmr(self, event: AstrMessageEvent, rid: str, r, selected_index: int = None):
        # ... (获取音轨和渲染逻辑与原代码相同) ...
        name = r["title"]
        ar = r["name"]
        img = r["mainCoverUrl"]

        result = await self.fetch_with_retry(f"/api/tracks/{rid}")

        if result is None:
            await event.send(event.plain_result("获取音轨信息失败"))
            return None, None, None

        keywords, urls = [], []

        async def process_item(item):
            if item["type"] == "audio":
                keywords.append(item["title"])
                urls.append(item["mediaDownloadUrl"])
            elif item["type"] == "folder":
                for child in item["children"]:
                    if isinstance(child, dict):
                        await process_item(child)

        for result2 in result:
            await process_item(result2)

        if not keywords:
            await event.send(event.plain_result("此音声没有可播放的音轨"))
            return None, None, None

        if selected_index is not None:
            if 0 <= selected_index < len(keywords):
                await self._play_track(event, selected_index, keywords, urls, name, ar, img, rid)
                return None, None, None
            else:
                await event.send(event.plain_result(f"节目编号 {selected_index + 1} 超出范围 (1 - {len(keywords)})"))

        msg = f'### <div align="center">选择编号: RJ{rid}</div>\n' \
              f'|<img width="250" src="{img}"/> |**{name}** \n社团名：{ar}|\n' \
              '| :---: | --- |\n'

        for i in range(len(keywords)):
            msg += f'|{str(i + 1)}. | {keywords[i]}|\n'

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
        return msg1, url, state

    async def _play_track(self, event: AstrMessageEvent, index: int, keywords: list,
                          urls: list, name: str, ar: str, img: str, rid: str):
        # ... (播放音轨逻辑与原代码相同) ...
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
                "Content-Type": "application/json"
            }
            data = {
                "url": audio_url,
                "song": track_name,
                "singer": ar,
                "cover": img,
                "jump": asmr_url,
                "format": "163",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post("https://oiapi.net/API/QQMusicJSONArk", json=data, headers=headers2,
                                        timeout=10) as response:
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
                            f"🎧 **{track_name}** (Track {index + 1})\n"
                            f"📻 **{name}** - {ar} (RJ{rid})\n"
                            f"🔗 **音频链接**: {audio_url}\n"
                            f"🌐 **作品页面**: {asmr_url}"
                        )
                        await event.send(event.plain_result(audio_info))

        else:
            audio_info = (
                f"--- 🎧 播放信息 ---\n"
                f"**曲目**: {track_name} (Track {index + 1})\n"
                f"**作品**: {name}\n"
                f"**作者**: {ar} (RJ{rid})\n"
                f"\n"
                f"**🔗 音频链接**: {audio_url}\n"
                f"**🌐 作品页面**: <{asmr_url}>"
            )
            await event.send(event.image_result(img))
            await event.send(event.plain_result(audio_info))

    # --- 下载功能的核心逻辑 ---

    async def download_worker(self, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore,
                              file_info: Dict[str, Any], base_dir: Path, event: AstrMessageEvent) -> bool:
        """处理单个文件的下载，支持断点续传"""

        file_url = file_info.get('url')
        file_name = file_info['title']
        expected_size = file_info.get('size', 0)

        folder_path = file_info.get("full_folder_path", "").replace(":", "：").replace("?", "？")
        file_name = file_name.replace(":", "：").replace("?", "？")
        full_path = base_dir / folder_path / file_name

        mode = 'wb'
        headers_range = {}
        downloaded_size = 0

        full_path.parent.mkdir(parents=True, exist_ok=True)

        if full_path.exists():
            downloaded_size = full_path.stat().st_size
            if downloaded_size == expected_size and expected_size > 0:
                logger.info(f"文件已完整存在: {file_name}")
                return True
            elif downloaded_size < expected_size:
                mode = 'ab'
                headers_range['Range'] = f'bytes={downloaded_size}-'
                logger.info(f"续传: {file_name}, 从 {format_size(downloaded_size)} 开始")
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

                    logger.info(f"开始下载: {file_name} (总大小 {format_size(total_size)})")

                    async with aiofiles.open(full_path, mode) as f:
                        async for chunk in response.content.iter_chunked(8192):
                            await f.write(chunk)

                logger.info(f"🎉 下载成功: {file_name}")
                return True

            except aiohttp.ClientResponseError as e:
                logger.error(f"❌ 下载失败 (HTTP {e.status}): {file_name}")
                return False
            except Exception as e:
                logger.error(f"❌ 下载失败 (错误): {file_name}, {e}")
                return False

    async def _send_download_summary(self, event: AstrMessageEvent, rj_id: str, final_files: List[Dict[str, Any]],
                                     success_count: int, output_dir: Path):
        """发送下载总结消息"""
        summary_msg = f"### 📦 RJ{rj_id} 下载总结\n"
        summary_msg += f"- **总文件数**: {len(final_files)}\n"
        summary_msg += f"- **成功下载/跳过**: {success_count}\n"
        summary_msg += f"- **失败数**: {len(final_files) - success_count}\n"
        summary_msg += f"文件已保存在机器人服务器的: `{output_dir.parent.name}/{output_dir.name}/` 目录下。"

        await event.send(event.plain_result(summary_msg))

    # --- 命令：下载音声 (交互式下载) ---

    @filter.command("下载音声")
    async def download_asmr(self, event: AstrMessageEvent):
        """交互式选择并下载音声文件"""

        args = event.message_str.replace("下载音声", "").split()
        if not args:
            yield event.plain_result("请输入 RJ ID (例如: RJ0123456)!")
            return

        search_query = args[0]
        rj_match = RJ_RE.search(search_query)

        if not rj_match:
            yield event.plain_result("输入格式错误，请输入有效的 RJ ID。")
            return

        rj_id = rj_match.group("id")
        url_path = f"/api/tracks/{rj_id}?v=2"

        yield event.plain_result(f"🔍 正在查询 **RJ{rj_id}** 的文件列表...")

        try:
            result = await self.fetch_with_retry(url_path)
        except Exception as e:
            logger.error(f"获取文件列表失败: {e}")
            yield event.plain_result("获取文件列表失败，请稍后再试。")
            return

        if result is None:
            yield event.plain_result("获取文件列表失败，可能是 RJ ID 错误或 API 暂时不可用。")
            return

        all_files: List[Dict[str, Any]] = []
        recursively_transform_data(result, all_files, [])

        if not all_files:
            yield event.plain_result(f"⚠️ 未找到 RJ{rj_id} 的可下载文件。")
            return

        folder_groups: Dict[str, List[Dict[str, Any]]] = {}
        for f in all_files:
            folder_path = f["full_folder_path"] or " (根目录)"
            if folder_path not in folder_groups:
                folder_groups[folder_path] = []
            folder_groups[folder_path].append(f)

        selectable_items: Dict[str, List[Dict[str, Any]]] = {}
        folder_index = 1
        item_index = 1

        msg = f"### 📦 RJ{rj_id} 找到 {len(all_files)} 个文件。\n"
        msg += "**[文件夹选项]**\n"

        sorted_folders = sorted(folder_groups.keys(), key=lambda x: (x == " (根目录)", x))

        for folder_path in sorted_folders:
            key = f"F{folder_index}"
            files_in_folder = folder_groups[folder_path]
            selectable_items[key] = files_in_folder
            file_count = len(files_in_folder)
            total_size_bytes = sum(f['size'] for f in files_in_folder)
            msg += f"**{key}**: 📁 `{folder_path}` ({file_count} 个文件, {format_size(total_size_bytes)})\n"
            folder_index += 1

        root_files = folder_groups.get(" (根目录)", [])
        if root_files:
            msg += "\n**[根目录下单个文件选项]**\n"
            for file_info in root_files:
                key = f"I{item_index}"
                selectable_items[key] = [file_info]
                file_size = format_size(file_info.get('size', 0))
                msg += f"**{key}**: 📄 `{file_info['title']}` ({file_size})\n"
                item_index += 1

        msg += "\n**提示**: 请回复选项编号 (例如: `F1`, `I2`, `F1,I3`) 或 `*` (全部下载) 或 `q` (退出)。"

        yield event.plain_result(msg)

        id = event.get_sender_id()

        @session_waiter(timeout=self.timeout, record_history_chains=False)
        async def selection_waiter(controller: SessionController, ev: AstrMessageEvent):
            if ev.get_sender_id() != id:
                return

            choice = ev.message_str.strip().upper()

            if choice == 'Q':
                await ev.send(ev.plain_result("下载已取消。"))
                controller.stop()
                return

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
                    return

            unique_files = {}
            for f in final_files:
                unique_key = f.get("url") + f.get("full_folder_path", "")
                if unique_key not in unique_files:
                    unique_files[unique_key] = f

            final_files = list(unique_files.values())

            if not final_files:
                await ev.send(ev.plain_result("没有有效的文件被选中，请重新输入。"))
                return

            await ev.send(ev.plain_result(f"✅ 您已选择下载 **{len(final_files)}** 个文件，正在启动异步下载..."))

            rj_output_dir = self.plugin_dir / "Downloads" / f"RJ{rj_id}"

            async with aiohttp.ClientSession() as session:
                semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

                download_tasks = [
                    self.download_worker(session, semaphore, f, rj_output_dir, ev)
                    for f in final_files
                ]

                results = await asyncio.gather(*download_tasks)
                success_count = sum(results)

                await self._send_download_summary(ev, rj_id, final_files, success_count, rj_output_dir)

            controller.stop()

        try:
            await selection_waiter(event)
        except TimeoutError:
            yield event.plain_result("选择超时，下载已取消。")