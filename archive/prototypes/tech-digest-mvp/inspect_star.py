# -*- coding: utf-8 -*-
from bs4 import BeautifulSoup
import re

html = open("tech-digest-mvp/trending_daily_sample.html", encoding="utf-8").read()
soup = BeautifulSoup(html, "html.parser")
row = soup.select_one("article.Box-row")
txt = row.get_text(" ", strip=True)
m = re.findall(r"[^ ]*star[^ ]*", txt)
print("star mentions:", m)
s = str(row)
i = s.find("stargazers")
j = s.rfind("stargazers")
print("---- first stargazers context ----")
print(s[max(0, i - 200):i + 200])
print("---- last stargazers context ----")
print(s[max(0, j - 200):j + 200])
