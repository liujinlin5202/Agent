# -*- coding: utf-8 -*-
import re
html = open("tech-digest-mvp/trending_daily_sample.html", encoding="utf-8").read()
h = html.find("stars today")
print(repr(html[h - 700:h + 60]))
