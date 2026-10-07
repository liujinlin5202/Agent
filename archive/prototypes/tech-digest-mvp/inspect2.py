# -*- coding: utf-8 -*-
import re
html = open("tech-digest-mvp/trending_daily_sample.html", encoding="utf-8").read()
for pat in ["stars today", "star today", "today", "since", "Trending", "This week", "This month"]:
    hits = [m.start() for m in re.finditer(pat, html, re.I)]
    print(f"{pat!r}: {len(hits)} hits")
    if hits and pat in ("stars today", "today"):
        for h in hits[:3]:
            print("   ...", html[max(0, h - 120):h + 80].replace("\n", " "))
