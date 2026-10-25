"""情绪核心 (Emotion Core) —— 给角色一颗会变的心。

单文件插件，依赖仅 AstrBot 自带 API 与标准库。

设计原则
--------
1. **不建第二套记忆**：不写任何记忆库、不 import 其它插件模块，只在需要时
   通过公开接口读取"当前会话"和"人设"，绝不修改已有记忆数据。
2. **不抢优先级**：注入钩子用 priority=-10（AstrBot 按优先级降序执行），保证
   排在天使之心（40）与 TTS（0）之后，只做"追加"，不覆盖别人的内容。
3. **只在跨阈值时说话**：心境文本只在状态档位变化时注入，且两次注入至少间隔
   min_gap_minutes，避免每轮刷屏、白烧 token。
4. **主动说话走正常发送通道**：用 Context.send_message 直接出站（等效于走管线
   的成品消息），并可复用 TTS 插件的合成能力配音；不伪造 event 去骗管线。
5. **兜底优先**：任何外部依赖（LLM / TTS / 会话历史）失败都只降级、不抛错。

作者: LLL   版本: 1.0.0   许可: MIT
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.star_tools import StarTools

# 念头引擎：同目录独立文件（市场更新会覆盖 main.py，但引擎与状态会留下，重接几行即可）
def _load_thought_engine():
    try:
        import importlib.util as _ilu
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thought_engine.py")
        _spec = _ilu.spec_from_file_location("astrbot_plugin_emotion_core_thought_engine", _p)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        return _mod
    except Exception as _e:  # 引擎坏了也不影响情绪核心本体
        logger.warning("[情绪核心] 念头引擎加载失败: %s", _e)
        return None


engine = _load_thought_engine()


def _migrate_thoughts(raw: list) -> list:
    if not engine:
        return []
    try:
        return engine.migrate(raw)
    except Exception:
        return []


def _pending_hint(thoughts: list) -> str:
    if not engine:
        return ""
    try:
        return engine.pending_hint(thoughts)
    except Exception:
        return ""


def _thought_summary(thoughts: list) -> list:
    if not engine:
        return []
    try:
        return engine.summarize(thoughts)
    except Exception:
        return []


PLUGIN_NAME = "astrbot_plugin_emotion_core"
VERSION = "1.1.0"

# 注入钩子优先级：AstrBot 按优先级 **降序** 执行。
# 天使之心 = 40，TTS = 0，这里用 -10 表示最后一个执行（只追加、不覆盖）。
INJECT_PRIORITY = -10
# 读取 [EMO:xxx] 必须抢在 TTS 之前（TTS 用 priority=1 会把标记 strip 掉）。
EMO_READ_PRIORITY = 2

# --------------------------------------------------------------------------- #
# 一、常量与默认值
# --------------------------------------------------------------------------- #

DRIVES = (
    "attachment",   # 依恋：想不想他
    "curiosity",    # 好奇：想不想知道
    "reflection",   # 反刍：会不会反复想同一件事
    "duty",         # 责任：想不想做事
    "social",       # 社交：想不想说话
    "fatigue",      # 疲惫：累不累
    "intimacy",     # 亲密：身体/情绪上的亲近感
    "stress",       # 压力：紧张、不安
    "joy",          # 喜悦：开心程度
)

DRIVE_CN = {
    "attachment": "依恋",
    "curiosity": "好奇",
    "reflection": "反刍",
    "duty": "责任",
    "social": "社交",
    "fatigue": "疲惫",
    "intimacy": "亲密",
    "stress": "压力",
    "joy": "喜悦",
}

# 静息基线：所有驱动都会缓慢回到这里（不会永久卡在 100 或 0）
BASELINE = {
    "attachment": 45.0,
    "curiosity": 55.0,
    "reflection": 50.0,
    "duty": 50.0,
    "social": 45.0,
    "fatigue": 20.0,
    "intimacy": 40.0,
    "stress": 15.0,
    "joy": 50.0,
}

# 回落速度：慢变量 0.03，快变量（情绪类）0.05
DECAY_RATE = {
    "attachment": 0.03,
    "curiosity": 0.04,
    "reflection": 0.03,
    "duty": 0.03,
    "social": 0.04,
    "fatigue": 0.05,
    "intimacy": 0.03,
    "stress": 0.05,
    "joy": 0.05,
}

# 事件 → 驱动增量（"blame"/"expectation" 是两条快变量）
EVENT_EFFECTS = {
    # 他说话了（普通一条）
    "user_msg": {"social": 2.0, "joy": 1.0},
    # 她主动开口后，他 30 分钟内回了
    "reply_fast": {"attachment": 4.0, "stress": -5.0, "intimacy": 2.0,
                   "expectation": -100.0, "joy": 4.0},
    # 她主动开口后，6 小时没回
    "silent_6h": {"expectation": 8.0, "attachment": -1.0, "stress": 3.0},
    # 24 小时没回
    "silent_24h": {"stress": 10.0, "attachment": -3.0, "joy": -5.0},
    # 夸奖 / 感谢
    "praise": {"joy": 8.0, "intimacy": 4.0, "stress": -3.0, "attachment": 2.0},
    # 责怪三档
    "blame_mild": {"stress": 6.0, "blame": 10.0, "joy": -4.0},
    "blame_mid": {"stress": 12.0, "blame": 25.0, "joy": -8.0, "attachment": -2.0},
    "blame_hard": {"stress": 20.0, "blame": 40.0, "joy": -12.0, "attachment": -5.0},
    # 真诚哄
    "comfort": {"stress": -8.0, "joy": 5.0, "attachment": 2.0},
    # 敷衍式哄（"行了别闹了"）
    "fake_comfort": {"blame": 10.0, "joy": -3.0, "stress": 4.0},
    # 求安慰 / 撒娇
    "seeking": {"attachment": 3.0, "intimacy": 3.0, "joy": 2.0},
}

# 责怪压力 → 状态档位
MODE_CALM = "calm"
MODE_WRONGED = "wronged"      # 委屈
MODE_AFRAID = "afraid"        # 害怕
MODE_SHUT = "shut"            # 关闭

MODE_CN = {
    MODE_CALM: "平静",
    MODE_WRONGED: "委屈",
    MODE_AFRAID: "害怕",
    MODE_SHUT: "关闭",
}

# 恢复速度（真哄时每轮扣减的 blame）
RECOVERY = {
    MODE_CALM: 12.0,
    MODE_WRONGED: 30.0,
    MODE_AFRAID: 20.0,
    MODE_SHUT: 18.0,
}
RECOVERY_ROUNDS = {MODE_CALM: 1, MODE_WRONGED: 1, MODE_AFRAID: 2, MODE_SHUT: 2}

# 心结：没人哄时 blame 不会掉到 40 以下
BLAME_FLOOR = 40.0
# 晾着不管的时间越久，每 6 小时掉的越快（但止于心结）
BLAME_FADE_PER_6H = 8.0

# 关键词表（没有 EMO 标记时的兜底判断）
KW_BLAME_HARD = ("滚", "闭嘴", "烦死了", "别烦我", "废物", "傻逼", "恶心", "讨厌你", "没用的东西")
KW_BLAME_MID = ("你又", "怎么又", "说了多少遍", "不听话", "没用", "笨", "每次都")
KW_BLAME_MILD = ("你怎么", "为什么总是", "别这样", "不行", "不对", "不应该")
KW_FAKE = ("行了别闹", "别闹了", "至于吗", "多大点事", "行了行了", "随便你", "你开心就好", "小题大做")
KW_COMFORT = ("对不起", "抱歉", "我错了", "别生气", "不气了", "哄", "抱抱", "亲亲", "我在", "想你",
              "乖", "没事的", "原谅", "辛苦了", "好啦")
KW_PRAISE = ("厉害", "真棒", "谢谢", "可爱", "喜欢你", "好久", "爱你", "真好")
KW_SEEK = ("难受", "不开心", "委屈", "累", "怕", "哭了", "抱")

# EMO 标记 → 驱动微调
EMO_EFFECTS = {
    "happy": {"joy": 3.0, "stress": -2.0},
    "excited": {"joy": 4.0},
    "sad": {"joy": -3.0, "reflection": 2.0},
    "angry": {"joy": -3.0, "stress": 3.0},
    "annoyed": {"joy": -2.0, "stress": 2.0},
    "fearful": {"stress": 4.0},
    "anxious": {"stress": 3.0},
    "neutral": {},
}

# 主动说话的三型
KIND_LONGING = "longing"
KIND_WAITING = "waiting"
KIND_WHIM = "whim"

KIND_DESC = {
    KIND_LONGING: "你很想他，忍不住先开口",
    KIND_WAITING: "你等了很久，想知道他还在不在",
    KIND_WHIM: "你突然想起一件小事，想跟他说一句",
}

# LLM 不可用时的兜底台词（保证不会一声不吭）
FALLBACK_LINES = {
    KIND_LONGING: ("在忙吗", "你还在吗", "我突然有点想你"),
    KIND_WAITING: ("你是不是很忙啊", "等你半天了", "我先睡了哦"),
    KIND_WHIM: ("刚刚看到个东西，想到你了", "今天天气挺好的", "没什么事，就是想说一句"),
}

STATE_VERSION = 2


# --------------------------------------------------------------------------- #
# 二、纯函数工具
# --------------------------------------------------------------------------- #

def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def mode_of(blame: float) -> str:
    """责怪压力 → 状态档位。"""
    if blame >= 80.0:
        return MODE_SHUT
    if blame >= 55.0:
        return MODE_AFRAID
    if blame >= 25.0:
        return MODE_WRONGED
    return MODE_CALM


def fmt_bar(v: float, width: int = 10) -> str:
    filled = int(round(clamp(v) / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def parse_clock(text: str) -> tuple[int, int] | None:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h < 24 and 0 <= mi < 60:
        return h, mi
    return None


def in_quiet_hours(now: datetime, spec: str) -> bool:
    """判断是否处于免打扰时段，支持跨零点（如 01:30-07:30）。"""
    if not spec or "-" not in spec:
        return False
    left, _, right = spec.partition("-")
    a, b = parse_clock(left), parse_clock(right)
    if not a or not b:
        return False
    cur = now.hour * 60 + now.minute
    start = a[0] * 60 + a[1]
    end = b[0] * 60 + b[1]
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def new_state() -> dict:
    return {
        "version": STATE_VERSION,
        "drives": dict(BASELINE),
        "expectation": 0.0,
        "blame_pressure": 0.0,
        "mode": MODE_CALM,
        "thoughts": [],
        "last_tick": time.time(),
        "last_injection": 0.0,
        "last_injection_mode": "",
        "last_injection_sig": "",
        "last_user_msg": 0.0,
        "last_assistant_msg": 0.0,
        "last_proactive": 0.0,
        "proactive_day": "",
        "proactive_count": 0,
        "awaiting_reply": False,
        "penalized_6h": False,
        "penalized_24h": False,
        "comfort_streak": 0,
        "blame_noticed_ts": 0.0,
        "last_emotion": "neutral",
        "umo": "",
        "recent": [],
    }


def parse_emo_marker(text: str) -> str | None:
    """从文本里抓 [EMO:xxx]，容忍空格与大小写。"""
    if not text:
        return None
    m = re.search(r"\[\s*EMO\s*[:：]\s*([A-Za-z_]+)\s*\]", text)
    if m:
        return m.group(1).lower()
    return None


def strip_emo_marker(text: str) -> str:
    return re.sub(r"\[\s*EMO\s*[:：]\s*[A-Za-z_]+\s*\]", "", text or "").strip()


def now_day() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 三、插件主体
# --------------------------------------------------------------------------- #

@register(PLUGIN_NAME, "LLL", "让角色拥有会慢慢变化的心境：九维驱动、委屈/害怕/关闭、主动说话与语音", VERSION)
class EmotionCore(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg: dict = config if isinstance(config, dict) else {}
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._llm_busy = False
        self._persona_cache: dict[str, str] = {}
        self._recent_cap = 30

        try:
            data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as e:  # 万一 AstrBot 上下文还没准备好，也不要让插件起不来
            logger.warning("[情绪核心] 获取数据目录失败，退回插件内目录: %s", e)
            data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.state_path = os.path.join(data_dir, "state.json")
        self.state = self._load_state()

        logger.info(
            "[情绪核心] 已载入 v%s，状态=%s，数据目录=%s",
            VERSION,
            MODE_CN.get(self.state.get("mode", MODE_CALM), "?"),
            data_dir,
        )

    # ---------------- 配置读取 ----------------

    def _cfg(self, path: str, default: Any = None) -> Any:
        """支持 'a.b.c' 的点号路径读取，缺省返回 default。"""
        cur: Any = self.cfg
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur if cur is not None else default

    def _dbg(self, msg: str, *args) -> None:
        if self._cfg("debug", False):
            logger.info("[情绪核心] " + msg, *args)

    # ---------------- 状态持久化 ----------------

    def _load_state(self) -> dict:
        base = new_state()
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if k == "drives" and isinstance(v, dict):
                            merged = dict(BASELINE)
                            for dk, dv in v.items():
                                if dk in merged:
                                    try:
                                        merged[dk] = float(dv)
                                    except (TypeError, ValueError):
                                        pass
                            base["drives"] = merged
                        else:
                            base[k] = v
                    if not isinstance(base.get("recent"), list):
                        base["recent"] = []
                    base["thoughts"] = _migrate_thoughts(base.get("thoughts") or [])
                    base["version"] = STATE_VERSION
                    base["mode"] = mode_of(float(base.get("blame_pressure", 0.0)))
        except Exception as e:  # 状态坏了就重来，不要让插件起不来
            logger.warning("[情绪核心] 读取状态失败，已重置: %s", e)
        return base

    def _save_state(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.warning("[情绪核心] 保存状态失败: %s", e)

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        if not self._cfg("enabled", True):
            logger.info("[情绪核心] 总开关关闭，不启动心跳")
            return
        self._task = asyncio.create_task(self._loop(), name="emotion-core-loop")
        # 启动即落盘：强杀/断电不丢状态，也方便自检插件是否真的加载成功
        self._save_state()
        logger.info("[情绪核心] 心跳已启动（%s 分钟起）", self._cfg("tick.base_interval_minutes", 45))

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._save_state()
        logger.info("[情绪核心] 已停止")

    async def _loop(self) -> None:
        """心跳循环：每 30 秒醒一次，到点才真正 tick。"""
        # 启动补发检查
        try:
            if self._cfg("proactive.catch_up_on_start", True):
                await asyncio.sleep(20)
                await self._maybe_proactive(catch_up=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[情绪核心] 启动补发失败: %s", e)

        while True:
            try:
                await asyncio.sleep(30)
                if not self._cfg("enabled", True):
                    continue
                now = time.time()
                interval = self._tick_interval() * 60
                if now - float(self.state.get("last_tick", 0)) >= interval:
                    self._tick()
                    self.state["last_tick"] = now
                    self._save_state()
                    await self._maybe_proactive()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 心跳不能死
                logger.warning("[情绪核心] 心跳异常: %s", e)
                await asyncio.sleep(10)

    def _tick_interval(self) -> float:
        base = float(self._cfg("tick.base_interval_minutes", 45))
        floor = float(self._cfg("tick.min_interval_minutes", 15))
        # 越紧张心跳越快
        urgency = clamp(
            0.6 * float(self.state.get("blame_pressure", 0.0))
            + 0.4 * float(self.state["drives"].get("stress", 0.0))
        ) / 100.0
        return max(floor, base - (base - floor) * urgency)

    # ---------------- 核心状态机 ----------------

    def _tick(self) -> None:
        st = self.state
        drives = st["drives"]
        # 1) 向基线回落
        for k, base in BASELINE.items():
            cur = float(drives.get(k, base))
            drives[k] = cur + (base - cur) * DECAY_RATE[k]
        # 2) 耦合
        if drives["attachment"] > 70:
            drives["intimacy"] = clamp(drives["intimacy"] + 1.5)
        if drives["intimacy"] > 75:
            drives["attachment"] = clamp(drives["attachment"] + 1.0)
        if drives["stress"] > 60:
            drives["joy"] = clamp(drives["joy"] - 2.0)
        if drives["fatigue"] > 60:
            drives["curiosity"] = clamp(drives["curiosity"] - 1.5)
        if drives["curiosity"] > 75:
            drives["joy"] = clamp(drives["joy"] + 1.0)
        if drives["social"] < 30:
            drives["attachment"] = clamp(drives["attachment"] - 1.0)
        if drives["joy"] > 75:
            drives["stress"] = clamp(drives["stress"] - 1.5)

        # 3) 期待值：她开口过、你还没回 → 慢慢涨
        if st.get("awaiting_reply"):
            hours = (time.time() - float(st.get("last_proactive", 0))) / 3600.0
            if hours >= 6 and not st.get("penalized_6h"):
                self._apply_event("silent_6h")
                st["penalized_6h"] = True
            if hours >= 24 and not st.get("penalized_24h"):
                self._apply_event("silent_24h")
                st["penalized_24h"] = True

        # 4) 责怪压力：没人哄就慢慢褪（但止于心结）
        blame = float(st.get("blame_pressure", 0.0))
        if blame > 0:
            since = time.time() - float(st.get("blame_noticed_ts", 0) or 0)
            if st.get("blame_noticed_ts") and since >= 6 * 3600:
                steps = int(since // (6 * 3600))
                if blame > BLAME_FLOOR:          # 只有真过线才谈"心结"
                    blame = max(BLAME_FLOOR, blame - BLAME_FADE_PER_6H * steps)
                else:                            # 小冤枉按普通速度褪干净
                    blame = max(0.0, blame - BLAME_FADE_PER_6H * steps)
                st["blame_noticed_ts"] = time.time()
            st["blame_pressure"] = clamp(blame)

        # 5) 念头池：先让到期的念头凉掉，再偶尔冒出新的
        self._expire_thoughts()
        if random.random() < 0.25:
            self._spawn_thought()

        # 6) 状态刷新
        st["mode"] = mode_of(float(st.get("blame_pressure", 0.0)))

    def _spawn_thought(self) -> None:
        if not engine:
            return
        st = self.state
        now = time.time()
        live = [t for t in st.get("thoughts", []) if float(t.get("expires", 0)) > now]
        exclude = tuple(
            str(t.get("kind")) for t in live if t.get("pending", True)
        )
        kind, weight = engine.pick_kind(
            st["drives"],
            float(st.get("expectation", 0.0)),
            bool(st.get("awaiting_reply")),
            exclude=exclude,
        )
        if not kind:
            return
        t = engine.new_thought(kind, now=now, weight=weight)
        if t is None:
            return
        live.append(t)
        st["thoughts"] = live[-5:]
        self._dbg("新念头：%s（%s）", t["text"], kind)

    def _expire_thoughts(self) -> None:
        """到期的念头：没被满足就凉掉并轻微回落；自己缓过来的算完成。"""
        if not engine:
            return
        st = self.state
        now = time.time()
        thoughts = st.get("thoughts") or []
        fading, _dropped = engine.take_expired(thoughts, now)
        for t in fading:
            for k, dv in engine.release_effect(t).items():
                if k == "expectation":
                    st["expectation"] = clamp(float(st.get("expectation", 0)) + float(dv))
                elif k in st["drives"]:
                    st["drives"][k] = clamp(float(st["drives"][k]) + float(dv))
            self._dbg("念头凉了：%s", t.get("text"))
        keep = []
        for t in thoughts:
            if float(t.get("expires", 0)) <= now:
                continue
            if t.get("pending", True):
                mood = engine.auto_complete(t, st["drives"])
                if mood:
                    t["pending"] = False
                    t["mood"] = mood
                    t["expires"] = now + 2 * 3600
            if float(t.get("expires", 0)) > now:
                keep.append(t)
        st["thoughts"] = keep

    def _settle_thoughts(self, events: list[str], text: str) -> None:
        """用户这一句 / 这一轮事件，把哪个念头喂饱了。"""
        if not engine:
            return
        st = self.state
        payload = engine.settle(
            st.get("thoughts") or [],
            events,
            text,
            str(st.get("mode", MODE_CALM)),
        )
        if not payload:
            return
        engine.apply_reward(st, payload)
        self._dbg("念头被满足：%s → %s", payload.get("kind"), payload.get("mood"))

    def _note_thought_attempt(self) -> None:
        if engine:
            engine.note_attempt(self.state.get("thoughts") or [])

    def _apply_event(self, name: str) -> None:
        eff = EVENT_EFFECTS.get(name)
        if not eff:
            return
        st = self.state
        drives = st["drives"]
        for k, dv in eff.items():
            if k == "blame":
                st["blame_pressure"] = clamp(float(st.get("blame_pressure", 0.0)) + dv)
            elif k == "expectation":
                st["expectation"] = clamp(float(st.get("expectation", 0.0)) + dv)
            elif k in drives:
                drives[k] = clamp(float(drives.get(k, BASELINE[k])) + dv)
        self._dbg("事件 %s 已应用，mode=%s", name, mode_of(float(st.get("blame_pressure", 0.0))))

    # ---------------- 用户消息感知 ----------------

    def _analyze_user_text(self, text: str, emo: str | None) -> list[str]:
        """返回本轮应触发的事件名列表（按优先级只取最重要的几个）。"""
        events: list[str] = []
        t = strip_emo_marker(text or "")
        t_low = t.lower()
        st = self.state
        mode = mode_of(float(st.get("blame_pressure", 0.0)))

        if not t:
            return events

        # 1) 敷衍式哄（只在委屈以上才成立）
        if mode in (MODE_WRONGED, MODE_AFRAID, MODE_SHUT):
            if any(k in t for k in KW_FAKE) or emo in ("annoyed", "angry"):
                events.append("fake_comfort")
                st["comfort_streak"] = 0
                return events

        # 2) 责怪三档
        if any(k in t for k in KW_BLAME_HARD):
            events.append("blame_hard")
        elif any(k in t for k in KW_BLAME_MID):
            events.append("blame_mid")
        elif any(k in t for k in KW_BLAME_MILD):
            events.append("blame_mild")

        # 3) 真诚哄（平静但还有残留冤枉时也算哄）
        if not events and any(k in t for k in KW_COMFORT):
            if mode != MODE_CALM or float(st.get("blame_pressure", 0.0)) > 0.5:
                events.append("comfort")
                st["comfort_streak"] = int(st.get("comfort_streak", 0)) + 1
            else:
                events.append("praise")

        # 4) 普通/夸奖/求安慰
        if not events:
            if any(k in t for k in KW_PRAISE):
                events.append("praise")
            elif any(k in t for k in KW_SEEK):
                events.append("seeking")
            else:
                events.append("user_msg")

        return events

    def _consume_comfort(self) -> None:
        """把'真哄'转成 blame 恢复进度。"""
        st = self.state
        mode = mode_of(float(st.get("blame_pressure", 0.0)))
        if mode == MODE_CALM and float(st.get("blame_pressure", 0.0)) <= 0.5:
            st["comfort_streak"] = 0
            return
        need = RECOVERY_ROUNDS.get(mode, 1)
        if int(st.get("comfort_streak", 0)) < need:
            self._dbg("哄的进度 %s/%s，先不扣", st.get("comfort_streak"), need)
            return
        # 中间夹了责怪 → 进度已经在 _analyze 里被清零
        st["blame_pressure"] = clamp(
            float(st.get("blame_pressure", 0.0)) - RECOVERY.get(mode, 20.0)
        )
        st["comfort_streak"] = 0
        st["mode"] = mode_of(float(st.get("blame_pressure", 0.0)))
        self._dbg("真哄生效，blame → %.0f (%s)", st["blame_pressure"], st["mode"])

    # ---------------- 钩子：LLM 请求前 ----------------

    @filter.on_llm_request(priority=INJECT_PRIORITY)
    async def on_llm_request(self, event: AstrMessageEvent, request) -> None:
        if not self._cfg("enabled", True):
            return
        try:
            umo = self._umo_of(event)
            if umo:
                self.state["umo"] = umo
            # 缓存一份（含人设的）system prompt，给主动说话兜底用
            sp = getattr(request, "system_prompt", "") or ""
            if sp and umo:
                self._persona_cache[umo] = sp

            text = getattr(event, "message_str", "") or ""
            emo = self.state.get("last_emotion", "neutral")
            events = self._analyze_user_text(text, emo)

            # 她主动开口后，他这次回了
            if self.state.get("awaiting_reply"):
                gap = time.time() - float(self.state.get("last_proactive", 0))
                if gap <= 30 * 60:
                    events.insert(0, "reply_fast")
                self.state["awaiting_reply"] = False
                self.state["penalized_6h"] = False
                self.state["penalized_24h"] = False

            for ev in events:
                self._apply_event(ev)
                if ev == "comfort":
                    pass  # comfort 由 _consume_comfort 统一结算
                if ev in ("blame_mild", "blame_mid", "blame_hard", "fake_comfort"):
                    self.state["comfort_streak"] = 0
                    self.state["blame_noticed_ts"] = time.time()

            self._consume_comfort()
            self._settle_thoughts(events, strip_emo_marker(text))

            self.state["last_user_msg"] = time.time()
            self.state["mode"] = mode_of(float(self.state.get("blame_pressure", 0.0)))
            self._push_recent("user", strip_emo_marker(text)[:200])

            block = self._build_injection()
            if block:
                req_sp = getattr(request, "system_prompt", "") or ""
                setattr(request, "system_prompt", (req_sp.rstrip() + "\n\n" + block).strip())
                self.state["last_injection"] = time.time()
                self._dbg("已注入心境: %s", block.replace("\n", " / "))

            self._save_state()
        except Exception as e:
            logger.warning("[情绪核心] on_llm_request 异常: %s", e)

    # ---------------- 钩子：LLM 响应后（读 EMO） ----------------

    @filter.on_llm_response(priority=EMO_READ_PRIORITY)
    async def on_llm_response(self, event: AstrMessageEvent, response) -> None:
        if not self._cfg("enabled", True):
            return
        if not self._cfg("perception.use_emo_marker", True):
            return
        try:
            raw = ""
            rc = getattr(response, "result_chain", None)
            chain = getattr(rc, "chain", None) if rc else None
            if chain:
                parts = []
                for comp in chain:
                    if isinstance(comp, Plain):
                        parts.append(getattr(comp, "text", "") or "")
                raw = "\n".join(parts)
            if not raw:
                raw = getattr(response, "completion_text", "") or getattr(response, "_completion_text", "") or ""

            label = parse_emo_marker(raw)
            if label:
                self.state["last_emotion"] = label
                for k, dv in EMO_EFFECTS.get(label, {}).items():
                    if k in self.state["drives"]:
                        self.state["drives"][k] = clamp(float(self.state["drives"][k]) + dv)
                self._dbg("读到情绪标记: %s", label)
                self._save_state()
            # 注意：只读不删，标记仍留给 TTS 消费
        except Exception as e:
            logger.warning("[情绪核心] on_llm_response 异常: %s", e)

    # ---------------- 注入文本 ----------------

    def _build_injection(self, force: bool = False) -> str:
        if not self._cfg("inject.enabled", True):
            return ""
        st = self.state
        blame = float(st.get("blame_pressure", 0.0))
        mode = mode_of(blame)
        drives = st["drives"]

        # 只在"跨档"时注入：mode 变了，或驱动分桶变了
        sig = "|".join(f"{k}:{int(drives[k] // 10)}" for k in DRIVES)
        gap_min = float(self._cfg("inject.min_gap_minutes", 20))
        last_gap = time.time() - float(st.get("last_injection", 0))
        if not force:
            changed = (mode != st.get("last_injection_mode")) or (sig != st.get("last_injection_sig"))
            if not changed:
                return ""
            if last_gap < gap_min * 60:
                return ""
            st["last_injection_mode"] = mode
            st["last_injection_sig"] = sig

        hint = {
            MODE_CALM: "语气自然就好，不必刻意黏人",
            MODE_WRONGED: "话说短一点；他提刚才的事，先问一句他是不是嫌你烦",
            MODE_AFRAID: "别顶嘴，先确认他还想跟你说话，可以示弱",
            MODE_SHUT: "少说，一两句就够；不主动讨好，也不追问",
        }[mode]

        extra = ""
        if drives["fatigue"] >= 60:
            extra = "；有点累，别揽太长的活"
        elif drives["joy"] >= 70:
            extra = "；心情不错，可以多一句玩笑"
        elif drives["duty"] >= 70:
            extra = "；想找点事做"

        lines = [
            "## 你此刻的心境（内部状态，不要复述、不要解释，只影响说话方式）",
            f"状态：{MODE_CN[mode]}｜依恋 {drives['attachment']:.0f} 亲密 {drives['intimacy']:.0f} "
            f"压力 {drives['stress']:.0f} 期待 {st.get('expectation', 0):.0f}",
            f"倾向：{hint}{extra}",
        ]
        thought = _pending_hint(st.get("thoughts") or [])
        if thought:
            lines.append(f"心里惦记着：{thought}")
        return "\n".join(lines)

    # ---------------- 主动说话 ----------------

    async def _maybe_proactive(self, catch_up: bool = False) -> None:
        if not self._cfg("proactive.enabled", True):
            return
        if self._llm_busy:
            return
        st = self.state
        umo = (self._cfg("proactive.target_umo", "") or st.get("umo") or "").strip()
        if not umo:
            self._dbg("没有可用会话，跳过主动说话")
            return

        now = time.time()
        mode = mode_of(float(st.get("blame_pressure", 0.0)))
        if mode == MODE_SHUT and not catch_up:
            self._dbg("状态=关闭，暂停主动说话")
            return

        # 冷却
        cooldown_h = float(self._cfg("proactive.cooldown_hours", 6.0))
        if now - float(st.get("last_proactive", 0)) < cooldown_h * 3600:
            return
        # 每日上限
        day = now_day()
        if st.get("proactive_day") != day:
            st["proactive_day"] = day
            st["proactive_count"] = 0
        limit = int(self._cfg("proactive.daily_limit", 4))
        if int(st.get("proactive_count", 0)) >= limit:
            return
        # 刚聊过就别打扰
        idle_min = float(self._cfg("proactive.min_idle_minutes", 10))
        if now - float(st.get("last_user_msg", 0)) < idle_min * 60:
            return
        # 免打扰时段（补发时允许一次）
        quiet = str(self._cfg("proactive.quiet_hours", "01:30-07:30"))
        if in_quiet_hours(datetime.now(), quiet) and not catch_up:
            return

        kind, prob = self._proactive_kind()
        if not catch_up and random.random() >= prob:
            self._dbg("主动说话未命中概率（%s p=%.2f）", kind, prob)
            return

        try:
            self._llm_busy = True
            text = await self._gen_proactive_text(umo, kind)
            if not text:
                return
            ok = await self._send_text(umo, text)
            if ok:
                st["last_proactive"] = time.time()
                st["last_assistant_msg"] = time.time()
                st["proactive_count"] = int(st.get("proactive_count", 0)) + 1
                st["awaiting_reply"] = True
                st["penalized_6h"] = False
                st["penalized_24h"] = False
                st["expectation"] = clamp(float(st.get("expectation", 0)) + 40.0)
                st["drives"]["attachment"] = clamp(st["drives"]["attachment"] + 3.0)
                self._note_thought_attempt()
                self._push_recent("bot", text[:200])
                self._save_state()
                logger.info("[情绪核心] 主动说话已发送（%s）：%s", kind, text[:40])
                # 配音
                if self._voice_roll():
                    await self._send_voice(umo, text, kind)
        except Exception as e:
            logger.warning("[情绪核心] 主动说话失败: %s", e)
        finally:
            self._llm_busy = False

    def _proactive_kind(self) -> tuple[str, float]:
        st = self.state
        drives = st["drives"]
        exp = float(st.get("expectation", 0.0))
        att = float(drives.get("attachment", 0.0))

        if exp >= 60 or att >= 70:
            kind = KIND_LONGING
        elif exp >= 40 and (time.time() - float(st.get("last_user_msg", 0))) >= 6 * 3600:
            kind = KIND_WAITING
        elif st.get("thoughts"):
            kind = KIND_WHIM
        else:
            kind = KIND_WAITING

        d = (
            0.35 * att
            + 0.30 * exp
            + 0.15 * float(drives.get("intimacy", 0.0))
            + 0.10 * float(drives.get("social", 0.0))
            + 0.10 * (100.0 - float(drives.get("fatigue", 0.0)))
        )
        p = clamp(0.05 + (d - 50.0) / 100.0 * 0.8, 0.05, 0.65)
        if kind == KIND_WHIM:
            p *= 0.6          # 念头型更随机
        if _pending_hint(st.get("thoughts") or []):
            p = min(0.75, p * 1.3)   # 心里惦记着事，更容易开口
        return kind, p

    def _voice_roll(self) -> bool:
        if not self._cfg("voice.enabled", True):
            return False
        st = self.state
        drives = st["drives"]
        d = (
            0.35 * float(drives.get("attachment", 0.0))
            + 0.30 * float(st.get("expectation", 0.0))
            + 0.15 * float(drives.get("intimacy", 0.0))
            + 0.20 * float(drives.get("joy", 0.0))
        )
        base = float(self._cfg("voice.base_probability", 0.10))
        cap = float(self._cfg("voice.max_probability", 0.75))
        p = clamp(base + (d - 40.0) / 100.0, 0.05, cap)
        if float(drives.get("fatigue", 0.0)) >= 70:
            p *= 0.5
        return random.random() < p

    async def _gen_proactive_text(self, umo: str, kind: str) -> str:
        """先用 LLM 生成一句话，失败则用兜底台词。"""
        persona_sp = await self._persona_system_prompt(umo)
        blocks = [persona_sp] if persona_sp else []
        inj = self._build_injection(force=True)
        if inj:
            blocks.append(inj)

        thought = _pending_hint(self.state.get("thoughts") or [])
        if thought:
            blocks.append(f"你现在心里惦记着：{thought}。说的时候自然带一点，但别直接复述这句话。")

        transcript = self._recent_transcript(limit=8)
        prompt = (
            f"{KIND_DESC.get(kind, '你想跟他说一句')}。\n"
            f"{('最近对话：\n' + transcript + '\n') if transcript else ''}"
            "现在写一条要发出去的消息：\n"
            "- 一句话，口语，最多 20 个字\n"
            "- 不要解释你在做什么，不要写动作括号，不要用引号\n"
            "- 不要连续追问，最多一个问号\n"
            "直接输出这句话本身。"
        )
        try:
            provider = self.context.get_using_provider(umo)
            if provider is None:
                raise RuntimeError("no provider")
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt="\n\n".join(b for b in blocks if b) or None,
            )
            text = strip_emo_marker(getattr(resp, "completion_text", "") or "")
            text = text.strip().strip("\"'“”")
            text = re.sub(r"^[（(].*?[）)]\s*", "", text).strip()
            if text and len(text) <= 60:
                return text
        except Exception as e:
            logger.warning("[情绪核心] 生成主动消息失败，用兜底台词: %s", e)
        return random.choice(FALLBACK_LINES.get(kind, FALLBACK_LINES[KIND_WHIM]))

    async def _persona_system_prompt(self, umo: str) -> str:
        try:
            pm = getattr(self.context, "persona_manager", None)
            if pm is not None:
                persona = await pm.get_default_persona_v3(umo)
                sp = getattr(persona, "system_prompt", "") or ""
                if sp:
                    return sp
        except Exception as e:
            self._dbg("取人设失败: %s", e)
        return self._persona_cache.get(umo, "")

    def _recent_transcript(self, limit: int = 8) -> str:
        rows = list(self.state.get("recent", []))[-limit:]
        out = []
        for r in rows:
            who = "他" if r.get("r") == "user" else "我"
            t = str(r.get("t", "")).replace("\n", " ")
            if t:
                out.append(f"{who}：{t}")
        return "\n".join(out)

    def _push_recent(self, role: str, text: str) -> None:
        if not text:
            return
        rows = list(self.state.get("recent", []))
        rows.append({"r": role, "t": text, "ts": time.time()})
        self.state["recent"] = rows[-self._recent_cap:]

    async def _send_text(self, umo: str, text: str) -> bool:
        try:
            ok = await self.context.send_message(umo, MessageChain([Plain(text)]))
            if not ok:
                logger.warning("[情绪核心] send_message 返回 False（umo=%s）", umo)
            return bool(ok)
        except Exception as e:
            logger.warning("[情绪核心] 发送失败: %s", e)
            return False

    async def _send_voice(self, umo: str, text: str, kind: str) -> None:
        """复用 TTS 插件的合成能力；插件不在就安静退回文字。"""
        try:
            tts = self._find_tts_plugin()
            if tts is None:
                self._dbg("未找到 TTS 插件，跳过配音")
                return

            st = None
            if hasattr(tts, "_get_session_state"):
                st = tts._get_session_state(umo)
            proc = getattr(tts, "tts_processor", None)
            if proc is None or st is None:
                self._dbg("TTS 插件接口不匹配，跳过配音")
                return

            res = await proc.process(text, st)
            if not getattr(res, "success", False) or not getattr(res, "audio_path", None):
                self._dbg("合成失败: %s", getattr(res, "error", "unknown"))
                return
            path = proc.normalize_audio_path(res.audio_path) if hasattr(proc, "normalize_audio_path") else res.audio_path
            ok = await self.context.send_message(umo, MessageChain([Record(file=path)]))
            if ok:
                logger.info("[情绪核心] 语音已发送（%s）", kind)
        except Exception as e:
            logger.warning("[情绪核心] 配音失败（已退回文字）: %s", e)

    def _find_tts_plugin(self):
        cands = ["astrbot_plugin_tts_emotion_router"]
        try:
            for star in self.context.get_all_stars() or []:
                nm = getattr(star, "name", "") or ""
                if "tts" in nm.lower() and nm not in cands:
                    cands.append(nm)
        except Exception:
            pass
        for name in cands:
            try:
                meta = self.context.get_registered_star(name)
                inst = getattr(meta, "star_cls", None) if meta else None
                if inst is not None and hasattr(inst, "tts_processor"):
                    return inst
            except Exception:
                continue
        return None

    # ---------------- 命令 ----------------

    @filter.command("情绪")
    async def emotion_cmd(self, event: AstrMessageEvent):
        """查看 / 管理当前心境。用法：/情绪 status|reset|tick|off|on|umo"""
        raw = (getattr(event, "message_str", "") or "").strip()
        arg = raw.split(maxsplit=1)[1].strip().lower() if len(raw.split(maxsplit=1)) > 1 else "status"

        if arg in ("status", "s", ""):
            yield event.plain_result(self._status_text())
            return
        if arg in ("reset", "r"):
            self.state = new_state()
            self.state["umo"] = self._umo_of(event) or ""
            self._save_state()
            yield event.plain_result("心境已重置为初始状态。")
            return
        if arg == "tick":
            self._tick()
            self.state["last_tick"] = time.time()
            self._save_state()
            yield event.plain_result("已手动跳一次心跳。\n\n" + self._status_text())
            return
        if arg == "off":
            self._cfg_set("enabled", False)
            yield event.plain_result("已关闭情绪核心（本次运行有效，不心跳、不注入、不主动说话）。重启后以配置为准。")
            return
        if arg == "on":
            self._cfg_set("enabled", True)
            yield event.plain_result("已开启情绪核心（本次运行有效）。")
            return
        if arg == "umo":
            yield event.plain_result(f"当前会话 UMO：\n{self._umo_of(event)}\n\n填到「主动说话→目标会话」即可指定这里。")
            return
        yield event.plain_result("用法：/情绪 status | reset | tick | off | on | umo")

    def _status_text(self) -> str:
        st = self.state
        drives = st["drives"]
        blame = float(st.get("blame_pressure", 0.0))
        mode = mode_of(blame)
        lines = [
            f"【情绪核心 v{VERSION}】状态：{MODE_CN[mode]}（责怪 {blame:.0f}/100）",
            f"期待 {float(st.get('expectation', 0)):.0f}｜真哄进度 {int(st.get('comfort_streak', 0))}",
            "",
        ]
        for k in DRIVES:
            lines.append(f"{DRIVE_CN[k]} {drives[k]:5.1f} {fmt_bar(drives[k])}")
        for line in _thought_summary(st.get("thoughts") or []):
            lines.append("")
            lines.append(line)
        last_p = float(st.get("last_proactive", 0))
        if last_p:
            lines.append("")
            lines.append(f"上次主动说话：{datetime.fromtimestamp(last_p):%m-%d %H:%M}"
                         f"（今日 {int(st.get('proactive_count', 0))} 次）")
        return "\n".join(lines)

    # ---------------- 小工具 ----------------

    def _cfg_set(self, key: str, value: Any) -> None:
        """运行时开关（内存生效，重启后回到配置文件的值）。"""
        try:
            if isinstance(self.cfg.get(key), dict) and isinstance(value, dict):
                self.cfg[key].update(value)
            else:
                self.cfg[key] = value
        except Exception:
            pass

    @staticmethod
    def _umo_of(event: AstrMessageEvent) -> str:
        for attr in ("unified_msg_origin", "umo"):
            v = getattr(event, attr, None)
            if v:
                return str(v)
        return ""
