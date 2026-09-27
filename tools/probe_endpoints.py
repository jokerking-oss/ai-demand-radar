# -*- coding: utf-8 -*-
"""一次性探测脚本：确认各真实接口返回什么，用来决定采集器怎么写。"""
import json
import re
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      "Accept-Language": "zh-CN,zh;q=0.9"}


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    r = urllib.request.urlopen(req, timeout=timeout)
    raw = r.read()
    return r.status, raw.decode("utf-8", "ignore")


def probe(name, url):
    print("=" * 70)
    print(name, "|", url[:110])
    try:
        st, html = fetch(url)
        print("  status:", st, "len:", len(html))
        body = html[:600].replace("\n", " ")
        print("  head :", body[:400])
        hits = {
            "thread_title": len(re.findall(r'class="j_th_tit|thread_title|j_thread_list', html)),
            "reply_item": len(re.findall(r'class="l_post|reply_item|post_content', html)),
            "__NEXT_DATA__": len(re.findall(r"__NEXT_DATA__|__INITIAL_STATE__", html)),
            "bili_reply": len(re.findall(r'"replies"|"content":\{"message"', html)),
            "result_node": len(re.findall(r'result|c-container|b_algo', html)),
        }
        print("  marks:", {k: v for k, v in hits.items() if v})
        if '"replies"' in html or '"message"' in html:
            try:
                j = json.loads(html)
                print("  json :", json.dumps(j, ensure_ascii=False)[:400])
            except Exception:
                m = re.search(r'"message":\{"message":"(.{0,120}?)"\}', html)
                if m:
                    print("  msg  :", m.group(1))
    except Exception as e:
        print("  FAIL:", type(e).__name__, str(e)[:150])


if __name__ == "__main__":
    kw = urllib.parse.quote("AI工具")

    # ---------- 贴吧：找可用的搜索/列表入口 ----------
    probe("贴吧-吧内搜索(旧)", "https://tieba.baidu.com/f/search/res?ie=utf-8&qw=%s&rn=10&pn=1" % kw)
    probe("贴吧-全吧搜索", "https://tieba.baidu.com/search?ie=utf-8&q=%s" % kw)
    probe("贴吧-移动版搜索", "https://tiebac.baidu.com/mo/q/search?word=%s" % kw)
    probe("贴吧-单个吧", "https://tieba.baidu.com/f?kw=ai&ie=utf-8")

    # ---------- B站 ----------
    probe("B站-搜索页HTML", "https://search.bilibili.com/all?keyword=%s" % kw)
    probe("B站-评论 API(已知aid)", "https://api.bilibili.com/x/v2/reply?type=1&oid=170001&pn=1&ps=10&sort=1")

    # ---------- 搜索引擎 ----------
    probe("百度搜索", "https://www.baidu.com/s?wd=%s" % kw)
    probe("Bing搜索", "https://cn.bing.com/search?q=%s" % kw)
