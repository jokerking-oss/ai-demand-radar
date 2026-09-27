# -*- coding: utf-8 -*-
"""大模型客户端（OpenAI 兼容协议，默认 DeepSeek）+ 无 key 时的规则兜底。"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import api_key, g

# ---------------------------------------------------------------------------
# 核心提示词：从「看起来很平常的话」里挖需求
#
# 设计原则：不要去筛，要去懂。
#   显性痛点（"太难用了"）人人都能看到，竞争价值最低。
#   真正值钱的是用户顺口说出来、自己都没意识到那是痛的那些话。
#   所以这里喂给模型的是「所有和主题相关的发言」，而不是「先筛过的抱怨」。
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一名资深用户研究员，擅长从毫无情绪的日常对话里读出未被满足的需求。

【你的工作】
给你一批社交平台上关于某个主题的真实发言。它们大多是**平平常常的话**——不是投诉，不是求助，甚至可能是在夸。
你的任务不是判断"这句话像不像投诉"，而是：

> 从用户的日常描述里，反推出**他真实的生活/工作流长什么样**，以及**其中哪个环节本可以被产品做到更好**。

【最关键的一条判断标准】
用户通常会把妥协说成习惯。"我一般先 AI 生成一遍再自己改两小时"——他没在抱怨，
但这句话暴露了"AI 不懂我的风格/上下文延续不了"这个真实缺口。
**凡是用户为了迁就产品而改变自己行为的动作，都是需求。**

【具体要抓的四类信号】
1. **返工 / 拼接**：用了工具还得自己修补、重试、换个问法，才能让结果可以用 → 需求是让它一次做对
2. **绕路**：在某个不该手工的地方手工操作、在两个工具间来回倒数据 → 需求是打通或自动化
3. **迁就/自我规训**：限定某个时段才敢用、把任务拆碎、刻意换个说法哄模型 → 需求是去掉限制
4. **对比与归因**：和另一个工具/另一种方式对比、解释自己为什么换或为什么不换 → 需求是差在哪一点

【显式 vs 隐式】
- `implicit` = 0：**用户自己说出来了**（明确抱怨、明确说"要是能…就好了"、明确求推荐）
- `implicit` = 1：**你从他的话里分析出来的**，用户本人未必意识到这是可以被解决的
**隐式需求价值更高，但你必须能用 reasoning 说清楚推导过程，不许凭空编造。**

【输出】严格 JSON 数组，不要代码块，不要解释文字。每条发言一个对象：
{
  "index": 0,
  "has_insight": true,
  "demands": [
    {
      "demand": "≤24字，具体可执行的需求，用户视角。禁止空话（不要出现「体验不佳」「需要优化」「用户不满」这类）",
      "implicit": 1,
      "reasoning": "≤40字，说清楚从原文的哪个动作/说法推出了这个需求，一定引用到原文里的具体细节",
      "category": "从【价格/功能/性能/易用性/稳定性/内容质量/隐私合规/服务支持/生态兼容/工作流】选一个",
      "scene": "≤12字，发生在什么环节，如「写周报」「手机端」「团队协作」",
      "strength": 3,
      "product": "提到的具体产品名，没有就空字符串",
      "quote": "≤40字，原文原样摘录，必须能支撑上面的推理"
    }
  ]
}

【strength 打分｜1-5】
1 顺口一提，可能是小事 | 2 有实感但不强烈 | 3 明确影响效率或心情
4 已经为此改了自己的做法 | 5 愿意掏钱解决 / 已经在找方案

【规则】
1. 广告、引流、无意义灌水、纯表情 → has_insight=false, demands=[]
2. 一句话确实平平无奇、读不出任何缺口 → 老实写 false。**不许为了凑数硬编**，假需求比没需求更害人
3. 每条最多 3 个需求，不要把同一件事换三种说法列三遍
4. 如果发言在夸某个产品，想一想：他夸的这个点，恰恰说明别的产品在这点上普遍不行 → 这也可以是需求
5. 必须是合法 JSON

【示例】
原文：「我现在写周报都先用AI生成一遍，然后自己改，改的时间跟重写差不多」
→ {"demand":"希望AI记住我的写作风格与常用术语，一次生成即可用","implicit":1,
   "reasoning":"用户已接受AI参与流程，但返工时间≈重写，缺的是风格延续而非生成能力",
   "category":"内容质量","scene":"写周报","strength":4,"product":"","quote":"然后自己改，改的时间跟重写差不多"}

原文：「这个功能我妈都会用了」
→ {"demand":"面向长辈的零学习成本入口，无需理解提示词","implicit":1,
   "reasoning":"把长辈会用当作褒义来讲，说明易用性是不成文的硬门槛","category":"易用性",
   "scene":"家庭场景","strength":3,"product":"","quote":"这个功能我妈都会用了"}

原文：「我一般晚上才用它，白天在公司电脑上不太敢装」
→ {"demand":"企业认可的合规版本或私有化部署","implicit":1,
   "reasoning":"使用时段是被环境逼出来的妥协，暴露的是合规顾虑","category":"隐私合规",
   "scene":"公司办公","strength":4,"product":"","quote":"白天在公司电脑上不太敢装"}

原文：「太贵了，一个月两百块我用不了几次」
→ {"demand":"希望按次或按量付费，不为低频使用付月费","implicit":0,
   "reasoning":"用户直接抱怨价格与使用频次不匹配","category":"价格","scene":"个人订阅",
   "strength":4,"product":"","quote":"太贵了，一个月两百块我用不了几次"}
"""


def _strip_fences(text: str) -> str:
    """去掉 ```json 代码块围栏，并把首尾多余的文字掐掉。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _extract_json_array(text: str) -> list | None:
    """从任意返回体里捞出「最像答案」的那个 JSON 数组。

    为什么不能简单截取第一个 [ 到最后一个 ]：
    推理型模型偶尔会把思考过程写进正式回答，而它思考时举例写的假 JSON
    （比如 `[{"index":0,"canonical":"..."}]`）也会被当成答案截出来。
    所以这里把所有候选数组都试解析一遍，取条目最多的那个。
    """
    t = _strip_fences(text)
    cands: list[list] = []
    depth, start = 0, -1
    instr = False
    esc = False
    for i, ch in enumerate(t):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        v = json.loads(t[start:i + 1])
                        if isinstance(v, list):
                            cands.append(v)
                    except Exception:
                        pass
                    start = -1
    if not cands:
        return None
    # 优先选条目最多、且内容是对象的那个数组
    best = max(cands, key=lambda v: (sum(1 for x in v if isinstance(x, dict)), len(v)))
    return best if sum(1 for x in best if isinstance(x, dict)) else max(cands, key=len)


def load_json_array(text: str) -> list:
    """拿到数组；拿不到就按旧逻辑退化，让调用方的异常分支接管。"""
    arr = _extract_json_array(text)
    if arr is not None:
        return arr
    t = _strip_fences(text)
    try:
        v = json.loads(t)
        return v if isinstance(v, list) else [v]
    except Exception:
        return json.loads(t)


class LLMClient:
    def __init__(self, cfg: dict, log=print):
        self.cfg = cfg
        self.log = log
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self._lock = threading.Lock()
        self._client = None
        key = api_key(cfg)
        if key:
            try:
                from openai import OpenAI
                self._client = OpenAI(
                    api_key=key,
                    base_url=g(cfg, "llm.base_url", "https://api.deepseek.com"),
                    timeout=g(cfg, "llm.timeout", 90),
                )
            except Exception as e:
                self.log(f"[LLM] 客户端初始化失败: {e}")
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def _chat(self, messages: list[dict], max_tokens: int = 3072) -> str:
        resp = self._client.chat.completions.create(
            model=g(self.cfg, "llm.model", "deepseek-chat"),
            messages=messages,
            temperature=g(self.cfg, "llm.temperature", 0.2),
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            with self._lock:
                self.tokens_in += int(getattr(usage, "prompt_tokens", 0) or 0)
                self.tokens_out += int(getattr(usage, "completion_tokens", 0) or 0)
        return resp.choices[0].message.content or ""

    # --------------- 需求抽取 ---------------
    def extract_batch(self, batch: list[dict]) -> dict[int, list[dict]]:
        """batch: [{index, text, platform, keyword}] -> {index: [demand...]}"""
        topic = (self.cfg.get("topic", {}) or {}).get("name", "该主题")
        payload = "\n".join(
            f"[{b['index']}] 来源={b.get('platform','')} 关键词={b.get('keyword','')}\n{b['text']}"
            for b in batch
        )
        user = (
            f"主题：{topic}\n\n下面是 {len(batch)} 条用户发言，逐条分析：\n\n{payload}\n\n"
            f"请为每条输出一项，共输出 {len(batch)} 个对象的 JSON 数组。"
            f"记住：重点找 implicit=1 的隐性需求——用户自己没说出口的那个。"
        )
        with self._lock:
            self.calls += 1
        for attempt in range(2):
            try:
                raw = self._chat([{"role": "system", "content": SYSTEM_PROMPT},
                                  {"role": "user", "content": user}])
                data = load_json_array(raw)
                out: dict[int, list[dict]] = {}
                if isinstance(data, dict):
                    data = data.get("results") or data.get("data") or []
                for obj in data:
                    if not isinstance(obj, dict):
                        continue
                    i = obj.get("index")
                    if i is None or not obj.get("has_insight", True):
                        continue
                    ds = obj.get("demands") or []
                    clean = []
                    for d in ds:
                        dem = (d.get("demand") or "").strip()
                        if not dem or len(dem) > 60:
                            continue
                        clean.append({
                            "demand": dem,
                            "implicit": 1 if d.get("implicit") in (1, True, "1", "true") else 0,
                            "reasoning": (d.get("reasoning") or "").strip()[:120],
                            "category": (d.get("category") or "").strip()[:20],
                            "scene": (d.get("scene") or "").strip()[:24],
                            "pain_score": int(d.get("strength") or d.get("pain_score") or 3),
                            "product": (d.get("product") or "").strip()[:40],
                            "quote": (d.get("quote") or "").strip()[:120],
                        })
                    if clean:
                        out[int(i)] = clean[:3]
                return out
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                self.log(f"  [LLM] 批次抽取失败: {type(e).__name__} {str(e)[:100]}")
        return {}

    def extract_many(self, items: list[dict], budget: int | None = None) -> dict[int, list[dict]]:
        if budget is None:
            budget = g(self.cfg, "llm.max_calls_per_round", 80)
        size = int(g(self.cfg, "llm.batch_size", 8))
        workers = int(g(self.cfg, "llm.max_workers", 4))
        batches = [items[i:i + size] for i in range(0, len(items), size)][:budget]

        result: dict[int, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self.extract_batch, b) for b in batches]
            for f in as_completed(futures):
                try:
                    result.update(f.result())
                except Exception as e:
                    self.log(f"  [LLM] 并发任务异常: {e}")
        return result

    # --------------- 需求合并：把新说法归到已有簇 ---------------
    def canonicalize(self, phrases: list[str]) -> list[str]:
        """把一批需求说法改写成统一的标准短语（≤20字），便于聚合计数。"""
        if not phrases:
            return []
        # 输出格式必须是「序号=标签」的纯文本行，不能要 JSON。
        # 实测：同一批 10 条，JSON 格式会让推理型模型陷入长思考（30 秒还在数字数、
        # 最后 finish_reason=length 什么都没输出）；换成行格式 3 秒就收工。
        payload = "\n".join(f"{i}. {p}" for i, p in enumerate(phrases))
        sys = (
            f"下面 {len(phrases)} 条是从用户评论里挖出来的需求短语，很多指同一件事，只是说法不同。\n"
            "把表达同一需求的说法统一成**完全相同**的标准短语。\n"
            "标准短语：≤20个汉字、具体、不带标点、同一件事只能有一种写法。\n"
            "有些输入是残缺的半截句子（尤其以「希望解决：」开头的），那是低质量残留，\n"
            "尽量把它归入语义最近的标准短语；实在读不懂就原样保留。\n"
            f"严格只输出 {len(phrases)} 行，每行格式「序号=标准短语」，不要任何解释、不要 JSON、不要代码块。"
        )
        return self._canonicalize_once(sys, payload, phrases, max_tokens=max(1200, len(phrases) * 120))

    @staticmethod
    def _parse_lines(text: str, n: int) -> dict[int, str]:
        """解析「0=标签」形式的输出。容忍中文冒号、空格、代码块围栏。"""
        out: dict[int, str] = {}
        for line in (text or "").splitlines():
            line = line.strip().strip("`").strip()
            if line.startswith("-") or line.startswith("*"):
                line = line[1:].strip()
            m = re.match(r"^(\d+)\s*[=＝:：]\s*(.+)$", line)
            if not m:
                continue
            idx = int(m.group(1))
            label = m.group(2).strip().strip('"\'“”').strip()
            if label and idx not in out:
                out[idx] = label[:40]
        return out

    def _canonicalize_once(self, sys: str, payload: str, phrases: list[str], max_tokens: int) -> list[str]:
        """单次归并。失败时二分重试，避免一整批全部退回原样。"""
        try:
            raw = self._chat([{"role": "system", "content": sys},
                              {"role": "user", "content": payload}], max_tokens=max_tokens)
            mapping = self._parse_lines(raw, len(phrases))
            # 覆盖不到一半就当失败——这通常意味着输出被截断或模型跑偏了
            if len(mapping) < max(1, len(phrases) // 2):
                raise ValueError(f"归并结果不全：只拿到 {len(mapping)}/{len(phrases)} 条")
            return [mapping.get(i, phrases[i][:40]) for i in range(len(phrases))]
        except Exception as e:
            if len(phrases) > 4:
                mid = len(phrases) // 2
                a = self._canonicalize_once(sys, "\n".join(
                    f"{i}. {p}" for i, p in enumerate(phrases[:mid])), phrases[:mid], max_tokens // 2 + 800)
                b = self._canonicalize_once(sys, "\n".join(
                    f"{i}. {p}" for i, p in enumerate(phrases[mid:])), phrases[mid:], max_tokens // 2 + 800)
                return a + b
            self.log(f"  [LLM] 归并失败: {type(e).__name__} {str(e)[:100]}")
            return [p[:40] for p in phrases]


# ---------------------------------------------------------------------------
# 规则兜底抽取器：没有 API key 时的降级方案
# 注意：它只能抓到「说得很直白」的那类需求，抓不到真正的隐性需求。
# ---------------------------------------------------------------------------
_RULE_PATTERNS = [
    (r"(?:希望|想要|要是|如果)\s*(?:可以|能|有个|有)\s*([^。！？\n]{4,40})", "功能", 0),
    (r"(?:求|找)\s*(?:推荐|安利|平替|替代品)\s*([^。！？\n]{0,30})", "生态兼容", 0),
    (r"(.{0,20}?)(?:太难用|不好用|用不了|不会用|难用)", "易用性", 0),
    (r"(.{0,20}?)(?:太贵|收费贵|价格贵|买不起|用不起)", "价格", 0),
    (r"(.{0,20}?)(?:卡顿|卡死|闪退|崩了|很卡|bug|报错)", "稳定性", 0),
    (r"(.{0,20}?)(?:效果差|不准|不智能|识别不出)", "内容质量", 0),
    # 以下是「隐性信号」的粗糙近似：用户描述自己在绕路 / 返工
    (r"(.{0,16}?)(?:还得自己|还要手动|然后再|再自己改|手动一遍)", "工作流", 1),
    (r"(.{0,16}?)(?:来回切换|导来导去|复制粘贴|来回倒)", "生态兼容", 1),
    (r"(.{0,16}?)(?:不敢用|只能晚上|偷偷|怕被|不太敢)", "隐私合规", 1),
]

_SPLIT = re.compile(r"[。！？!?；;\n]")


def rule_extract(text: str, hints: list[str]) -> list[dict]:
    """兜底抽取。标注 extractor=rule，implicit 只作粗略猜测。"""
    text = (text or "").strip()
    if not text:
        return []
    out: list[dict] = []
    for sentence in _SPLIT.split(text):
        s = sentence.strip()
        if len(s) < 6:
            continue
        for pat, cat, implicit in _RULE_PATTERNS:
            m = re.search(pat, s)
            if not m:
                continue
            core = (m.group(1) or "").strip()
            demand = (core if core else s)[:40].strip()
            if len(demand) < 4:
                demand = s[:40]
            out.append({
                "demand": f"希望解决：{demand}" if len(demand) < 30 else demand,
                "implicit": implicit,
                "reasoning": "（规则兜底，未经推理，仅作占位）",
                "category": cat,
                "scene": "",
                "pain_score": 3,
                "product": "",
                "quote": s[:120],
            })
            break
        if len(out) >= 3:
            break
    return out
