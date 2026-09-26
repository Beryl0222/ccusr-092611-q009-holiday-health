"""假期健康提醒编排中的基础对象与纯领域规则。

时间约定：
- 存储层统一使用带时区的 UTC 时刻（ISO 字符串）；
- 计划中的出发/回程是“目的地当地日期”，生成提醒时按目的地 IANA 时区
  锚定到当天 00:00，再换算 UTC，避免跨时区错位；
- 回程后监测窗口固定为回程当日起一个自然月，月运算做日历进位
  （1 月 31 日 + 1 个月 = 2 月最后一天）。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Callable, Optional
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

class Clock:
    """可注入的当前时间来源。"""

    def now(self) -> datetime:  # pragma: no cover - 接口
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    """测试用固定时钟，接受 naive UTC 或带时区的时间。"""

    def __init__(self, value: datetime | str):
        if isinstance(value, str):
            value = parse_ts(value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        self._value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._value

    def advance(self, delta: timedelta) -> None:
        self._value += delta


def parse_ts(value: str) -> datetime:
    """解析 ISO 时间字符串，naive 视为 UTC，统一返回 aware UTC。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def get_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # zoneinfo 对未知时区抛 ZoneInfoNotFoundError
        raise ValueError(f"未知时区: {tz_name}") from exc


def parse_local_date(value: str | date) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(value)


def local_midnight(day: date, tz_name: str) -> datetime:
    """目的地当地某一日的 00:00（aware）。"""
    return datetime.combine(day, time.min, tzinfo=get_zone(tz_name))


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "moderate": 1, "high": 2}[self.value]

    @classmethod
    def of(cls, raw: str) -> "RiskLevel":
        try:
            return cls(raw)
        except ValueError as exc:
            raise ValueError(f"未知风险等级: {raw}") from exc


class MeasureKind(str, Enum):
    HANDWASHING = "handwashing"          # 洗手
    VACCINE = "vaccine"                  # 疫苗
    MOSQUITO = "mosquito"                # 防蚊
    SYMPTOM_MONITORING = "monitoring"    # 症状监测
    CHILD_GROUP_HOLD = "child_group_hold"  # 儿童暂缓集体活动
    ADULT_ADVISORY = "adult_advisory"    # 成人健康建议


class Audience(str, Enum):
    CHILD = "child"
    ADULT = "adult"
    ALL = "all"


CHILD_AGE_LIMIT = 18  # 出发当日未满 18 岁视为儿童


def is_child_age(birth_date: date, trip_departure: date) -> bool:
    age = trip_departure.year - birth_date.year - (
        (trip_departure.month, trip_departure.day) < (birth_date.month, birth_date.day)
    )
    return age < CHILD_AGE_LIMIT


# ---------------------------------------------------------------------------
# 数据对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    plan_id: str
    owner_id: str
    destination: str
    dest_timezone: str
    depart_date: date
    return_date: date
    status: str            # active | cancelled
    version: int
    updated_at: str


@dataclass(frozen=True)
class Member:
    member_id: str
    plan_id: str
    name: str
    birth_date: date
    relationship: str
    is_child: bool
    created_at: str


@dataclass(frozen=True)
class Consent:
    member_id: str
    granted_by: str
    scope: str
    status: str            # granted | revoked
    version: int
    updated_at: str


@dataclass(frozen=True)
class RiskWindow:
    """目的地某段日期的风险等级，risk_version 随等级变化单调递增。"""
    destination: str
    start_date: date
    end_date: date
    level: RiskLevel
    risk_version: int


@dataclass(frozen=True)
class MeasureSpec:
    """措施模板：在给定计划/成员与风险画像下的一个措施窗口。"""
    key: str                         # 窗口内稳定身份，如 vaccine_pre
    kind: MeasureKind
    audience: Audience
    title: str
    advice: str
    due_date: date                   # 提醒到期的当地日期
    window_start: date
    window_end: date
    risk_level: RiskLevel            # 生成时窗口内最高风险
    revision_digest: str             # 覆盖日期上的风险版本摘要；变化才重算


@dataclass(frozen=True)
class Measure:
    """持久化的措施记录（模板的一个修订版本）。"""
    measure_uid: str
    plan_id: str
    member_id: str
    spec_key: str
    revision: int
    kind: MeasureKind
    audience: Audience
    title: str
    advice: str
    due_at: str                      # UTC，提醒到期时刻
    window_start: date
    window_end: date
    risk_level: RiskLevel
    status: str                      # pending | done | superseded
    created_at: str
    completed_at: Optional[str] = None
    completion_request: Optional[str] = None


@dataclass(frozen=True)
class SymptomReport:
    report_id: str
    member_id: str
    occurred_at: str                 # 症状出现当地时间换算的 UTC
    symptoms: tuple[str, ...]
    note: str
    from_risk_area: bool
    created_at: str


@dataclass(frozen=True)
class Escalation:
    escalation_id: str
    member_id: str
    report_id: str
    level: str                       # urgent_care
    status: str                      # open | acknowledged | resolved
    care_pathway: tuple[str, ...]
    created_at: str
    acknowledged_at: Optional[str] = None


@dataclass(frozen=True)
class ReminderView:
    """值班人员看到的一条提醒；排序保证就医升级在普通提示之前。"""
    measure_uid: str
    member_id: str
    kind: MeasureKind
    audience: Audience
    title: str
    advice: str
    due_at: str
    window_start: date
    window_end: date
    status: str                      # pending | done | superseded
    delivered: bool


# ---------------------------------------------------------------------------
# 风险画像
# ---------------------------------------------------------------------------

@dataclass
class RiskProfile:
    """根据一组风险窗口回答“某当地日期的等级/版本”。"""
    windows: tuple[RiskWindow, ...] = ()
    default_level: RiskLevel = RiskLevel.LOW
    default_version: int = 0

    def level_on(self, day: date) -> RiskLevel:
        return self._pick(day)[0]

    def version_on(self, day: date) -> int:
        return self._pick(day)[1]

    def _pick(self, day: date) -> tuple[RiskLevel, int]:
        """覆盖该日的窗口中以风险版本最高者（最新评估）为准。"""
        best: Optional[tuple[RiskLevel, int]] = None
        for w in self.windows:
            if w.start_date <= day <= w.end_date:
                candidate = (w.level, w.risk_version)
                if best is None or candidate[1] > best[1]:
                    best = candidate
        return best if best is not None else (self.default_level, self.default_version)

    def max_level(self, start: date, end: date) -> RiskLevel:
        level = self.default_level
        day = start
        while day <= end:
            candidate = self.level_on(day)
            if candidate.rank > level.rank:
                level = candidate
            day += timedelta(days=1)
        return level


# ---------------------------------------------------------------------------
# 措施模板生成（纯函数）
# ---------------------------------------------------------------------------

ALERT_SYMPTOMS = frozenset({"fever", "diarrhea", "vomiting", "rash", "jaundice", "cough", "dyspnea"})

FEVER_CARE_PATHWAY = (
    "立即佩戴口罩并就地单间隔离，避免乘坐公共交通",
    "拨打当地疾控/海关热线或 120，主动报告疫区旅行史与回程日期",
    "前往政府指定发热门诊/传染病院就诊，出示行程与疫苗记录",
    "由医护人员评估是否需要疟疾/登革热/肠道传染病检测",
    "在获得医生明确意见前保持居家、不参加聚集活动",
)


def _digest(spec_key: str, profile: RiskProfile, start: date, end: date,
            risk_sensitive: bool) -> str:
    """窗口覆盖日期上的风险版本摘要。

    仅风险敏感措施（受风险等级门槛或等级内容影响）参与摘要：风险未变化
    时摘要稳定（重复同步不再产生新修订），只有覆盖日期上的版本变化才
    会改变摘要。与风险无关的措施（洗手、日常监测、成人建议）使用常量
    摘要，风险变化不会重算这些窗口。
    """
    if not risk_sensitive:
        return spec_key + "|risk-independent"
    parts: list[str] = []
    day = start
    run_start = day
    run_version = profile.version_on(day)
    while day <= end:
        version = profile.version_on(day)
        if version != run_version:
            parts.append(f"{run_start}:{day - timedelta(days=1)}=v{run_version}")
            run_start = day
            run_version = version
        day += timedelta(days=1)
    parts.append(f"{run_start}:{end}=v{run_version}")
    return spec_key + "|" + ";".join(parts)


def build_measure_specs(
    plan: Plan,
    is_child: bool,
    profile: RiskProfile,
) -> list[MeasureSpec]:
    """根据计划、年龄角色与当前风险画像生成全部措施模板。

    儿童专属“暂缓集体活动”和成人建议使用不同 kind/audience 独立成条，
    绝不合并记录。
    """
    dep = plan.depart_date
    ret = plan.return_date
    monitor_end = add_months(ret, 1) - timedelta(days=1)  # 回程后满一个自然月
    audience = Audience.CHILD if is_child else Audience.ADULT
    # 措施“是否需要”的门槛看行程期间目的地的最高风险；
    # 而“是否需要重算”的 digest 仍按各措施自身窗口覆盖的日期计算，
    # 因此行程中段的风险变化只会修订真正覆盖该段的窗口。
    trip_level = profile.max_level(dep, ret)
    specs: list[MeasureSpec] = []

    def add(key, kind, aud, title, advice, due, ws, we, *,
            gate: Callable[[RiskLevel], bool] = lambda _l: True,
            risk_sensitive: bool = True):
        if not gate(trip_level):
            return
        window_level = profile.max_level(ws, we)
        level = trip_level if trip_level.rank > window_level.rank else window_level
        specs.append(MeasureSpec(
            key=key, kind=kind, audience=aud, title=title, advice=advice,
            due_date=due, window_start=ws, window_end=we, risk_level=level,
            revision_digest=_digest(key, profile, ws, we, risk_sensitive),
        ))

    # 行前洗手（出发前一周窗口，到期日 D-3）；与风险等级无关
    add("wash_pre", MeasureKind.HANDWASHING, audience,
        "行前洗手与消毒用品准备",
        "准备肥皂、含醇免洗手液；向家庭成员演示七步洗手法。",
        dep - timedelta(days=3), dep - timedelta(days=7), dep - timedelta(days=1),
        risk_sensitive=False)

    # 行中洗手（整个行程）；与风险等级无关
    add("wash_travel", MeasureKind.HANDWASHING, audience,
        "旅途中坚持洗手",
        "餐前、便后、接触公共设施后用肥皂流水洗手至少 20 秒。",
        dep, dep, ret, risk_sensitive=False)

    # 疫苗（窗口内最高风险达到 moderate 才安排），到期日 D-14
    add("vaccine_pre", MeasureKind.VACCINE, audience,
        "行前疫苗接种评估",
        "至少提前 14 天到旅行医学门诊评估黄热病、甲肝、伤寒等疫苗。",
        dep - timedelta(days=14), dep - timedelta(days=21), dep - timedelta(days=1),
        gate=lambda l: l.rank >= RiskLevel.MODERATE.rank)

    # 防蚊（行程内有 moderate 及以上风险时安排）
    add("mosquito_travel", MeasureKind.MOSQUITO, audience,
        "旅途中防蚊",
        "使用含 DEET/派卡瑞丁驱避剂、蚊帐与长袖衣物；清理积水。",
        dep, dep, ret,
        gate=lambda l: l.rank >= RiskLevel.MODERATE.rank)

    # 回程后症状监测：前 14 天与第 15 天至满一个月两段，日期不重不漏；
    # 监测义务不随风险等级变化，风险变化不重算这两个窗口
    add("monitor_early", MeasureKind.SYMPTOM_MONITORING, audience,
        "回程后早期症状监测（第 1-14 天）",
        "每日测量体温，出现发热、腹泻、皮疹、黄疸等立即记录并上报。",
        ret, ret, ret + timedelta(days=13), risk_sensitive=False)
    add("monitor_late", MeasureKind.SYMPTOM_MONITORING, audience,
        "回程后后期症状监测（第 15 天至满一个月）",
        "继续每日健康监测直至回程后满一个自然月，症状可迟至数周出现。",
        ret + timedelta(days=14), ret + timedelta(days=14), monitor_end,
        risk_sensitive=False)

    if is_child:
        # 儿童专属：暂缓集体活动（moderate+ 才要求），独立记录
        add("child_group_hold", MeasureKind.CHILD_GROUP_HOLD, Audience.CHILD,
            "儿童暂缓集体活动",
            "回程后 14 天内不返园/返校、不参加培训班等集体活动，直至监测期满无症状。",
            ret, ret, ret + timedelta(days=13),
            gate=lambda l: l.rank >= RiskLevel.MODERATE.rank)
    else:
        # 成人专属建议：与儿童条目分开；内容不随风险等级变化
        add("adult_advisory", MeasureKind.ADULT_ADVISORY, Audience.ADULT,
            "成人回程健康建议",
            "监测期内减少聚餐与密闭场所活动；出现症状及时就医并告知雇主与同行人。",
            ret, ret, ret + timedelta(days=13), risk_sensitive=False)

    return specs


def requires_urgent_care(report: SymptomReport) -> bool:
    """疫区返回者出现预警症状 → 就医路径优先于普通提示。"""
    if not report.from_risk_area:
        return False
    return any(s in ALERT_SYMPTOMS for s in report.symptoms)
