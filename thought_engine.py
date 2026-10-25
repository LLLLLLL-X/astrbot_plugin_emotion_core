"""念头引擎：把"九维驱动"翻译成"她现在想要什么"，被满足时结算心情。

纯函数模块，不依赖 AstrBot，方便单独测试。

念头（v2）在 state.json 里是这样一个 dict：
    {"kind": "praise", "text": "想让他夸一句", "weight": 0.8,
     "created": 1710000000.0, "expires": 1710010800.0,
     "pending": True, "attempts": 0, "mood": ""}

生命周期：
    spawn  → 驱动没被满足，长出一个"想要"
    live   → 注入进 system prompt，影响说话方式；主动开口时尝试一次
    settle → 用户的话/事件把那个"想要"喂饱了 → pending=False，写入 mood
    fade   → 一直没被喂，到点凉掉 → 对应驱动轻微回落（release）
"""

from __future__ import annotations

import random
import time
from typing import Any

DRIVES = (
    "attachment", "curiosity", "reflection", "duty", "social",
    "fatigue", "intimacy", "stress", "joy",
)

# 念头的存活时长（秒）：越"贴身"的念头留得越久
TTL = {
    "praise": 3 * 3600,
    "miss": 4 * 3600,
    "hug": 4 * 3600,
    "talk": 2.5 * 3600,
    "ask_day": 2 * 3600,
    "task": 4 * 3600,
    "quiet": 1.5 * 3600,
    "dwell": 1.2 * 3600,
}

# 旧版（v1）固定的五条念头池 → 新版 kind，迁移时用
OLD_TEXT_TO_KIND = {
    "想问他今天吃了什么": "ask_day",
    "想让他夸一句": "praise",
    "想知道他有没有想我": "miss",
    "想找点事做": "task",
    "想安静一会儿": "quiet",
}


def _g(drives: dict, key: str) -> float:
    return float(drives.get(key, 50.0))


def _calm(mode: str) -> bool:
    return mode == "calm"


KIND_META: dict[str, dict] = {
    "praise": {
        "label": "夸夸",
        "text": "想让他夸一句",
        "hint": "想被夸一句，想被看见",
        "done": "被他夸了，心里甜甜的",
        "reward": {"joy": 6, "intimacy": 4, "attachment": 2},
        "release": {"joy": -1},
        "events": ("praise",),
        "trigger": lambda d, e, w: (
            1.0 if _g(d, "attachment") >= 40 and _g(d, "joy") < 78 else 0.0
        ),
    },
    "miss": {
        "label": "想他",
        "text": "想知道他有没有想我",
        "hint": "想知道他有没有想你",
        "done": "听他说想我了，悬着的心放下了",
        "reward": {"attachment": 5, "intimacy": 3, "expectation": -100},
        "release": {"attachment": -2, "expectation": 1},
        "events": ("seeking",),
        "trigger": lambda d, e, w: (
            1.2 if _g(d, "attachment") >= 58 and float(e) >= 15 else 0.0
        ),
    },
    "hug": {
        "label": "抱抱",
        "text": "想要抱抱",
        "hint": "有点想被抱住，想撒个娇",
        "done": "被抱了一下，整个人都松了",
        "reward": {"stress": -8, "attachment": 4, "intimacy": 3, "joy": 3},
        "release": {"attachment": -1},
        "events": ("comfort",),
        "trigger": lambda d, e, w: (
            1.1
            if (_g(d, "stress") >= 55 or _g(d, "attachment") >= 72)
            and _g(d, "fatigue") < 72
            else 0.0
        ),
    },
    "talk": {
        "label": "想讲",
        "text": "想把今天的事讲给他听",
        "hint": "攒了些话想讲给他听",
        "done": "跟他说完了，心里轻松",
        "reward": {"social": 5, "joy": 4, "intimacy": 2},
        "release": {"social": -3},
        "events": ("user_msg",),
        "trigger": lambda d, e, w: (
            0.9 if _g(d, "social") >= 58 and _g(d, "joy") >= 45 else 0.0
        ),
    },
    "ask_day": {
        "label": "问他",
        "text": "想问他今天过得怎么样",
        "hint": "想问问你那边今天怎么样",
        "done": "知道他今天还好好的",
        "reward": {"expectation": -100, "social": 3},
        "release": {"social": -1},
        "events": ("user_msg",),
        "trigger": lambda d, e, w: (
            0.8 if _g(d, "social") >= 45 and float(e) >= 10 else 0.0
        ),
    },
    "task": {
        "label": "想做事",
        "text": "想找点事做",
        "hint": "想找点能帮上忙的事做",
        "done": "帮上忙了，有点小得意",
        "reward": {"duty": 5, "joy": 3},
        "release": {"duty": -2},
        "events": ("task",),
        "trigger": lambda d, e, w: (
            0.9 if _g(d, "duty") >= 55 and _g(d, "fatigue") < 65 else 0.0
        ),
    },
    "quiet": {
        "label": "安静",
        "text": "想安静一会儿",
        "hint": "有点累，想安静一会儿，话可以少一点",
        "done": "安静地待了会儿，缓过来了",
        "reward": {"stress": -6, "fatigue": -4},
        "release": {"fatigue": -1},
        "events": ("quiet",),
        "trigger": lambda d, e, w: (
            1.0 if _g(d, "fatigue") >= 60 or _g(d, "stress") >= 50 else 0.0
        ),
    },
    "dwell": {
        "label": "等回话",
        "text": "在等他回话",
        "hint": "在等你回话，忍不住总看手机",
        "done": "他回我了，心里踏实了",
        "reward": {"expectation": -60, "joy": 2},
        "release": {"expectation": -3},
        "events": ("reply_fast",),
        "trigger": lambda d, e, w: (
            1.0 if w or float(e) >= 40 else 0.0
        ),
    },
}

# 从用户原话里直接反推"喂饱了哪个念头"（比事件名细）
# 顺序即优先级：先匹配到的生效
KEYWORD_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("miss", ("想你", "想我", "有没有想我", "惦记")),
    ("hug", ("抱", "贴贴", "搂", "亲亲", "撒娇")),
    ("praise", ("厉害", "真棒", "谢谢", "可爱", "喜欢你", "爱你", "真好", "优秀", "夸")),
    ("ask_day", ("过得", "怎么样", "怎么样呀", "顺利", "累不累", "还好吗")),
    ("task", ("帮我", "给你", "安排", "做点", "需要我", "帮个忙")),
    ("talk", ("今天", "跟你说", "告诉你", "我跟你讲")),
    ("quiet", ("歇", "休息", "睡", "安静", "别忙了")),
)


def match_kind(text: str) -> str | None:
    """从用户一句话里反推她想被喂饱的念头。"""
    t = (text or "").strip()
    if not t:
        return None
    for kind, kws in KEYWORD_KINDS:
        if any(k in t for k in kws):
            return kind
    return None


def new_thought(kind: str, now: float | None = None, weight: float = 0.0) -> dict | None:
    meta = KIND_META.get(kind)
    if not meta:
        return None
    now = time.time() if now is None else now
    return {
        "kind": kind,
        "text": meta["text"],
        "weight": round(float(weight or 1.0), 2),
        "created": now,
        "expires": now + TTL.get(kind, 3 * 3600),
        "pending": True,
        "attempts": 0,
        "mood": "",
    }


def pick_kind(
    drives: dict,
    expectation: float,
    awaiting_reply: bool,
    exclude: tuple[str, ...] = (),
    rng: random.Random | None = None,
) -> tuple[str | None, float]:
    """按当下驱动状态加权抽一个念头；全都够不着时返回 (None, 0)。"""
    rng = rng or random
    cands: list[tuple[str, float]] = []
    for kind, meta in KIND_META.items():
        if kind in exclude:
            continue
        w = float(meta["trigger"](drives, expectation, awaiting_reply))
        if w > 0:
            cands.append((kind, w))
    if not cands:
        return None, 0.0
    total = sum(w for _, w in cands)
    r = rng.random() * total
    acc = 0.0
    for kind, w in cands:
        acc += w
        if r <= acc:
            return kind, w
    return cands[-1]


def active_kinds(thoughts: list[dict], now: float | None = None) -> tuple[str, ...]:
    now = time.time() if now is None else now
    return tuple(
        str(t.get("kind"))
        for t in thoughts or []
        if t.get("pending", True) and float(t.get("expires", 0)) > now
    )


def settle(
    thoughts: list[dict],
    events: list[str],
    text: str,
    mode: str,
    now: float | None = None,
) -> dict | None:
    """本轮最多喂饱一个念头，返回 {kind, text, mood, reward} 或 None。"""
    now = time.time() if now is None else now
    evset = set(events or [])
    by_text = match_kind(text)
    candidates = [
        t
        for t in thoughts or []
        if t.get("pending", True) and float(t.get("expires", 0)) > now
    ]
    if not candidates:
        return None

    def hit(t: dict) -> bool:
        kind = str(t.get("kind"))
        meta = KIND_META.get(kind) or {}
        if by_text and by_text == kind:
            return True
        return bool(evset & set(meta.get("events") or ()))

    # 文本反推优先，其次按"最想要"（weight 高）的喂
    chosen = None
    soft = False
    if by_text:
        chosen = next((t for t in candidates if t.get("kind") == by_text), None)
    if chosen is None:
        hits = [t for t in candidates if hit(t)]
        if hits:
            chosen = max(hits, key=lambda t: float(t.get("weight", 0)))
    if chosen is None:
        # 她开口后他回了：哪怕没对上具体内容，"被听见了"也算半个满足
        if "reply_fast" in evset:
            attempted = [
                t for t in candidates if int(t.get("attempts", 0)) >= 1
            ]
            if attempted:
                chosen = max(attempted, key=lambda t: float(t.get("weight", 0)))
                soft = True
    if chosen is None:
        return None

    kind = str(chosen.get("kind"))
    meta = KIND_META[kind]
    chosen["pending"] = False
    chosen["mood"] = meta["done"]
    chosen["expires"] = now + 2 * 3600
    reward = dict(meta.get("reward") or {})
    if soft:
        reward = {k: round(float(v) * 0.5, 1) for k, v in reward.items()}
    if _calm(mode):
        reward["joy"] = float(reward.get("joy", 0)) + 2
        reward["intimacy"] = float(reward.get("intimacy", 0)) + 1
    return {"kind": kind, "text": meta["text"], "mood": meta["done"], "reward": reward}


def apply_reward(state: dict, payload: dict) -> None:
    """把 settle 的奖励落到 state 上（clamp 0-100）。"""
    drives = state.setdefault("drives", {})
    for key, dv in (payload.get("reward") or {}).items():
        if key == "expectation":
            state["expectation"] = max(
                0.0, min(100.0, float(state.get("expectation", 0.0)) + float(dv))
            )
        elif key in DRIVES:
            drives[key] = max(0.0, min(100.0, float(drives.get(key, 50.0)) + float(dv)))
    state["last_reward"] = {
        "kind": payload.get("kind"),
        "mood": payload.get("mood"),
        "ts": time.time(),
    }


def note_attempt(thoughts: list[dict], now: float | None = None) -> bool:
    """主动开口时记一次尝试；连续开口没被接住就放弃这条念头。"""
    now = time.time() if now is None else now
    for t in thoughts or []:
        if not t.get("pending", True):
            continue
        if float(t.get("expires", 0)) <= now:
            continue
        t["attempts"] = int(t.get("attempts", 0)) + 1
        if int(t["attempts"]) >= 3:
            # 喊了三次没人应，就不再惦记了
            t["pending"] = False
            t["mood"] = "算了，先不想了"
            t["expires"] = now + 1800
        return True
    return False


def take_expired(
    thoughts: list[dict], now: float | None = None
) -> tuple[list[dict], list[dict]]:
    """返回 (过期未满足的[需淡出], 过期已满足的[直接删])；不修改原列表。"""
    now = time.time() if now is None else now
    fading: list[dict] = []
    dropped: list[dict] = []
    for t in thoughts or []:
        if float(t.get("expires", 0)) > now:
            continue
        if t.get("pending", True):
            fading.append(t)
        else:
            dropped.append(t)
    return fading, dropped


def release_effect(thought: dict) -> dict:
    meta = KIND_META.get(str(thought.get("kind"))) or {}
    return dict(meta.get("release") or {})


def auto_complete(thought: dict, drives: dict) -> str | None:
    """有些念头不是"他做了某事"才满足的，而是状态自己缓过来。"""
    kind = str(thought.get("kind"))
    if kind == "quiet" and _g(drives, "fatigue") <= 45 and _g(drives, "stress") <= 35:
        return "安静了会儿，缓过来了"
    if kind == "task" and _g(drives, "duty") <= 40:
        return "事儿做完啦，心里踏实"
    return None


def pending_hint(thoughts: list[dict], now: float | None = None) -> str:
    """给 system prompt 用的、当前最该惦记的一条。"""
    now = time.time() if now is None else now
    live = [
        t
        for t in thoughts or []
        if t.get("pending", True) and float(t.get("expires", 0)) > now
    ]
    if not live:
        return ""
    best = max(live, key=lambda t: float(t.get("weight", 0)))
    meta = KIND_META.get(str(best.get("kind"))) or {}
    return str(meta.get("hint") or "")


def pending_text(thoughts: list[dict], now: float | None = None) -> str:
    now = time.time() if now is None else now
    live = [
        t
        for t in thoughts or []
        if t.get("pending", True) and float(t.get("expires", 0)) > now
    ]
    if not live:
        return ""
    best = max(live, key=lambda t: float(t.get("weight", 0)))
    return str(best.get("text") or "")


def summarize(thoughts: list[dict], now: float | None = None, limit: int = 2) -> list[str]:
    """状态行用的摘要：未满足的"念头" + 已满足的"心情"。"""
    now = time.time() if now is None else now
    live = [
        t
        for t in thoughts or []
        if float(t.get("expires", 0)) > now
    ]
    pending = [t for t in live if t.get("pending", True)]
    done = [t for t in live if not t.get("pending", True)]
    out: list[str] = []
    if pending:
        out.append("念头：" + "；".join(str(t.get("text")) for t in pending[-limit:]))
    if done:
        out.append("心情：" + "；".join(str(t.get("mood")) for t in done[-limit:]))
    return out


def migrate(raw: list[Any]) -> list[dict]:
    """v1 → v2：老念头只有 text/weight/expires，补 kind 与生命周期字段。"""
    out: list[dict] = []
    now = time.time()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind not in KIND_META:
            kind = OLD_TEXT_TO_KIND.get(str(item.get("text") or ""), "")
            if not kind:
                continue
        t = new_thought(kind, now=now, weight=item.get("weight", 0) or 1.0)
        if t is None:
            continue
        exp = float(item.get("expires", 0) or 0)
        if exp > now:
            t["expires"] = exp
        t["created"] = float(item.get("created", now) or now)
        # v1 里的念头一律视为"还没被喂饱"
        t["pending"] = True
        t["mood"] = ""
        out.append(t)
    return out
