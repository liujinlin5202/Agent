# -*- coding: utf-8 -*-
"""daily AI v4：从「3-4 条摘要日报」改为「每天 1 篇爆文全文转载」。

2026-09-17 用户决策：日报只挑一篇兼具时效性与影响性的爆点文章，全文搬到集市
（转载自 xxx）；抓不到全文的（知乎/公众号系）降级为深度导读+链接；英文文章
AI 全文翻译成中文。GitHub 星榜移出日报，周六单独发帖。

三个入口：
  ai_pick(candidates, day, preference=None)   → 挑 1 篇 + 编者按 + 深度导读 + 发帖标题候选
  ai_translate(text)         → 英文全文 → 中文
  ai_trending_desc(trending) → 星榜中文简介（周六星榜帖用）

不变的护栏——模型在这几件事上被实测证伪过，一层都不能省：
  ① **标题精确绑定**（2026-08-30）：编号记忆不可靠 → 按标题精确匹配候选，
     写错/写不存在的条目整体丢弃。
  ② **内容相关性校验**：编者按/导读必须与「标题+原文摘要」有词元重叠，
     否则整体丢弃（防串条目、防空话）。
"""
from __future__ import annotations

import json
import logging
import re

from app import llm, titles
from app.preference import Preference

log = logging.getLogger("tech-digest.daily_ai")

SYSTEM_PROMPT = (
    "你是校园技术社区「每日一篇」的编辑，读者是高校在校学生（会写代码，关心课程、"
    "实习、竞赛和项目）。你只基于给定候选条目撰写，绝不编造标题、作者或事实。"
    "全文禁止使用 emoji 表情符号。"
)

TRANSLATE_SRC_CAP = 12_000   # 送翻译的英文原文上限（≈中文 8k 输出预算）
MAX_WHY_CHARS = 400          # 编者按
MAX_DIGEST_CHARS = 1200      # 深度导读
MIN_WHY_CHARS = 40
MIN_DIGEST_CHARS = 100
MAX_POST_TITLES = 3
MAX_GLOSSARY = 3             # 知识卡片条数
MIN_GLOSSARY_EXPL = 10       # 解释最短（防「太短」占位）
MAX_GLOSSARY_EXPL = 120
GLOSSARY_CTX_CAP = 2_500     # 送选术语的正文片段上限

# ---- 标题-内容相关性校验（沿用 v3 实测护栏） ----------------
_CJK_RE = re.compile(r"[一-鿿]+")
_ENG_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ACRONYM_RE = re.compile(r"\b[A-Z]{1,3}\b")
_WS_RE = re.compile(r"\s+")

_STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "for", "with", "to", "and", "or", "not",
    "no", "this", "that", "these", "those", "its", "it", "as", "by",
    "from", "into", "over", "under", "about", "after", "before", "how",
    "what", "why", "when", "where", "who", "which", "while", "will",
    "would", "can", "could", "should", "may", "might", "must", "now",
    "new", "out", "up", "down", "off", "than", "then", "so", "vs",
    "via", "you", "your", "we", "our", "they", "them", "their", "he",
    "she", "his", "her", "but", "if", "do", "does", "did", "has",
    "have", "had", "get", "gets", "got", "made", "make", "makes",
    "show", "shows", "showing", "let", "lets", "say", "says", "said",
    "use", "uses", "using", "still", "just", "more", "most", "much",
    "many", "some", "any", "all", "one", "two", "three", "also",
    "even", "only", "very", "too", "here", "there", "need", "needs",
    "want", "wants", "help", "helps", "set", "sets", "see", "seen",
}


def _keywords(text: str) -> tuple[set[str], list[str]]:
    """返回 (英文词元集, 中文连续片段列表)。"""
    t = (text or "").lower()
    eng = {w for w in _ENG_TOKEN_RE.findall(t)
           if len(w) >= 4 and w not in _STOP_WORDS}
    for w in _ACRONYM_RE.findall(text or ""):
        if len(w) >= 2 and w.lower() not in _STOP_WORDS:
            eng.add(w.lower())
    return eng, _CJK_RE.findall(text or "")


def _cjk_bigrams(segments: list[str]) -> set[str]:
    grams: set[str] = set()
    for seg in segments:
        if len(seg) < 2:
            continue
        grams.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return grams


def is_content_related(title: str, orig_summary: str, ai_text: str) -> bool:
    """AI 文本是否真的在讲这条（标题+原文摘要）。三路任一命中即通过。"""
    text = (ai_text or "").lower()
    title_eng, title_cjk = _keywords(title)
    if any(re.search(rf"\b{re.escape(w)}\b", text) for w in title_eng):
        return True
    ai_grams = _cjk_bigrams(_CJK_RE.findall(ai_text or ""))
    if _cjk_bigrams(title_cjk) and len(_cjk_bigrams(title_cjk) & ai_grams) >= 2:
        return True
    orig_grams = _cjk_bigrams(_CJK_RE.findall(orig_summary or ""))
    return bool(orig_grams and len(orig_grams & ai_grams) >= 2)


_WRAP_PAIRS = [("《", "》"), ("「", "」"), ("『", "』"), ("【", "】"),
               ('"', '"'), ("'", "'")]


def _unwrap(title: str) -> str:
    """剥最外层成对包裹符号（模型常学候选行格式给标题加《》）。"""
    t = (title or "").strip()
    for left, right in _WRAP_PAIRS:
        if len(t) >= 2 and t.startswith(left) and t.endswith(right):
            return t[1:-1].strip()
    return t


_CLIP_BREAKS = "，。！？；：、,.!?;:"
_CLIP_TAILS = "，,、；;：: "


def _clip(text: str, limit: int) -> str:
    """超长时按标点截断，避免切出半句。"""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    head = t[:limit]
    for i in range(len(head) - 1, max(limit // 3, 1) - 1, -1):
        if head[i] in _CLIP_BREAKS:
            cut = head[:i].rstrip(_CLIP_TAILS)
            return cut or head
    return head


def _strip_fence(text: str) -> str:
    """剥 ```json ... ``` 包装。找不到 { 时原样返回。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        end = t.rfind("```")
        if end != -1:
            t = t[:end]
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        t = t[start:end + 1]
    return t.strip()


# ---- 候选格式化（热度信号进 prompt：时效×影响是挑选依据） ----

def _signal(it: dict) -> str:
    src = it.get("source") or ""
    t = it.get("trending") or {}
    if src == "hacker-news":
        pts = it.get("points") or 0
        return f"HN {pts} points" if pts else src
    if src == "devto":
        pts = it.get("points") or 0
        return f"dev.to {pts} 人赞" if pts else src
    if src == "zhihu-hot":
        return f"知乎热榜第 {t.get('rank', '?')} 名（{t.get('heat', 0)} 万热度）"
    if it.get("published_at"):
        return f"发布于 {it['published_at'][:16]}"
    return src


def _candidate_lines(candidates: list[dict]) -> str:
    from app.digest import SOURCE_LABEL
    lines = []
    for i, it in enumerate(candidates, 1):
        src = SOURCE_LABEL.get(it["source"], it["source"])
        summary = (it.get("summary") or "").replace("\n", " ")[:200]
        author = f"｜作者: {it['author']}" if it.get("author") else ""
        gate = (f"｜正文约 {it['body_len']} 字，已验证可抓全文"
                if it.get("body_len") else "")
        lines.append(
            f'[{i}] 《{it["title"]}》（来源: {src}｜热度: {_signal(it)}{author}{gate}）\n'
            f'   原文摘要: {summary or "（无）"}')
        excerpt = (it.get("excerpt") or "").strip()
        if excerpt:
            lines[-1] += f"\n   正文预览: {excerpt}"
    return "\n".join(lines)


def _gated_clause(candidates: list[dict]) -> str:
    """全池过闸门时提示 AI：正文预览是判断内容质量的直接证据。"""
    if not any(it.get("body_len") for it in candidates):
        return ""
    return (
        "本批候选已全部预取验证能抓到全文：「正文预览」是文章开头原文，"
        "请用它判断真实内容质量——预览里空洞的（没有具体技术内容），标题再热也不选。\n"
    )


def _build_pick_prompt(candidates: list[dict],
                       preference: Preference | None = None) -> str:
    prefer_clause = ""
    if preference is not None and preference.active:
        prefer_clause = (
            f"7. 编辑当前倾向：{preference.text}。这是**加分项，不是硬性条件**："
            "倾向话题的候选若今天质量差（过气、太浅、擦边、与学习无关），"
            "必须放弃倾向，仍按上述 1-6 条标准挑今天最高价值的一篇。"
            "宁可发非倾向的高价值文章，不发倾向的低质量文章。\n"
        )
    return (
        f"以下是今天各热点榜的候选文章，共 {len(candidates)} 条"
        f"（标题是唯一标识，不可改写）：\n\n"
        f"{_candidate_lines(candidates)}\n\n"
        "请从中挑出**恰好 1 篇**今天最值得全文转载到校园技术社区的文章，"
        "只输出一个 JSON 对象（不要 markdown 代码块、不要多余文字）：\n"
        '{"title": "候选原始标题（必须与上面完全一字不差）",\n'
        ' "why": "编者按 100-200 字：今天为什么值得读这篇（结合热度/时效信号），'
        '读完能收获什么，跟学生（课程/实习/竞赛/项目）有什么关系",\n'
        ' "digest": "500-800 字深度导读：讲清文章的核心内容与脉络。'
        '注意：若原文抓取不到全文，这段导读将代替全文发布，请写得完整可读、有信息量",\n'
        ' "post_titles": ["≤30 字发帖标题候选，共 3 个"]}\n\n'
        f"{_gated_clause(candidates)}"
        "挑选标准（按优先级）：\n"
        "1. 信息价值（一票否决）：读完必须能带走具体的东西——讲清一个机制/原理/实现，"
        "给出可复现的步骤或代码，或有一手数据与教训。以下类型一律不选：新闻通稿/"
        "发布会转述、融资/人事/并购消息、观点评论/访谈、榜单盘点、营销软文、"
        "小版本更新（修复与常规迭代）。AI 话题例外：AI 相关的行业动态、重大产品发布、"
        "投融资并购/人事变动类文章不在禁选之列——说清谁、做了什么、对开发者/行业"
        "意味着什么，就可选；非 AI 话题的同类消息仍然不选；\n"
        "2. 时效性：今天榜上的才算热，宁可要今天的热点，不要陈年旧文；\n"
        "3. 实质信号：摘要或正文节选里有具体技术名词、代码、数字、架构细节的优先；"
        "通篇「突破/颠覆/革命性」却给不出任何具体内容的，再热也不选；\n"
        "4. 深度长文 > 短讯：这篇是要「全文转载」的，太浅太短的文章撑不起来"
        "（标注了字数的候选都实际抓到了全文，字数通常与内容厚度成正比）；\n"
        "5. 影响性与大厂/人物：热度信号（HN points / 知乎热榜排名）说明关注度；"
        "知名大厂（OpenAI、英伟达、Google、字节、华为这类）或知名人物是加分——"
        "但只能在信息价值相当时用来分胜负，不许因为名字响就选一篇没内容的稿子；"
        "时效仍是前提，过气旧闻再知名也不选；\n"
        "6. 学生相关性：课程、实习/秋招、竞赛、做项目、学习路线，至少沾一个；"
        "话题必须是技术/科技类——计算机、软件、AI、硬件、网络与安全、工程实践、"
        "科技产业动态与产品发布，或与工程/科学直接相关的深度内容。旅行游记、"
        "城市/历史文化随笔、生活方式、职场感悟、泛社会新闻这类非技术文章一律不选，"
        "写得再长再好也不选——不许用「对思维/做项目有启发」把它合理化"
        "（知乎热榜的娱乐八卦、体育类同理）。\n\n"
        f"{prefer_clause}"
        "写作规则：\n"
        "· 说人话，像同学之间转述；不要「据悉」「赋能」「生态」「引领」这类词；\n"
        "· 全部内容基于候选信息改写，禁止引用候选之外的事实；\n"
        "· 核心技术名词保留英文原名（如 RISC-V、Postgres、LLM）；\n"
        "· 文章若涉及知名大厂/人物，编者按要把名字放在显眼位置（比如开头），"
        "读者对熟悉的名字更敏感；\n"
        "· 导读里最关键的 2-3 个结论或数字用 **加粗** 标出，方便快速抓重点；\n"
        "· post_titles：钩子式，可带问句、具体数字，让人一眼觉得「跟我有关」；"
        "禁止「速览/动态/盘点/重磅」，禁止日期前缀。"
    )


def _lexical_possible(cand: dict) -> bool:
    """词元校验对该候选是否可能命中。

    纯英文候选（标题+摘要都无中文）+ 中文编者按 → 词元上结构性地不可能重叠
    （AI 会把 "query plans" 译成「查询计划」）。这类候选靠标题逐字绑定守门，
    跳过内容校验；中文候选（sspai/知乎）保留完整校验——v3 实测的串条目
    就发生在中文源。
    """
    return bool(_CJK_RE.search(cand.get("title") or "")
                or _CJK_RE.search(cand.get("summary") or ""))


def sanitize_pick(content: str | None, candidates: list[dict]) -> dict | None:
    """解析并校验 ai_pick 输出。任何一层不过 → None（调用方整体降级）。"""
    if not content or not candidates:
        return None
    try:
        raw = json.loads(_strip_fence(content))
        if not isinstance(raw, dict):
            log.warning("pick 被拒: 输出不是 JSON 对象")
            return None
    except (ValueError, TypeError):
        log.warning("pick 被拒: JSON 解析失败（%.120s）", content or "")
        return None

    title = _unwrap(str(raw.get("title") or ""))
    idx = next((i for i, it in enumerate(candidates, 1)
                if (it.get("title") or "").strip().lower() == title.lower()), 0)
    if not idx:
        log.warning("pick 被拒: 标题绑定失败（模型输出标题=%s）", title[:60])
        return None                                   # 标题绑定失败 → 整体丢弃
    cand = candidates[idx - 1]

    why = str(raw.get("why") or "").strip()
    if len(why) < MIN_WHY_CHARS:
        log.warning("pick 被拒: 编者按过短（%d 字）", len(why))
        return None
    if _lexical_possible(cand) and not is_content_related(
            cand.get("title", ""), cand.get("summary", ""), why):
        log.warning("pick 被拒: 编者按与候选内容不相关（候选=%s）", cand.get("title", "")[:40])
        return None                                   # 编者应是空话/串条目 → 丢弃
    why = _clip(why, MAX_WHY_CHARS)

    digest = str(raw.get("digest") or "").strip()
    if len(digest) < MIN_DIGEST_CHARS or (
            _lexical_possible(cand) and not is_content_related(
                cand.get("title", ""), cand.get("summary", ""), digest)):
        log.info("导读不合格置空（len=%d），渲染层降级", len(digest))
        digest = ""                                   # 导读不合格 → 置空（渲染层降级导读）

    post_titles: list[str] = []
    for t in raw.get("post_titles") or []:
        if isinstance(t, str) and t.strip():
            post_titles.append(t.strip())
        if len(post_titles) >= MAX_POST_TITLES:
            break

    return {"idx": idx, "why": why, "digest": _clip(digest, MAX_DIGEST_CHARS),
            "post_titles": post_titles}


def ai_pick(candidates: list[dict], day: str = "",
            preference: Preference | None = None) -> dict | None:
    """挑 1 篇爆文。LLM 失败/校验不过 → None。"""
    if not candidates:
        return None
    content = llm.chat(_build_pick_prompt(candidates, preference), system=SYSTEM_PROMPT)
    return sanitize_pick(content, candidates)


TRANSLATE_PROMPT = (
    "你是专业的技术文章译者。把下面的文章翻译成中文，要求：\n"
    "· 忠实原文，不增删观点，不概括不缩写；\n"
    "· 输入可能混入网页噪声：评论区、作者资料卡（Email/Location/Work/Joined 等字段）、"
    "导航、相关推荐，以及「复制链接/Copy link/reactions/View full discussion」这类交互"
    "元素——这些一律丢弃，不要翻译进正文，只译文章本身的正文；\n"
    "· 代码块（``` 围栏）原样保留：代码一字不改、不翻译（注释可译成中文），"
    "绝不许把代码改写成文字描述，也绝不许删掉代码块；\n"
    "· 技术名词、项目名、专有名词保留英文（如 RISC-V、LLM、Postgres）；\n"
    "· 保留段落结构，段落之间空一行；\n"
    "· 必须执行：用 **加粗** 标出全文 3-6 处最关键的句子（核心结论、关键数字、重要转折），"
    "这是硬性要求，一处都不能少；只加粗句子中真正关键的部分或整句，全篇不超过 6 处；\n"
    "· 只输出译文，不要任何解释或前言。"
)


def ai_translate(text: str) -> str | None:
    """英文全文 → 中文，带完整性机械断言：不通过重试 1 次，两次均不过 → None
    （调用方走 digest→summary→none 降级阶梯）。09-18 删码事故的机制防线：
    prompt 约定挡不住模型抖动，只有翻译后断言能拦。"""
    body = (text or "").strip()
    if not body:
        return None
    src = body[:TRANSLATE_SRC_CAP]
    for attempt in (1, 2):
        out = (llm.chat(src, system=TRANSLATE_PROMPT) or "").strip()
        if not out or _is_refusal(out):
            return None
        ok, why = translation_intact(src, out)
        if ok:
            return out
        log.warning("翻译完整性断言未过（第 %d 次）：%s", attempt, why)
    log.error("翻译两次未过完整性断言，降级导读：%s", why)
    return None


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def translation_intact(original: str, translated: str) -> tuple[bool, str]:
    """翻译完整性断言（与 TRANSLATE_PROMPT 的约定一一对应，但靠机制不靠约定）：

    ① 围栏数一致——代码块不许增删（09-18：9 个 SQL 块被整段删光）；
    ② 段落保全 ≥80%——整段漏译兜底；
    ③ 数字保留 ≥90%——关键数据（延迟/版本/百分比）不许静默蒸发。
    返回 (是否通过, 不通过原因)；通过时原因 given ""。
    """
    o, t = original or "", translated or ""
    if t.count("```") != o.count("```"):
        return False, (f"代码围栏数不一致：原文 {o.count('```')} 个围栏，"
                       f"译文 {t.count('```')} 个（代码块被删或编造）")
    o_paras, t_paras = o.count("\n\n") + 1, t.count("\n\n") + 1
    if t_paras < 0.8 * o_paras:
        return False, f"段落丢失：原文约 {o_paras} 段，译文仅 {t_paras} 段"
    nums = _NUM_RE.findall(o)
    if nums:
        kept = sum(1 for n in nums if n in t)
        if kept < 0.9 * len(nums):
            return False, f"数字丢失：原文 {len(nums)} 个数字仅保留 {kept} 个"
    return True, ""


def _is_refusal(text: str) -> bool:
    """模型拒答/跑题时返回的英文说明不是译文，不能当正文发。"""
    head = text[:120].lower()
    return head.startswith(("i can't", "i cannot", "i'm sorry", "sorry,")) \
        and not _CJK_RE.search(text)


# ---- 知识卡片（v4.1：本科生可能陌生的概念，details 折叠卡展示） ----

def sanitize_glossary(content: str | None) -> list[dict]:
    """解析知识卡片输出：术语 2-40 字、解释 10-120 字且必须含中文，最多 3 条。"""
    if not content:
        return []
    try:
        raw = json.loads(_strip_fence(content))
    except (ValueError, TypeError):
        return []
    entries = raw.get("glossary") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    out: list[dict] = []
    for g in entries:
        if not isinstance(g, dict):
            continue
        term = str(g.get("term") or "").strip()
        expl = str(g.get("expl") or "").strip()
        if not (2 <= len(term) <= 40) or len(expl) < MIN_GLOSSARY_EXPL:
            continue
        if not _CJK_RE.search(expl):
            continue
        out.append({"term": term, "expl": _clip(expl, MAX_GLOSSARY_EXPL)})
        if len(out) >= MAX_GLOSSARY:
            break
    return out


def ai_glossary(title: str, context: str) -> list[dict]:
    """从文章里挑 ≤3 个本科生可能陌生的概念做知识卡片。

    纯装饰性增强：任何失败（无 key/超时/输出不合格）→ []，不影响发布。
    """
    ctx = _WS_RE.sub(" ", (context or ""))[:GLOSSARY_CTX_CAP].strip()
    if not (title or "").strip() or len(ctx) < 50:
        return []
    prompt = (
        f"文章标题：《{title.strip()}》\n正文片段：{ctx}\n\n"
        "从中挑出最多 3 个「计算机本科生可能陌生的概念/术语」（宁缺毋滥，可以是 0 个），"
        "每个给 ≤80 字的通俗解释——说人话，像学长给学弟讲。"
        '只输出 JSON：{"glossary": [{"term": "术语（可中英并列，如 查询计划（query plan））", '
        '"expl": "解释"}]}\n'
        "· 只解释文中真正出现且影响理解的概念，解释要贴合文中语境，不要编造；\n"
        "· 本科生都懂的常见词（API、数据库、框架名）不要解释。"
    )
    try:
        return sanitize_glossary(llm.chat(prompt, system=SYSTEM_PROMPT))
    except Exception:  # noqa: BLE001 卡片是装饰，失败静默跳过
        return []


# ---- 周六星榜简介 ----

def _norm_repo_name(key: str) -> str:
    k = re.sub(r"^https?://github\.com/", "", (key or "").strip(), flags=re.I)
    return k.strip().strip("/").lower()


def _trending_lines(trending: list[dict]) -> str:
    lines = []
    for i, it in enumerate(trending, 1):
        t = it.get("trending") or {}
        desc = (it.get("summary") or "").replace("\n", " ")[:180]
        lines.append(f"[{i}] {it['title']}（lang={t.get('language') or '?'} "
                     f"｜ 今日+{t.get('today_stars', 0):,}）\n   英文简介: {desc or '（无）'}")
    return "\n".join(lines)


def sanitize_trending_desc(content: str | None,
                           trending: list[dict]) -> dict[str, str]:
    if not content or not trending:
        return {}
    try:
        raw = json.loads(_strip_fence(content))
        if not isinstance(raw, dict):
            return {}
    except (ValueError, TypeError):
        return {}
    name2title: dict[str, str] = {}
    for t in trending:
        name2title.setdefault(_norm_repo_name(t.get("title") or ""), t.get("title", ""))
    out: dict[str, str] = {}
    for k, v in (raw.get("trending_desc") or {}).items():
        v = str(v or "").strip()
        name = name2title.get(_norm_repo_name(str(k)))
        if name and v and _CJK_RE.search(v):
            out[name] = v[:60]
    return out


def ai_trending_desc(trending: list[dict]) -> dict[str, str]:
    """星榜中文简介（repo 全名 → ≤60 字）。失败 → {}（渲染回退英文简介）。"""
    if not trending:
        return {}
    prompt = (
        f"以下是 GitHub Trending 今日榜单（项目全名是唯一标识）：\n\n"
        f"{_trending_lines(trending)}\n\n"
        '只输出 JSON：{"trending_desc": {"项目全名（逐字一致）": "≤40 字中文简介"}}\n'
        "简介说明这个项目解决什么问题、为什么今天火；不要整段翻译英文简介。"
    )
    return sanitize_trending_desc(llm.chat(prompt, system=SYSTEM_PROMPT), trending)
