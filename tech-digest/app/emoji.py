# -*- coding: utf-8 -*-
"""emoji 清洗（2026-09-21 用户决策：周报/日报正文全面禁用 emoji）。

豁免仅一个：⭐（U+2B50）——GitHub 星榜的星数标识。
渲染层（render_weekly / render_repost / render_star_post / titles.clean）统一调用，
AI 产出哪怕带 emoji 也出不了帖。
"""
import re

# 覆盖常见 emoji 区段：
#   1F000-1FAFF 各类 pictograph（🤖📰🥇🚀🧠💡📖📈📌🔮🔁📅📡）
#   2600-27BF   杂项符号 + dingbats（⚠️⚡✅）
#   1F1E6-1F1FF 区域指示旗；2B00-2BFF 杂项符号与箭头（⬆️，⭐ 也在本块→单独豁免）
#   FE0F 变体选择符、200D ZWJ（emoji 组合用零宽字符）
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF️‍]"
)
_STAR = "⭐"
_KEEP = "\x00"   # 哨兵：先把 ⭐ 挪出去，清完再放回来


def strip_emoji(text: str | None) -> str | None:
    """去 emoji；保留 ⭐（GitHub 榜单星数）。空值原样返回。"""
    if not text:
        return text
    t = _EMOJI_RE.sub("", text.replace(_STAR, _KEEP))
    return t.replace(_KEEP, _STAR)
