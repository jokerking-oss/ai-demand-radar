# -*- coding: utf-8 -*-
"""第二轮探测：贴吧详情/JSON API、微博移动端、其他中文社区。"""
import json
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
                    "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
      "Accept-Language": "zh-CN,zh;q=0.9"}
PC_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}


def probe(name, url, mobile=True, show=300):
    print("=" * 70)
    print(name, "|", url[:120])
    try:
        req = urllib.request.Request(url, headers=UA if mobile else PC_UA)
        r = urllib.request.urlopen(req, timeout=15)
        html = r.read().decode("utf-8", "ignore")
        print("  status:", r.status, "len:", len(html))
        snippet = html[:show].replace("\n", " ")
        print("  head :", snippet)
        return html
    except Exception as e:
        print("  FAIL:", type(e).__name__, str(e)[:140])
        return ""


def as_json(html, name):
    try:
        j = json.loads(html)
        print("  JSON keys:", list(j.keys())[:12])
        print("  JSON     :", json.dumps(j, ensure_ascii=False)[:500])
        return j
    except Exception:
        print("  (not json)")
        return None


if __name__ == "__main__":
    kw = urllib.parse.quote("AI工具")

    # ---- 贴吧：移动端 js API ----
    probe("贴吧-H5搜索thread", "https://tiebac.baidu.com/mo/q/h5/msearch/thread?word=%s&pn=0&rn=10" % kw)
    probe("贴吧-mo search新版", "https://tiebac.baidu.com/mo/q/m/searchthread?word=%s&pn=0&rn=10" % kw)
    probe("贴吧-聚合搜索", "https://tieba.baidu.com/mo/q/search/thread?word=%s" % kw)
    probe("贴吧-帖子详情", "https://tieba.baidu.com/p/9000000000", mobile=False)

    # ---- 微博移动端 ----
    probe("微博-m搜索", "https://m.weibo.cn/api/container/getIndex?containerid=100103type%%3D1%%26q%%3D%s&page_type=searchall" % kw)
    probe("微博-评论", "https://m.weibo.cn/comments/hotFlow?id=4700000000000000&mid=4700000000000000&max_id_type=0")

    # ---- 其他中文社区 ----
    probe("什么值得买", "https://search.smzdm.com/?c=home&s=%s&v=b" % kw, mobile=False)
    probe("虎扑搜索", "https://www.hupu.com/search?q=%s" % kw, mobile=False)
    probe("豆瓣小组搜索", "https://www.douban.com/search?q=%s" % kw, mobile=False)
    probe("知乎-移动端热榜", "https://api.zhihu.com/topstory/hot-list?limit=10")
