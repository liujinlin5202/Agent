# -*- coding: utf-8 -*-
"""MVP 实验：解析 GitHub Trending 快照 → 打印结果 + 生成 markdown。

用法: python exp_parse.py trending_daily_sample.html
"""
import json
import re
import sys

from bs4 import BeautifulSoup


def parse_num(text: str) -> int:
    """'2.3k' -> 2300, '1.2m' -> 1200000, '5,678' -> 5678"""
    t = text.strip().lower().replace(',', '')
    if not t:
        return 0
    m = re.match(r'^([\d.]+)\s*([km]?)$', t)
    if not m:
        return 0
    val = float(m.group(1))
    suffix = m.group(2)
    if suffix == 'k':
        val *= 1_000
    elif suffix == 'm':
        val *= 1_000_000
    return int(val)


def parse_trending(html: str) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    rows = soup.select('article.Box-row')
    items = []
    for i, row in enumerate(rows, 1):
        # 仓库全名: h2 a 的 href (/owner/repo)
        a = row.select_one('h2 a')
        if a is None:
            continue
        href = a.get('href', '').strip('/')
        # 语言
        lang_el = row.select_one('span[itemprop="programmingLanguage"]')
        lang = lang_el.get_text(strip=True) if lang_el else None
        # 星总数: 顶部 a[href$="/stargazers"]
        star_counts = row.select('a[href$="/stargazers"]')
        stars = parse_num(star_counts[0].get_text()) if star_counts else 0
        # 今日新增: 底部 span.d-inline-block.float-sm-right，直接含文本 "3,902 stars today"（2026-08 实测无 a 标签）
        today_stars = 0
        bottom = row.select_one('span.d-inline-block.float-sm-right')
        if bottom:
            txt = bottom.get_text(' ', strip=True).lower()
            m = re.search(r'([\d,]+)\s*stars?\s*today', txt)
            if m:
                today_stars = int(m.group(1).replace(',', ''))
        # 描述
        desc_el = row.select_one('p')
        desc = desc_el.get_text(' ', strip=True) if desc_el else ''
        items.append({
            'rank': i,
            'full_name': href,
            'url': f'https://github.com/{href}',
            'language': lang,
            'stars': stars,
            'today_stars': today_stars,
            'desc': desc or '(无描述)',
        })
    return items


def render_md(items: list[dict], source: str) -> str:
    lines = [
        f'# GitHub 每日星榜 {source}',
        '',
        f'- 生成时间: {__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'- 数据源: {source}',
        f'- 榜单规模: Top {len(items)}',
        '',
        '| # | 仓库 | 语言 | ⭐ 总数 | 今日 ⭐ | 简介 |',
        '|---| --- | --- | --- | --- | --- |',
    ]
    for it in items:
        desc = it['desc'].replace('|', '/')
        lines.append(
            f"| {it['rank']} | [{it['full_name']}]({it['url']}) "
            f"| {it['language'] or '—'} | {it['stars']:,} "
            f"| {it['today_stars']:,} | {desc} |"
        )
    return '\n'.join(lines)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else 'trending_daily_sample.html'
    html = open(src, encoding='utf-8').read()
    items = parse_trending(html)
    print(f'[parse] rows={len(items)}')
    for it in items[:5]:
        print(f"  #{it['rank']} {it['full_name']} lang={it['language']} "
              f"stars={it['stars']:,} today={it['today_stars']:,} desc={it['desc'][:60]!r}")
    md = render_md(items, 'https://github.com/trending?since=daily (实验快照)')
    out = 'mvp_daily_output.md'
    open(out, 'w', encoding='utf-8').write(md)
    print(f'[write] {out} ({len(md)} bytes)')


if __name__ == '__main__':
    main()
