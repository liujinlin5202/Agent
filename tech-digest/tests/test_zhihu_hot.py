# -*- coding: utf-8 -*-
"""知乎热榜源（tophub 中转）解析单测：fixture 按真实 HTML 结构，不联网。"""
import unittest

from app.sources.zhihu_hot import parse_page

# 按 2026-09-17 实抓结构手写：<td>N.</td> 排名 + zhihu question 链接 + item-desc 热度
# 真页里同一链接出现两次（桌面/移动双份标记），须按 URL 去重保首次（即排名序）
TOPHUB_FIXTURE = """<html><body>
<a href="https://tophubdata.com/ad" target="_blank">广告位不该被抓</a>
<table><tbody>
<tr><td align="center">1.</td>
  <td class="al" align="center"><img src="x.png" width="100"></td>
  <td class="al">
    <div><a href="https://www.zhihu.com/question/111" target="_blank" rel="nofollow" itemid="1">存款利率下调，市场会有哪些影响？</a></div>
    <div class="item-desc">4283 万热度</div>
  </td></tr>
<tr><td align="center">2.</td>
  <td class="al" align="center"><img src="y.png" width="100"></td>
  <td class="al">
    <div><a href="https://www.zhihu.com/question/222" target="_blank" rel="nofollow">DeepSeek 新模型发布，技术水平如何？</a></div>
    <div class="item-desc">3105 万热度</div>
  </td></tr>
<tr><td align="center">3.</td>
  <td class="al" align="center"><img src="z.png" width="100"></td>
  <td class="al">
    <div><a href="https://www.zhihu.com/question/111" target="_blank" rel="nofollow">存款利率下调，市场会有哪些影响？</a></div>
    <div class="item-desc">4283 万热度</div>
  </td></tr>
<tr><td align="center">4.</td>
  <td class="al" align="center"><img src="w.png" width="100"></td>
  <td class="al">
    <div><a href="https://sspai.com/post/90001" target="_blank">非知乎链接应跳过</a></div>
    <div class="item-desc">100 万热度</div>
  </td></tr>
<tr><td align="center">5.</td>
  <td class="al" align="center"><img src="v.png" width="100"></td>
  <td class="al">
    <div><a href="https://www.zhihu.com/question/333" target="_blank">无热度文本的条目</a></div>
  </td></tr>
</tbody></table>
</body></html>"""


class TestZhihuHot(unittest.TestCase):
    def test_parse_items(self):
        items = parse_page(TOPHUB_FIXTURE)
        # 5 条 tr → 去重(111 重复) + 过滤(非知乎) → 3 条
        self.assertEqual(len(items), 3)
        first = items[0]
        self.assertEqual(first["type"], "news")
        self.assertEqual(first["source"], "zhihu-hot")
        self.assertEqual(first["title"], "存款利率下调，市场会有哪些影响？")
        self.assertEqual(first["url"], "https://www.zhihu.com/question/111")
        self.assertEqual(first["trending"]["rank"], 1)
        self.assertEqual(first["trending"]["heat"], 4283)

    def test_dedup_keeps_first_rank(self):
        items = parse_page(TOPHUB_FIXTURE)
        urls = [it["url"] for it in items]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(items[0]["trending"]["rank"], 1)

    def test_no_heat_defaults_zero(self):
        items = parse_page(TOPHUB_FIXTURE)
        self.assertEqual(items[-1]["trending"]["heat"], 0)
        self.assertEqual(items[-1]["trending"]["rank"], 3)  # 去重后顺序重编

    def test_rank_renumbered_after_dedup(self):
        # 排名以出现顺序为准（去重后连续重编，不用 td 原值留洞）
        items = parse_page(TOPHUB_FIXTURE)
        self.assertEqual([it["trending"]["rank"] for it in items], [1, 2, 3])

    def test_empty_page_raises(self):
        with self.assertRaises(ValueError):
            parse_page("<html><body>登录墙</body></html>")

    def test_cap(self):
        from app.sources.zhihu_hot import MAX_ITEMS
        rows = "".join(
            f'<tr><td align="center">{i}.</td><td class="al">'
            f'<div><a href="https://www.zhihu.com/question/{i}" target="_blank">条目{i}</a></div>'
            f'<div class="item-desc">{i} 万热度</div></td></tr>'
            for i in range(1, 40))
        items = parse_page(f"<table><tbody>{rows}</tbody></table>")
        self.assertEqual(len(items), MAX_ITEMS)


if __name__ == "__main__":
    unittest.main()
