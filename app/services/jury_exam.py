"""入门考试和练习场（docs/jury-v2.md 2.3、2.4）。

题目都来自题库（jury_bank）：考试只出计分用的那一份（已核对）、不暴露案件编号；练习场只出练习用的那一份
（未核对的题 + 已核对里固定切出来的一份）。练习场会公布标准答案，所以两份题不重叠，见 jury_bank.question_pool。
内容用评审台同一个匿名视图生成。
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import case as sa_case
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.case import JuryExamAttempt, JuryMember, JuryPracticeAnswer, ReportCase
from app.models.user import User
from app.services import cases as case_service
from app.services import jury as jury_service
from app.services import jury_bank as bank_service
from app.services import jury_points as points_service
from app.services import jury_settings
from app.services import penalties as penalty_service

logger = logging.getLogger(__name__)
UTC = timezone.utc

EXAM_TOTAL = settings.jury_exam_total
EXAM_PASS_SCORE = settings.jury_exam_pass_score
EXAM_TIME_LIMIT_MINUTES = settings.jury_exam_time_limit_minutes
# 截止之后还收卷的宽限：客户端倒计时结束才自动提交，路上要花时间
EXAM_GRACE_SECONDS = settings.jury_exam_grace_seconds
EXAM_ATTEMPTS_MAX = settings.jury_exam_attempts_max
EXAM_WINDOW_DAYS = settings.jury_exam_window_days
EXAM_RETRY_HOURS = settings.jury_exam_retry_hours
EXAM_VALID_DAYS = settings.jury_exam_valid_days
PRACTICE_DAILY_LIMIT = settings.jury_practice_daily_limit
ANSWER_VOTES = bank_service.DECIDED_VOTES
# 抽题 / 练习出题时最多看多少道候选
QUESTION_SCAN_LIMIT = 500


def _shuffle(items: list[Any]) -> None:
    """抽题的随机源。单独拿出来是为了测试里能替换掉。"""
    random.shuffle(items)


def _not_eligible(requirements: list[dict[str, Any]]) -> HTTPException:
    failed = [item["text"] for item in requirements if not item["ok"]]
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "jury_not_eligible", "message": "暂不满足条件：" + "、".join(failed)},
    )


def _answer_view(case: ReportCase) -> dict[str, Any]:
    answer = bank_service.question_answer(case)
    return {"outcome": answer["outcome"], "level": answer["level"], "category_label": answer["category_label"], "explanation": answer["explanation"]}


async def _usable_questions(
    db: AsyncSession, user_id: int, case_ids: list[int], *, limit: int | None = None
) -> list[ReportCase]:
    """按给定顺序挑出这个人可以做的题：符合入库规则、和他没有回避关系（不能考到自己被举报的那个案件）。

    回避关系和敏感类别都是整批预取的，查询条数不随题目数逐个增加 —— 练习场每次取题都要把整份题过一遍来报进度。
    """
    if not case_ids:
        return []
    loaded: dict[int, ReportCase] = {}
    for start in range(0, len(case_ids), case_service.LOOKUP_BATCH):
        batch = case_ids[start : start + case_service.LOOKUP_BATCH]
        loaded.update({case.id: case for case in (await db.execute(select(ReportCase).where(ReportCase.id.in_(batch)))).scalars().all()})
    cases = [loaded[case_id] for case_id in case_ids if case_id in loaded]
    context = await jury_service._conflict_context(db, user_id)
    await jury_service._prefetch_conflicts(db, context, cases)
    sensitive = await case_service.sensitive_case_ids(db, cases)
    group_content = await jury_settings.is_enabled(db, "public_group_content")
    found: list[ReportCase] = []
    for case in cases:
        if await jury_service._has_conflict(db, user_id, case, context):
            continue
        if await bank_service.block_reason(db, case, group_content=group_content, sensitive=case.id in sensitive) is not None:
            continue
        found.append(case)
        if limit is not None and len(found) >= limit:
            break
    return found


# ---------------------------------------------------------------- 考试状态


async def _attempts(db: AsyncSession, user_id: int) -> list[JuryExamAttempt]:
    """这个人的考试场次，新的在前。"""
    rows = await db.execute(select(JuryExamAttempt).where(JuryExamAttempt.user_id == user_id).order_by(JuryExamAttempt.id.desc()).limit(100))
    return list(rows.scalars().all())


def _deadline(attempt: JuryExamAttempt) -> datetime:
    return penalty_service.aware(attempt.expires_at) + timedelta(seconds=EXAM_GRACE_SECONDS)


async def _settle_expired(db: AsyncSession, attempts: list[JuryExamAttempt], now: datetime) -> bool:
    """超时没交的场次按 0 分、未通过记一次。惰性结算：谁来读状态谁顺手结。"""
    changed = False
    for attempt in attempts:
        if attempt.submitted_at is None and now > _deadline(attempt):
            attempt.submitted_at = penalty_service.aware(attempt.expires_at)
            attempt.score = 0
            attempt.passed = False
            attempt.timed_out = True
            changed = True
    if changed:
        await db.commit()
    return changed


def _pass_valid(attempt: JuryExamAttempt, member: JuryMember | None, now: datetime) -> bool:
    """通过之后 EXAM_VALID_DAYS 天内有效；期间已经被任命的不用重考。"""
    if not attempt.passed or attempt.submitted_at is None:
        return False
    if penalty_service.aware(attempt.submitted_at) + timedelta(days=EXAM_VALID_DAYS) > now:
        return True
    return member is not None and member.status == "active"


def _state(attempts: list[JuryExamAttempt], member: JuryMember | None, now: datetime) -> dict[str, Any]:
    finished = [attempt for attempt in attempts if attempt.submitted_at is not None]
    active = next((attempt for attempt in attempts if attempt.submitted_at is None), None)
    passed = next((attempt for attempt in finished if _pass_valid(attempt, member, now)), None)
    window_start = now - timedelta(days=EXAM_WINDOW_DAYS)
    in_window = [attempt for attempt in attempts if penalty_service.aware(attempt.started_at) >= window_start]
    last = finished[0] if finished else None
    next_attempt_at: datetime | None = None
    exhausted = len(in_window) >= EXAM_ATTEMPTS_MAX
    if passed is None and active is None:
        waits: list[datetime] = []
        if last is not None and not last.passed:
            waits.append(penalty_service.aware(last.submitted_at) + timedelta(hours=EXAM_RETRY_HOURS))
        if exhausted:
            # 最早的一场滑出窗口之后才有新的名额
            waits.append(penalty_service.aware(in_window[-1].started_at) + timedelta(days=EXAM_WINDOW_DAYS))
        future = [moment for moment in waits if moment > now]
        next_attempt_at = max(future) if future else None
    return {
        "active": active,
        "passed": passed,
        "last": last,
        "attempts_used": len(in_window),
        "exhausted": exhausted and active is None,
        "next_attempt_at": next_attempt_at,
    }


async def _load_state(db: AsyncSession, user_id: int, member: JuryMember | None, now: datetime) -> dict[str, Any]:
    attempts = await _attempts(db, user_id)
    await _settle_expired(db, attempts, now)
    return _state(attempts, member, now)


async def has_valid_pass(db: AsyncSession, user_id: int, member: JuryMember | None, now: datetime) -> bool:
    return (await _load_state(db, user_id, member, now))["passed"] is not None


async def summary(db: AsyncSession, user_id: int, member: JuryMember | None, now: datetime) -> dict[str, Any]:
    """/jury/me.exam。"""
    state = await _load_state(db, user_id, member, now)
    passed, last = state["passed"], state["last"]
    return {
        # false 表示题库里已核对的题不够，暂时不能考
        "available": await bank_service.verified_count(db) >= EXAM_TOTAL,
        "passed": passed is not None,
        "passed_at": penalty_service.aware(passed.submitted_at) if passed is not None else None,
        "attempts_used": state["attempts_used"],
        "attempts_max": EXAM_ATTEMPTS_MAX,
        "next_attempt_at": state["next_attempt_at"],
        "last_score": last.score if last is not None else None,
        "pass_score": EXAM_PASS_SCORE,
        "total": EXAM_TOTAL,
    }


async def _question_views(db: AsyncSession, attempt: JuryExamAttempt) -> list[dict[str, Any]]:
    """这一场的题目。只给内容和题号，不带案件编号 —— 否则可以拿着编号去别处找答案。"""
    case_ids = [int(case_id) for case_id in json.loads(attempt.question_ids)]
    cases = {case.id: case for case in (await db.execute(select(ReportCase).where(ReportCase.id.in_(case_ids)))).scalars().all()}
    questions: list[dict[str, Any]] = []
    started = penalty_service.aware(attempt.started_at)
    for index, case_id in enumerate(case_ids, start=1):
        # 考试题和质控题是同一批旧案件：内容的时间同样平移到开考前不久，否则考生能据此认出哪些案件是旧的。
        # 按场次和题号取值，同一场里重复读（GET /jury/exam 的 active）看到的时间不变。
        view = await jury_service.juror_case_view(db, cases[case_id], disguise=(started, ("exam", attempt.id, index)))
        questions.append(
            {
                "question": index,
                "kind": view["kind"],
                "reasons": view["reasons"],
                "report_count": view["report_count"],
                "messages": view["messages"],
                "post": view["post"],
            }
        )
    return questions


async def _active_view(db: AsyncSession, attempt: JuryExamAttempt) -> dict[str, Any]:
    return {"attempt_id": attempt.id, "expires_at": penalty_service.aware(attempt.expires_at), "questions": await _question_views(db, attempt)}


async def exam_state(db: AsyncSession, user: User) -> dict[str, Any]:
    """GET /jury/exam。"""
    now = datetime.now(UTC)
    member = await jury_service.get_member(db, user.id)
    requirements = await jury_service.eligibility(db, user)
    state = await _load_state(db, user.id, member, now)
    passed, last, active = state["passed"], state["last"], state["active"]
    return {
        "available": await bank_service.verified_count(db) >= EXAM_TOTAL,
        "eligible": all(item["ok"] for item in requirements),
        "passed": passed is not None,
        "passed_at": penalty_service.aware(passed.submitted_at) if passed is not None else None,
        "total": EXAM_TOTAL,
        "pass_score": EXAM_PASS_SCORE,
        "time_limit_minutes": EXAM_TIME_LIMIT_MINUTES,
        "attempts_used": state["attempts_used"],
        "attempts_max": EXAM_ATTEMPTS_MAX,
        "window_days": EXAM_WINDOW_DAYS,
        "next_attempt_at": state["next_attempt_at"],
        "last_result": {"score": last.score or 0, "passed": bool(last.passed), "submitted_at": penalty_service.aware(last.submitted_at)}
        if last is not None
        else None,
        "active": await _active_view(db, active) if active is not None else None,
        "server_time": now,
    }


# ---------------------------------------------------------------- 开考 / 交卷


async def _draw_questions(db: AsyncSession, user_id: int, attempts: list[JuryExamAttempt]) -> list[int]:
    """从计分用的那一份题里随机抽 EXAM_TOTAL 道，之前考到过的排在后面，在练习场做过的排在最后。不够返回空列表。"""
    case_ids = list(
        (
            await db.execute(select(ReportCase.id).where(*bank_service.bank_clauses(pool="scored")).order_by(ReportCase.id.asc()).limit(QUESTION_SCAN_LIMIT))
        ).scalars()
    )
    seen: set[int] = set()
    for attempt in attempts:
        seen.update(int(case_id) for case_id in json.loads(attempt.question_ids))
    # 题目还没核对的时候可能在练习场出过，那时已经给他看过标准答案了。
    practiced = set((await db.execute(select(JuryPracticeAnswer.case_id).where(JuryPracticeAnswer.user_id == user_id))).scalars().all())
    _shuffle(case_ids)
    case_ids.sort(key=lambda case_id: (case_id in practiced, case_id in seen))
    cases = await _usable_questions(db, user_id, case_ids, limit=EXAM_TOTAL)
    return [case.id for case in cases] if len(cases) >= EXAM_TOTAL else []


async def start_exam(db: AsyncSession, user: User) -> dict[str, Any]:
    """POST /jury/exam/start。已有进行中的一场时返回同一场。"""
    now = datetime.now(UTC)
    requirements = await jury_service.eligibility(db, user)
    if not all(item["ok"] for item in requirements):
        raise _not_eligible(requirements)
    member = await jury_service.get_member(db, user.id)
    attempts = await _attempts(db, user.id)
    await _settle_expired(db, attempts, now)
    state = _state(attempts, member, now)
    active = state["active"]
    if active is None:
        if state["passed"] is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "exam_passed", "message": "你已经通过入门考试了"})
        next_attempt_at = state["next_attempt_at"]
        detail_time = jury_service._iso(next_attempt_at) if next_attempt_at is not None else None
        if state["exhausted"]:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "exam_attempts_exhausted",
                    "message": f"{EXAM_WINDOW_DAYS} 天内最多考 {EXAM_ATTEMPTS_MAX} 次，次数已用完",
                    "next_attempt_at": detail_time,
                },
            )
        if next_attempt_at is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": "exam_cooldown", "message": f"没通过的话要过 {EXAM_RETRY_HOURS} 小时才能重考", "next_attempt_at": detail_time},
            )
        question_ids = await _draw_questions(db, user.id, attempts)
        if not question_ids:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "exam_unavailable", "message": "题库里的题目还不够，暂时不能考试"})
        active = JuryExamAttempt(
            user_id=user.id,
            started_at=now,
            expires_at=now + timedelta(minutes=EXAM_TIME_LIMIT_MINUTES),
            question_ids=json.dumps(question_ids),
            total=len(question_ids),
        )
        db.add(active)
        await db.commit()
    return {**await _active_view(db, active), "server_time": now}


def _parse_answers(raw: list[Any], total: int) -> dict[int, str]:
    invalid = HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": "invalid_answers", "message": "答案格式不对：题号不存在、重复，或者选项不是 none / l1 / l2 / l3"},
    )
    answers: dict[int, str] = {}
    for item in raw:
        question = item.get("question") if isinstance(item, dict) else getattr(item, "question", None)
        vote = item.get("vote") if isinstance(item, dict) else getattr(item, "vote", None)
        if not isinstance(question, int) or isinstance(question, bool) or not 1 <= question <= total or question in answers or vote not in ANSWER_VOTES:
            raise invalid
        answers[question] = vote
    return answers


async def submit_exam(db: AsyncSession, user: User, attempt_id: int, raw_answers: list[Any]) -> dict[str, Any]:
    """POST /jury/exam/{attempt_id}/submit。可以不答完就交，没答的按答错算。"""
    now = datetime.now(UTC)
    attempt = await db.get(JuryExamAttempt, attempt_id)
    if attempt is None or attempt.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "exam_not_found", "message": "没有这场考试"})
    closed = HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "exam_closed", "message": "这一场已经交卷或者已经超时"})
    if attempt.submitted_at is not None:
        raise closed
    if await _settle_expired(db, [attempt], now):
        raise closed
    case_ids = [int(case_id) for case_id in json.loads(attempt.question_ids)]
    answers = _parse_answers(raw_answers, len(case_ids))
    cases = {case.id: case for case in (await db.execute(select(ReportCase).where(ReportCase.id.in_(case_ids)))).scalars().all()}
    results: list[dict[str, Any]] = []
    for index, case_id in enumerate(case_ids, start=1):
        vote = answers.get(index)
        case = cases.get(case_id)
        results.append(
            {
                "question": index,
                # 只判「违规 / 不违规」对不对；等级不计分，但标准答案里给出
                "correct": case is not None and bank_service.is_correct(case, vote),
                "your_vote": vote,
                "answer": _answer_view(case) if case is not None else None,
            }
        )
    score = sum(1 for item in results if item["correct"])
    attempt.submitted_at = now
    attempt.answers = json.dumps({str(question): vote for question, vote in answers.items()})
    attempt.score = score
    attempt.passed = score >= EXAM_PASS_SCORE
    await db.flush()
    member = await jury_service.get_member(db, user.id)
    if attempt.passed:
        if await jury_settings.is_enabled(db, "auto_appoint") and jury_service.can_auto_appoint(member):
            # 再看一眼资格：开考到交卷之间可能刚被处罚。不满足就不自动任命，留给本人之后申请。
            if all(item["ok"] for item in await jury_service.eligibility(db, user)):
                member = await jury_service.auto_appoint(db, user, member, now)
        if member is not None:
            await jury_service.grant_exam_points(db, member)
        await points_service.check_achievements(db, user.id, member)
    await db.commit()
    await points_service.flush_events(db)
    from app.websocket.manager import admin_manager

    try:
        await admin_manager.broadcast("admin.jury.changed", {"user_id": user.id})
    except Exception:
        logger.exception("Failed to broadcast exam result", extra={"user_id": user.id})
    state = await _load_state(db, user.id, member, now)
    return {
        "score": score,
        "total": len(case_ids),
        "pass_score": EXAM_PASS_SCORE,
        "passed": bool(attempt.passed),
        "results": results,
        "attempts_left": max(0, EXAM_ATTEMPTS_MAX - state["attempts_used"]),
        "next_attempt_at": state["next_attempt_at"],
        "status": await jury_service.my_status(db, user),
    }


# ---------------------------------------------------------------- 练习场


async def _practice_gate(db: AsyncSession, user: User) -> None:
    """满足四项资格的用户、以及当过委员的人都可以练习。委员受处罚期间不能评审，但练习不拦。"""
    member = await jury_service.get_member(db, user.id)
    if member is not None and (member.serving_since is not None or member.status in {"active", "suspended", "retired"}):
        return
    requirements = await jury_service.eligibility(db, user)
    if not all(item["ok"] for item in requirements):
        raise _not_eligible(requirements)


async def _practice_numbers(db: AsyncSession, user_id: int, now: datetime) -> tuple[int, dict[str, int]]:
    """(今天还能做几道, 我的练习累计)。「今天」和每日评审上限一样按最近 24 小时算。"""
    today = await db.execute(
        select(func.count()).select_from(JuryPracticeAnswer).where(JuryPracticeAnswer.user_id == user_id, JuryPracticeAnswer.created_at >= now - timedelta(hours=24))
    )
    answered, correct = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(sa_case((JuryPracticeAnswer.correct.is_(True), 1), else_=0)), 0)).where(
                JuryPracticeAnswer.user_id == user_id
            )
        )
    ).one()
    return max(0, PRACTICE_DAILY_LIMIT - int(today.scalar_one() or 0)), {"answered": int(answered or 0), "correct": int(correct or 0)}


async def _practice_resets_at(db: AsyncSession, user_id: int, now: datetime) -> datetime | None:
    """练习上限的下一个名额什么时候释放；最近 24 小时没练过为 None。"""
    return await jury_service.rolling_resets_at(db, JuryPracticeAnswer.created_at, JuryPracticeAnswer.user_id == user_id, now=now)


async def practice_next(db: AsyncSession, user: User) -> dict[str, Any]:
    """GET /jury/practice/next。没做过的题优先；都做过后按「最久没做」的顺序重出。"""
    await _practice_gate(db, user)
    now = datetime.now(UTC)
    left, stats = await _practice_numbers(db, user.id, now)
    case_ids = list(
        (
            await db.execute(select(ReportCase.id).where(*bank_service.bank_clauses(pool="practice")).order_by(ReportCase.id.asc()).limit(QUESTION_SCAN_LIMIT))
        ).scalars()
    )
    last_answered = dict(
        (
            await db.execute(
                select(JuryPracticeAnswer.case_id, func.max(JuryPracticeAnswer.id)).where(JuryPracticeAnswer.user_id == user.id).group_by(JuryPracticeAnswer.case_id)
            )
        ).all()
    )
    # 没做过的排前面（按题号）；做过的按上次作答的先后，最久没做的在前
    case_ids.sort(key=lambda case_id: (case_id in last_answered, last_answered.get(case_id, 0), case_id))
    # 要报「这一轮」的进度，所以把我能练的题全部过一遍，不是找到第一道就停。
    cases = await _usable_questions(db, user.id, case_ids)
    question: dict[str, Any] | None = None
    if left > 0 and cases:
        case = cases[0]
        view = await jury_service.juror_case_view(db, case)
        question = {
            "question_id": case.id,
            "verified": bool(case.golden_verified),
            "answered_before": case.id in last_answered,
            "kind": view["kind"],
            "reasons": view["reasons"],
            "report_count": view["report_count"],
            "messages": view["messages"],
            "post": view["post"],
        }
    return {
        "question": question,
        "left_today": left,
        "daily_limit": PRACTICE_DAILY_LIMIT,
        "stats": stats,
        "round": {"total": len(cases), "done": sum(1 for case in cases if case.id in last_answered)},
        "resets_at": await _practice_resets_at(db, user.id, now),
    }


async def practice_answer(db: AsyncSession, user: User, question_id: int, vote: str) -> dict[str, Any]:
    """POST /jury/practice/{question_id}/answer。不计入一致率，不给风纪值。"""
    await _practice_gate(db, user)
    if vote not in ANSWER_VOTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_vote", "message": "选项只能是 none / l1 / l2 / l3"})
    now = datetime.now(UTC)
    case = await db.get(ReportCase, question_id)
    # 只认练习场那一份里的题：否则拿任意题号来「练习」就能问出考试题和质控题的标准答案。
    if (
        case is None
        or bank_service.question_pool(case, await bank_service.is_precedent(db, case)) != "practice"
        or not await _usable_questions(db, user.id, [case.id], limit=1)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "question_not_found", "message": "题目不存在"})
    left, _stats = await _practice_numbers(db, user.id, now)
    if left <= 0:
        resets_at = await _practice_resets_at(db, user.id, now)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "practice_daily_limit",
                "message": f"今天的练习已达上限（{PRACTICE_DAILY_LIMIT} 道），明天再来",
                "resets_at": jury_service._iso(resets_at) if resets_at is not None else None,
            },
        )
    correct = bank_service.is_correct(case, vote)
    db.add(JuryPracticeAnswer(user_id=user.id, case_id=case.id, vote=vote, correct=correct, created_at=now))
    await db.commit()
    left, stats = await _practice_numbers(db, user.id, now)
    return {
        "correct": correct,
        "answer": _answer_view(case),
        "left_today": left,
        "stats": stats,
        "resets_at": await _practice_resets_at(db, user.id, now),
    }
