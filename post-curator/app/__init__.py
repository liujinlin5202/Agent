# -*- coding: utf-8 -*-
"""post-curator：集市精华帖自动策展（秋坞 job 模式）。

每日扫描近 N 天未加精帖子（默认 3 天），把「正文 + 高赞评论」交给 LLM 评审，
对高价值问询（价值在评论区）与高价值内容帖自动加精；引战/灌水帖无论互动
数据多高一票否决。另备 --full 全量模式（手动触发，按互动信号预排后评审 top N）。

写路径走集市 Go 后端内部端点 POST /internal/featured（X-Internal-Token 守卫，
单一写方，写后失效主页浏览缓存）；读路径走 xiaoqiu_ro 只读视图
（v_posts_public / v_pcomments_public），零基表权限。
"""
