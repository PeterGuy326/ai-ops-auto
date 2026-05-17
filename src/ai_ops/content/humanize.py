"""文案人味化（反 AI 检测）。

目标：把 LLM 直出的"AI 味"文案改成更像真人写的，规避：
  - 小红书内部的"营销号/AI 文案"判定模型
  - 第三方 AI 检测器（GPTZero / 朱雀 / 万方 等）

底层逻辑（按重要性）：
  1. 句长方差 —— AI 句长非常均匀（标准差小），真人波动大
  2. 句式破坏 —— AI 高频用 "首先/其次/最后"、"不仅...而且..."、"总而言之"
  3. 标点偏好 —— AI 爱用破折号（—— / —）和全角分号；真人少用
  4. 口语化 —— 注入语气词（"真的"、"超"、"绝了"、"我跟你说"）、错别字风
  5. 反对称 —— AI 爱写"三个要点"、"三段排比"，主动打破

不要做的：
  - 不胡乱加 emoji（小红书风格的 emoji 由 prompts/platform_style 控制）
  - 不破坏链接、@用户、#标签
  - 不破坏代码块、引用块（虽然小红书没这些，长文有）

调用方：
  - content/generator.py 生成完原始 body 后，过一遍 humanize_for_xhs()
  - 也可在 publishers/xhs_camoufox.py 发布前最后一道净化
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

# —— AI 高频结构词及其口语化替换 —— #
_AI_TRANSITIONS = {
    "首先": ["先说一个", "我先讲", "开头"],
    "其次": ["然后", "接着", "再来"],
    "最后": ["最后嘛", "最后一个", "收个尾"],
    "此外": ["另外", "还有", "顺便提一句"],
    "总而言之": ["所以说", "归根到底", "讲白了"],
    "综上所述": ["这么一看", "讲白了", "你看下来就发现"],
    "值得注意的是": ["有一点要提", "我专门讲一下", "这里得多嘴一句"],
    "不仅": ["不止"],
    "而且": ["还"],
    "因此": ["所以"],
    "然而": ["但是", "不过", "可是"],
    "进而": ["然后"],
    "从而": ["这样就"],
}

# —— AI 偏爱标点 → 真人化替换 —— #
_AI_PUNCT = [
    ("——", "，"),   # 长破折号 → 逗号
    ("—", ""),      # 短破折号 → 删除
    ("；", "。"),    # 全角分号 → 句号
    ("·", "·"),     # 全角点（保留）
]

# —— 注入口语化 hook（按句频概率插入） —— #
_FILLERS_HEAD = ["真的，", "说实话，", "我跟你讲，", "讲白了，", "", "", "", ""]   # 大概率空
_FILLERS_TAIL = ["真的", "真的是", "你别说", "就这样", "", "", "", "", ""]
_EMPHASIS = {
    r"很好": ["超棒", "好得离谱", "顶呱呱"],
    r"非常": ["超", "巨", "贼"],
    r"特别": ["超级", "巨"],
    r"重要": ["关键", "顶重要", "顶要紧"],
    r"建议": ["推荐", "我建议"],
}

# —— 受保护的片段（不改）—— #
_PROTECT_PATTERNS = [
    (re.compile(r"```.*?```", re.DOTALL), "CODE"),     # 代码块
    (re.compile(r"`[^`]+`"), "INLINE_CODE"),
    (re.compile(r"https?://\S+"), "URL"),
    (re.compile(r"#[^\s#]{1,30}"), "TAG"),               # 小红书 hashtag
    (re.compile(r"@[\w\-_]{1,30}"), "MENTION"),
    (re.compile(r"!\[.*?\]\(.*?\)"), "IMAGE"),
    (re.compile(r"\[.*?\]\(.*?\)"), "LINK"),
]


@dataclass(slots=True)
class HumanizeOptions:
    transition_replace_prob: float = 0.85  # AI 转折词替换概率
    filler_head_prob: float = 0.18         # 句首注入口语词概率
    filler_tail_prob: float = 0.10
    emphasis_prob: float = 0.5             # 程度词加强概率
    typo_prob: float = 0.0                 # 错别字概率（默认关，太冒险）
    short_sentence_split_prob: float = 0.25  # 长句拆短概率
    seed: int | None = None


def _protect(text: str) -> tuple[str, dict[str, str]]:
    """把受保护片段替换成占位符，避免后续 regex 误伤。"""
    placeholders: dict[str, str] = {}
    counter = 0
    for pat, tag in _PROTECT_PATTERNS:
        def repl(m: re.Match) -> str:
            nonlocal counter
            key = f"__PROTECT_{tag}_{counter}__"
            placeholders[key] = m.group(0)
            counter += 1
            return key
        text = pat.sub(repl, text)
    return text, placeholders


def _restore(text: str, placeholders: dict[str, str]) -> str:
    for k, v in placeholders.items():
        text = text.replace(k, v)
    return text


def _replace_transitions(text: str, rng: random.Random, prob: float) -> str:
    for ai_word, alts in _AI_TRANSITIONS.items():
        if ai_word not in text:
            continue
        # 逐次替换，但保留一定概率不动
        def repl(_m: re.Match) -> str:
            return rng.choice(alts) if rng.random() < prob else ai_word
        text = re.sub(re.escape(ai_word), repl, text)
    return text


def _strip_ai_punct(text: str) -> str:
    for src, dst in _AI_PUNCT:
        text = text.replace(src, dst)
    return text


def _apply_emphasis(text: str, rng: random.Random, prob: float) -> str:
    for pat, alts in _EMPHASIS.items():
        if not re.search(pat, text):
            continue
        def repl(_m: re.Match) -> str:
            return rng.choice(alts) if rng.random() < prob else _m.group(0)
        text = re.sub(pat, repl, text)
    return text


def _split_long_sentences(text: str, rng: random.Random, prob: float) -> str:
    """把过长（>40 字）的句子按逗号点处拆短。AI 倾向写长句，真人爱断句。"""
    out_parts: list[str] = []
    for para in text.split("\n"):
        if len(para) < 40:
            out_parts.append(para)
            continue
        sentences = re.split(r"(?<=[。！？])", para)
        new_sents = []
        for s in sentences:
            if len(s) > 40 and rng.random() < prob:
                # 在第一个逗号处一刀切
                idx = s.find("，", 15)
                if 15 < idx < len(s) - 5:
                    new_sents.append(s[:idx] + "。")
                    new_sents.append(s[idx + 1:])
                    continue
            new_sents.append(s)
        out_parts.append("".join(new_sents))
    return "\n".join(out_parts)


def _inject_fillers(text: str, rng: random.Random, head_p: float, tail_p: float) -> str:
    """按句首/句尾概率注入口语化 filler。一段最多注入 1 次。"""
    paragraphs = text.split("\n")
    out_paras = []
    for para in paragraphs:
        if not para.strip():
            out_paras.append(para)
            continue
        new_para = para
        if rng.random() < head_p:
            new_para = rng.choice(_FILLERS_HEAD) + new_para
        if rng.random() < tail_p:
            # 在最后一个 。/！/？ 前插入
            m = re.search(r"[。！？]\s*$", new_para)
            tail = rng.choice(_FILLERS_TAIL)
            if tail:
                if m:
                    new_para = new_para[:m.start()] + tail + new_para[m.start():]
                else:
                    new_para = new_para + tail
        out_paras.append(new_para)
    return "\n".join(out_paras)


def humanize_for_xhs(
    text: str,
    options: HumanizeOptions | None = None,
) -> str:
    """对小红书文案做人味化处理。幂等性：多次跑结果会进一步发散（不是稳态）。

    推荐只跑一次。如果想稳定可复现，传 options.seed。
    """
    if not text:
        return text
    opt = options or HumanizeOptions()
    rng = random.Random(opt.seed)

    protected, placeholders = _protect(text)
    out = protected
    out = _replace_transitions(out, rng, opt.transition_replace_prob)
    out = _strip_ai_punct(out)
    out = _apply_emphasis(out, rng, opt.emphasis_prob)
    out = _split_long_sentences(out, rng, opt.short_sentence_split_prob)
    out = _inject_fillers(out, rng, opt.filler_head_prob, opt.filler_tail_prob)
    out = _restore(out, placeholders)
    return out


def ai_smell_score(text: str) -> float:
    """快速 AI 味自检（0-1，越高越像 AI）。给生成器/CI 用。

    评分维度（粗略，仅做经验性提示，不是真实 AI 检测）：
      - 高频转折词密度
      - 句长方差（低 = 可疑）
      - 破折号/全角分号出现频率
      - 排比对称结构（"三...，三...，三..."）
    """
    if not text or len(text) < 20:
        return 0.0
    text_len = len(text)
    score = 0.0

    # 转折词密度
    trans_hits = sum(text.count(w) for w in _AI_TRANSITIONS)
    score += min(0.3, trans_hits / max(text_len / 200, 1) * 0.3)

    # 破折号/全角分号
    bad_punct = text.count("——") + text.count("；")
    score += min(0.2, bad_punct * 0.05)

    # 句长方差（AI 倾向均匀长度）
    sentences = [s for s in re.split(r"[。！？\n]", text) if len(s.strip()) > 3]
    if len(sentences) >= 3:
        lens = [len(s) for s in sentences]
        mean = sum(lens) / len(lens)
        var = sum((l - mean) ** 2 for l in lens) / len(lens)
        std = var ** 0.5
        # 标准差 < 8 字判定可疑
        if std < 8:
            score += 0.25 * (1 - std / 8)

    # 排比对称（连续两段开头同字）
    paras = [p for p in text.split("\n") if p.strip()]
    if len(paras) >= 3:
        starts = [p[:2] for p in paras]
        dup = len(starts) - len(set(starts))
        score += min(0.25, dup * 0.08)

    return min(1.0, score)
