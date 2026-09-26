"""假期健康提醒编排中的基础对象与时间约定。

所有内部时刻都使用时区感知的 UTC ``datetime``；面向家庭成员的日期
（出发日、回程日、监测截止日）则锚定在具体 IANA 时区上，避免跨时区
错位。领域规则（风险分级、措施派生、症状分诊）放在本模块且不接触
持久化，便于单测与复用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# 旧版通用记录（service.py 仍在使用，保持兼容）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str
    version: int
    updated_at: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 常量：成员、风险、措施、建议、状态
# ---------------------------------------------------------------------------

MEMBER_CHILD = "child"   # 儿童：措施中含“暂缓集体活动”等专属建议
MEMBER_ADULT = "adult"   # 成人：健康自评等成人建议单独记录
MEMBER_CATEGORIES = (MEMBER_CHILD, MEMBER_ADULT)

RISK_LOW = "low"
RISK_ELEVATED = "elevated"
RISK_HIGH = "high"
RISK_LEVELS = (RISK_LOW, RISK_ELEVATED, RISK_HIGH)
RISK_RANK = {RISK_LOW: 0, RISK_ELEVATED: 1, RISK_HIGH: 2}

# 措施类型
MEASURE_HANDWASHING = "handwashing"        # 洗手（行程内每日，基础卫生）
MEASURE_VACCINE = "vaccine"                # 疫苗（出发前一次性，按疾病）
MEASURE_MOSQUITO = "mosquito_protection"   # 防蚊（风险窗口相交日每日）
MEASURE_CHILD_GROUP_PAUSE = "child_group_activity_pause"  # 儿童暂缓集体活动
MEASURE_ADULT_SELF_CHECK = "adult_self_check"             # 成人健康自评

# 建议分类：儿童暂缓集体活动与成人建议必须分开记录
ADVICE_GENERAL = "general"
ADVICE_CHILD_GROUP = "child_group_activity"
ADVICE_ADULT = "adult_advice"

MEASURE_META = {
    MEASURE_HANDWASHING: {"audience": "all", "advice_class": ADVICE_GENERAL},
    MEASURE_VACCINE: {"audience": "all", "advice_class": ADVICE_GENERAL},
    MEASURE_MOSQUITO: {"audience": "all", "advice_class": ADVICE_GENERAL},
    MEASURE_CHILD_GROUP_PAUSE: {"audience": MEMBER_CHILD,
                                "advice_class": ADVICE_CHILD_GROUP},
    MEASURE_ADULT_SELF_CHECK: {"audience": MEMBER_ADULT,
                               "advice_class": ADVICE_ADULT},
}

# 措施生命周期
STATUS_PENDING = "pending"        # 待办，可被同步触发提醒
STATUS_COMPLETED = "completed"    # 已完成：任何重复同步都不得再次触发
STATUS_SUPERSEDED = "superseded"  # 风险解除/窗口变化后不再需要（保留可解释）
STATUS_HELD = "held"              # 授权撤销等原因冻结，不再主动触发
STATUS_CANCELLED = "cancelled"    # 计划取消：停止提醒但记录保留，可人工办结

# 提醒 / 升级 / 计划状态
REMINDER_WAITING = "waiting"
REMINDER_SENT = "sent"
REMINDER_DONE = "done"
REMINDER_CANCELLED = "cancelled"

ESCALATION_OPEN = "open"
ESCALATION_RESOLVED = "resolved"

PLAN_DRAFT = "draft"
PLAN_ACTIVE = "active"
PLAN_CANCELLED = "cancelled"

# 蚊媒传播疾病（决定防蚊措施）
MOSQUITO_DISEASES = frozenset({
    "dengue", "zika", "chikungunya", "malaria",
    "yellow_fever", "japanese_encephalitis",
})
# 需要疫苗提前安排的疾病（高风险窗口时建议出发前接种）
VACCINE_DISEASES = frozenset({
    "yellow_fever", "japanese_encephalitis", "hepatitis_a",
    "hepatitis_b", "typhoid", "cholera", "rabies",
})

VACCINE_LEAD_DAYS = 14          # 疫苗在出发前 14 天提醒
MONITOR_DAYS_CHILD_PAUSE = 14   # 儿童返程后暂缓集体活动 14 天
ESCALATION_GRACE = timedelta(hours=24)  # 提醒发出后 24 小时未完成则升级

# 症状关键词
SYMPTOM_FEVER = "fever"
SYMPTOM_DIARRHEA = "diarrhea"
SYMPTOM_RASH = "rash"
SYMPTOM_JAUNDICE = "jaundice"
SYMPTOM_BLEEDING = "bleeding"
SYMPTOM_SHORTNESS_BREATH = "shortness_of_breath"
RED_FLAG_SYMPTOMS = frozenset({
    SYMPTOM_SHORTNESS_BREATH, SYMPTOM_JAUNDICE, SYMPTOM_BLEEDING,
})

# ---------------------------------------------------------------------------
# 时间工具：跨时区不允许错位
# ---------------------------------------------------------------------------


def get_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # pragma: no cover - 防御非法时区
        raise ValueError(f"未知时区: {tz_name}") from exc


def as_aware(value: datetime) -> datetime:
    """把任意 datetime 归一化为时区感知 UTC 时刻。"""
    if value.tzinfo is None:
        raise ValueError("时刻必须带时区信息")
    return value.astimezone(timezone.utc)


def local_datetime(day: date, tz_name: str,
                   hour: int = 0, minute: int = 0,
                   second: int = 0, microsecond: int = 0) -> datetime:
    """把“某地某日本地钟面时间”锚定为绝对时刻。"""
    return datetime.combine(
        day, time(hour, minute, second, microsecond),
        tzinfo=get_zone(tz_name),
    )


def to_local(value: datetime, tz_name: str) -> datetime:
    return as_aware(value).astimezone(get_zone(tz_name))


def local_date(value: datetime, tz_name: str) -> date:
    """绝对时刻在指定时区下的当地日历日。"""
    return to_local(value, tz_name).date()


def add_calendar_month(day: date, months: int = 1) -> date:
    """加一个“自然月”，而不是固定 30 天；月末日期钳位到目标月最后一天。

    例如 1 月 31 日 + 1 月 => 2 月最后一天。回程后“一个月”的监测
    截止日必须按日历月计算。
    """
    total = day.month - 1 + months
    year = day.year + total // 12
    month = total % 12 + 1
    # 目标月的天数：用下个月第 1 天往回退一天
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    last_day = (next_first - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def dates_between(start: date, end: date):
    """闭区间内的每一天。"""
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def ranges_overlap(a_start: date, a_end: date,
                   b_start: date, b_end: date) -> bool:
    return a_start <= b_end and b_start <= a_end


def trip_timezone(day: date, depart: date, returning: date,
                  home_tz: str, destination_tz: str) -> str:
    """旅行相关日期所在的时区：出发前/返程后按居家地，行程中按目的地。"""
    if depart <= day <= returning:
        return destination_tz
    return home_tz


def monitoring_window(return_date_: date, home_tz: str):
    """返程后一个月监测窗口（按居家地日历，闭区间）。

    返回 (起始时刻, 截止时刻, 截止当地日期)。起始为回程当日 00:00，
    截止为回程日加一个自然月当天的 23:59:59.999999。
    """
    end_day = add_calendar_month(return_date_, 1)
    start_dt = local_datetime(return_date_, home_tz)
    end_dt = local_datetime(end_day, home_tz, 23, 59, 59, 999999)
    return as_aware(start_dt), as_aware(end_dt), end_day


def is_within_monitoring(value: datetime, return_date_: date,
                         home_tz: str) -> bool:
    start, end, _ = monitoring_window(return_date_, home_tz)
    instant = as_aware(value)
    return start <= instant <= end


# ---------------------------------------------------------------------------
# 领域派生规则
# ---------------------------------------------------------------------------


def disease_set(payload) -> frozenset:
    if not payload:
        return frozenset()
    return frozenset(payload)


def window_covers_trip(window_start: date, window_end: date,
                       depart: date, returning: date) -> bool:
    """风险窗口是否与行程（含出发与回程当日）相交。"""
    return ranges_overlap(window_start, window_end, depart, returning)


def intersect_dates(window_start: date, window_end: date,
                    depart: date, returning: date):
    lo = max(window_start, depart)
    hi = min(window_end, returning)
    if lo <= hi:
        yield from dates_between(lo, hi)


@dataclass(frozen=True)
class ExpectedMeasure:
    """派生器认为“应当存在”的一条措施。"""
    kind: str
    scheduled: date                 # 当地日历日（一次性措施为出发当天/当天锚）
    tz_name: str
    window_id: str | None = None    # 由哪个风险窗口支撑（洗手/监测建议为 None）
    disease: str | None = None      # 疫苗按疾病区分

    @property
    def dedup_key(self) -> str:
        if self.kind == MEASURE_VACCINE:
            return f"{self.kind}|{self.disease}|{self.scheduled.isoformat()}"
        return f"{self.kind}|{self.scheduled.isoformat()}"


def expected_window_measures(window, depart: date, returning: date):
    """根据单个风险窗口与行程的相交情况，派生防蚊/疫苗措施期望。

    风险变化时只有与该窗口相关的期望需要重算，因此这里严格按窗口产出。
    """
    diseases = disease_set(window.diseases)
    if window.risk_level == RISK_LOW:
        return
    for day in intersect_dates(window.start_date, window.end_date,
                               depart, returning):
        if diseases & MOSQUITO_DISEASES:
            yield ExpectedMeasure(
                MEASURE_MOSQUITO, day,
                trip_timezone(day, depart, returning,
                              window.destination_tz, window.destination_tz),
                window_id=window.window_id,
            )
    # 高风险且疾病有对应疫苗时，出发前一次性措施（锚定出发当天，
    # 提醒时刻由 VACCINE_LEAD_DAYS 前移）
    if window.risk_level == RISK_HIGH:
        for disease in sorted(diseases & VACCINE_DISEASES):
            yield ExpectedMeasure(
                MEASURE_VACCINE, depart, window.destination_tz,
                window_id=window.window_id, disease=disease,
            )


def expected_routine_measures(depart: date, returning: date,
                              destination_tz: str, home_tz: str,
                              exposed: bool, category: str):
    """与风险窗口无关的常规措施：洗手、返程监测期的分类建议。

    ``exposed`` 表示该成员行程中是否曾暴露于 elevated/high 窗口；
    只有疫区返回者需要整段返程监测安排。
    """
    for day in dates_between(depart, returning):
        yield ExpectedMeasure(
            MEASURE_HANDWASHING, day,
            trip_timezone(day, depart, returning, home_tz, destination_tz),
        )
    if not exposed:
        return
    monitor_end = add_calendar_month(returning, 1)
    if category == MEMBER_CHILD:
        # 含回程当日共 MONITOR_DAYS_CHILD_PAUSE 个自然日，且不超出监测期
        last_pause = min(monitor_end,
                         returning + timedelta(days=MONITOR_DAYS_CHILD_PAUSE - 1))
        for day in dates_between(returning, last_pause):
            yield ExpectedMeasure(MEASURE_CHILD_GROUP_PAUSE, day, home_tz)
    elif category == MEMBER_ADULT:
        for day in dates_between(returning, monitor_end):
            yield ExpectedMeasure(MEASURE_ADULT_SELF_CHECK, day, home_tz)


@dataclass(frozen=True)
class Guidance:
    """一条指导，``order`` 越小越靠前：就医路径永远在普通提示之前。"""
    order: int
    code: str
    message: str
    urgent: bool = False

    def as_dict(self):
        return {"code": self.code, "message": self.message, "urgent": self.urgent}


def triage_guidance(symptoms, exposed_risk_area: bool,
                    in_monitoring: bool, mosquito_exposed: bool):
    """症状分诊：返回有序指导列表。

    疫区返回者在监测期内出现症状时，就医路径（含旅行史告知、发热门诊、
    口岸检疫渠道）整体置于普通提示之前；红旗症状升级为急诊。
    """
    symptoms = set(symptoms or [])
    guidance: list[Guidance] = []

    red_flag = bool(symptoms & RED_FLAG_SYMPTOMS)
    fever = SYMPTOM_FEVER in symptoms
    enteric = fever and SYMPTOM_DIARRHEA in symptoms
    # 监测/就医窗口内发热且有蚊媒暴露需紧急排查；窗口外单纯发热按普通就医
    urgent = red_flag or enteric or (
        fever and mosquito_exposed and in_monitoring)

    if exposed_risk_area and in_monitoring and symptoms:
        if urgent:
            guidance.append(Guidance(
                0, "emergency_now",
                "立即前往急诊或拨打当地急救电话，不要等待门诊预约", True))
        guidance.extend([
            Guidance(1 if urgent else 0, "fever_clinic",
                     "立即前往发热门诊/感染科就诊，并做好佩戴口罩等防护", True),
            Guidance(2, "declare_travel_history",
                     "就诊时第一时间告知疫区旅行史、回程日期与同行人情况", True),
            Guidance(3, "quarantine_hotline",
                     "联系属地疾控/口岸检疫热线报备，按其指引安排交通与隔离", True),
            Guidance(4, "avoid_public_transit",
                     "尽量避免乘坐公共交通，由专人专车或救护车转送", True),
            Guidance(5, "isolate_from_household",
                     "在得到评估前与家中老人、儿童及慢性病成员分开居住用餐", True),
        ])
    elif symptoms:
        if urgent:
            guidance.append(Guidance(
                0, "emergency_now",
                "立即前往急诊或拨打当地急救电话", True))
        else:
            guidance.append(Guidance(
                0, "seek_care", "尽快就医评估症状原因", True))

    # 普通提示永远排在就医路径之后
    tip_order = 100
    tips = [
        "多补充口服补液盐或温水，记录体温与症状变化",
        "充分休息、清淡饮食，避免饮酒与自行服用抗菌药",
        "在家佩戴口罩、勤洗手，咳嗽打喷嚏遮挡口鼻",
    ]
    for index, tip in enumerate(tips):
        guidance.append(Guidance(tip_order + index,
                                 f"tip_{index}", tip))
    guidance.sort(key=lambda g: g.order)
    return guidance
