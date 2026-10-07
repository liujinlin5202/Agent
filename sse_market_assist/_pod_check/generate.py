# -*- coding: utf-8 -*-
"""AI 回答生成（LCEL 链）：LLM 基于检索材料生成带引用回答（约束规则见文档 4.3）。

模型通道经 app.llm.get_routed_llm（判断层）：小 prompt → qwen，大 prompt → deepseek。

提供两个入口：
- build_answer()：首次回答生成（站内+可选站外资料）
- build_followup_answer()：追问回答生成（基于历史对话）
"""
from __future__ import annotations

from typing import Any

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from app.llm import get_routed_llm

SYSTEM_TEMPLATE = """你是软工集市的站内智能助手。用户刚在【{partition}】分区发了一篇帖子，我们检索了该分区内的历史讨论（帖子、回复、楼中楼）供你参考。

严格规则：
1. 只依据下方【检索材料】回答；材料不足以回答时，明确说"站内暂时没有找到相关讨论"，禁止编造帖子内容、发帖人或结论。
2. 回答中提到的每个观点，若来自某条材料，须用 [n] 形式标注对应参考编号。
3. 禁止泄露任何用户个人信息（姓名、联系方式等）；匿名帖的内容可以引用观点，但不得提及发帖人身份。
4. 语言简洁，分点组织；先给结论，再给依据；最后列出相关帖子标题与链接（用材料里给出的链接）。
5. 不要复述用户刚发的帖子内容本身，只输出"站内相关讨论"的结论。
6. 信息密度要高：优先提炼材料中具体、可操作的事实（时间、地点、价格、课程代码、链接、数据、流程、经验做法等），避免空泛复述；相关回复与楼中楼和帖子正文具有同等引用价值，引用评论中的事实时同样标注 [n]。
7. 【站外资料】部分检索材料可能来自站外 Web 搜索（已标注 🌐 站外 标签）。这些内容仅供参考，引用时须明确说明"据站外资料"。"""

PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_TEMPLATE),
        (
            "user",
            "用户刚发的帖子：\n标题：{title}\n正文：{content}\n\n【检索材料】\n{context}\n\n请回答：",
        ),
    ]
)

_chain = PROMPT | get_routed_llm() | StrOutputParser()


# ---------------------------------------------------------------------------
# 追问对话
# ---------------------------------------------------------------------------

FOLLOWUP_SYSTEM_TEMPLATE = """你是软工集市的站内智能助手。你之前已经基于站内历史讨论回答过用户的问题。现在用户在追问更多细节。

规则：
1. 基于之前的回答和检索材料继续回答。
2. 如果追问超出原始检索材料的范围，诚实地说明"之前的检索材料中没有覆盖这方面"。
3. 继续保持简洁、分点、有依据的风格。
4. 禁止编造不存在的帖子或信息。"""

FOLLOWUP_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", FOLLOWUP_SYSTEM_TEMPLATE),
        MessagesPlaceholder(variable_name="history"),
        ("user", "{question}"),
    ]
)


def build_answer(partition: str, title: str, content: str, context: str) -> str:
    """生成 AI 回答（同步调用；由 FastAPI 线程池承载）。"""
    return _chain.invoke(
        {
            "partition": partition,
            "title": title,
            "content": content[:500],
            "context": context,
        }
    )


def build_followup_answer(post_title: str, original_context: str,
                           references: list[dict[str, Any]],
                           history: list[dict[str, str]], question: str) -> str:
    """追问回答生成。

    Args:
        post_title: 原始帖子标题
        original_context: 首次回答的检索材料文本（供参考）
        references: 引用列表（用于构建历史摘要）
        history: 对话历史 [{"role": "user"|"assistant", "content": "..."}]
        question: 用户追问的问题
    """
    # 构建历史消息
    history_msgs = []
    for h in history:
        role = "user" if h["role"] == "user" else "assistant"
        history_msgs.append((role, h["content"]))

    chain = FOLLOWUP_PROMPT | get_routed_llm() | StrOutputParser()
    return chain.invoke(
        {
            "history": history_msgs,
            "question": question,
        }
    )
