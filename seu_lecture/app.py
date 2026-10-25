"""主流程：登录 → 筛选 → 定时抢位 → 通知。"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

from . import captcha, log
from .client import (
    BUSY,
    CAPTCHA_WRONG,
    DUPLICATE,
    EXPIRED,
    FAILED,
    FULL,
    NOT_OPEN,
    SUCCESS,
    ApiError,
    LectureClient,
    LoginError,
    SessionExpired,
)
from .config import Settings
from .mailer import Mailer
from .model import SHANGHAI, Lecture, select
from .state import Store

CAPTCHA_FAIL_LIMIT = 30
FULL_LOG_INTERVAL = 30.0
# 服务器判定满员后的冷却：先 5 秒，没抢到就翻倍，最多 60 秒。
# 列表的余量字段经常是空的（YYRS 缺失），判断不了是否满员，
# 只能靠服务器返回结果来感知，冷却避免对着满员场次狂打。
FULL_BASE_WAIT = 5.0
FULL_MAX_WAIT = 30.0
# 服务器实际开放时刻常比列表标注的晚几百毫秒到几秒。
# 这时返回「尚未开放」，必须密集探测——空等一轮的机会成本太高。
NOT_OPEN_DELAYS = (0.2, 0.2, 0.3, 0.3, 0.5, 0.7, 1.0)
# 连续几次都解不出四位后，允许「四段均分」兜底给结果
STRICT_CODE_ATTEMPTS = 4
# 连续取不到任何四位结果的次数上限
CODE_MISS_LIMIT = 15
MAX_MISSING = 100  # 连续拉不到目标讲座多少次后放弃


class Runner:
    def __init__(self, settings: Settings, store: Store, mailer: Mailer | None,
                 *, dry_run: bool = False) -> None:
        self.settings = settings
        self.store = store
        self.mailer = mailer
        self.dry_run = dry_run
        self.client = LectureClient(
            settings.username, settings.password, settings.endpoints,
            use_proxy=settings.use_proxy,
        )

        self._scheduler: BlockingScheduler | None = None
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._relogin_lock = threading.Lock()
        self._codes: dict[str, tuple[str, float]] = {}

    # ── 入口 ────────────────────────────────────────────────

    def run(self) -> int:
        log.info("配置摘要：\n" + self.settings.describe())
        if not self._login():
            return 1

        try:
            self.client.probe()
        except SessionExpired as exc:
            log.error(f"接口探测时会话失效：{exc}")
            if not self._relogin():
                return 1
            try:
                self.client.probe()
            except (ApiError, SessionExpired) as retry_exc:
                log.error(f"接口不可用：{retry_exc}")
                self._notify("api_unavailable", str(retry_exc))
                return 1
        except ApiError as exc:
            log.error(f"接口不可用：{exc}")
            self._notify("api_unavailable", str(exc))
            return 1

        captcha.warmup()
        targets = self._targets()
        if not targets:
            return 0
        return self._schedule(targets)

    # ── 登录 ────────────────────────────────────────────────

    def _login(self) -> bool:
        for attempt in range(1, 4):
            try:
                self.client.login()
                return True
            except LoginError as exc:
                log.error(f"登录失败：{exc}")
                if attempt == 3:
                    self._notify("login_failed", str(exc))
                    return False
                time.sleep(3 * attempt)
            except Exception as exc:
                log.error(f"登录异常：{type(exc).__name__} {exc}")
                if attempt == 3:
                    return False
                time.sleep(3 * attempt)
        return False

    def _relogin(self) -> bool:
        with self._relogin_lock:
            for attempt in range(1, 4):
                try:
                    self.client.login()
                    self.client.probe()
                    log.info("重新登录成功，继续任务")
                    return True
                except LoginError as exc:
                    log.error(f"重新登录被拒绝：{exc}")
                    self._notify("login_failed", str(exc))
                    return False
                except Exception as exc:
                    log.warn(f"重新登录失败（第 {attempt} 次）：{type(exc).__name__}")
                    time.sleep(3 * attempt)
            self._notify("session_expired", "连续三次重新登录均失败，程序已停止。")
            return False

    # ── 目标筛选 ────────────────────────────────────────────

    def _targets(self) -> list[Lecture]:
        rows = self.client.list_lectures()
        log.info(f"讲座列表共 {len(rows)} 条")
        candidates = select(rows, self.settings)
        candidates = [item for item in candidates if not self.store.already_done(item.wid)]

        if not candidates:
            log.info("没有符合条件且可预约的讲座。以下为列表中前几条，可用于核对关键词：")
            for row in rows[:8]:
                lecture = Lecture.parse(row)
                if lecture:
                    log.info(f"  · {lecture.title}｜{lecture.category or '无分类'}｜"
                             f"{lecture.seat_text}｜已约={lecture.reserved}")
            return []

        log.info(f"命中 {len(candidates)} 个目标：")
        for lecture in candidates:
            log.info(f"  · {lecture}")
        return candidates

    # ── 调度 ────────────────────────────────────────────────

    def _schedule(self, lectures: list[Lecture]) -> int:
        now = datetime.now(SHANGHAI)
        scheduler = BlockingScheduler(
            timezone="Asia/Shanghai",
            job_defaults={"coalesce": True, "max_instances": 5, "misfire_grace_time": 30},
        )
        self._scheduler = scheduler
        self._pending = {lecture.wid for lecture in lectures}

        scheduler.add_job(
            self._keepalive, "interval",
            seconds=self.settings.keepalive_seconds,
            max_instances=1, coalesce=True, id="keepalive",
        )

        for index, lecture in enumerate(lectures):
            if lecture.reserve_start and lecture.reserve_start > now:
                prefetch_at = lecture.reserve_start - timedelta(
                    seconds=self.settings.prefetch_seconds
                )
                if prefetch_at > now:
                    scheduler.add_job(self._prefetch, "date", run_date=prefetch_at,
                                      args=[lecture], id=f"prefetch-{lecture.wid}")
                    log.info(f"将于 {prefetch_at:%H:%M:%S} 预取验证码")
                run_at = lecture.reserve_start + timedelta(milliseconds=index * 50)
                log.info(f"将于 {run_at:%H:%M:%S} 开始抢位：{lecture.title}")
            else:
                run_at = now + timedelta(milliseconds=index * 50)
                log.info(f"讲座已开放，立即开始抢位/候补：{lecture.title}")
            scheduler.add_job(self._rob, "date", run_date=run_at,
                              args=[lecture], id=f"rob-{lecture.wid}")

        if self.settings.report_hour and self.mailer is not None:
            scheduler.add_job(self._daily_report, "cron",
                              hour=self.settings.report_hour, minute=0, id="report")

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("收到中断信号，正在停止")
        finally:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                pass
        return 0

    def _finish(self, lecture: Lecture) -> None:
        with self._lock:
            self._pending.discard(lecture.wid)
            done = not self._pending
        if done and self._scheduler is not None:
            log.info("全部目标已处理完毕，程序退出")
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass

    # ── 保活 ────────────────────────────────────────────────

    def _keepalive(self) -> None:
        try:
            self.client.list_lectures()
            log.info("会话保活成功")
        except SessionExpired:
            log.warn("会话已过期，尝试重新登录")
            self._relogin()
        except Exception as exc:
            log.warn(f"保活失败：{type(exc).__name__}")

    # ── 验证码 ──────────────────────────────────────────────

    def _fresh_code(self, wid: str, *, allow_rescue: bool = False) -> str | None:
        """取一张新验证码并识别。

        只要解出四位数字就返回，不做置信度筛选。allow_rescue 仅对 ddddocr
        回退生效：解不出四位时是否允许「四段均分」强行给结果。
        """
        try:
            image = self.client.captcha()
        except SessionExpired:
            raise
        except Exception as exc:
            log.warn(f"获取验证码异常：{type(exc).__name__}")
            return None
        if not image:
            return None
        return captcha.recognize(image, rescue=allow_rescue)

    def _prefetch(self, lecture: Lecture) -> None:
        try:
            code = self._fresh_code(lecture.wid)
        except SessionExpired:
            self._relogin()
            return
        if code:
            self._codes[lecture.wid] = (code, time.time())
            log.info(f"预取验证码成功：{code}")

    def _take_code(self, wid: str) -> str | None:
        item = self._codes.pop(wid, None)
        if item and time.time() - item[1] <= 10:
            return item[0]
        return None

    # ── 抢位主循环 ──────────────────────────────────────────

    def _current(self, wid: str) -> Lecture | None:
        rows = self.client.list_lectures()
        for row in rows:
            if (row.get("HD_WID") or row.get("WID")) == wid:
                return Lecture.parse(row)
        return None

    def _rob(self, lecture: Lecture) -> None:
        wid = lecture.wid
        # 多个讲座是并行抢的，日志前缀用于区分是谁在说话
        short = lecture.title[:6] + ("…" if len(lecture.title) > 6 else "")
        tag = f"[{short}]"
        log.info(f"{tag} 开始抢位：{lecture}")
        end_at = lecture.reserve_end
        cached = self._take_code(wid)
        submit_directly = cached is not None
        burst = self.settings.burst_attempts if cached is None else 0
        last_full_log = 0.0
        captcha_failures = 0
        code_misses = 0
        not_open = 0
        missing = 0
        attempts = 0
        full_wait = FULL_BASE_WAIT
        full_until = 0.0

        try:
            while True:
                now = datetime.now(SHANGHAI)
                if end_at and now > end_at:
                    log.info(f"预约已截止（{end_at:%m-%d %H:%M}），放弃：{lecture.title}")
                    return

                # 满员冷却期：只等，不取码也不提交，避免对着满员场次空打
                if time.monotonic() < full_until:
                    time.sleep(self.settings.poll_seconds)
                    continue

                if submit_directly:
                    current = lecture
                    submit_directly = False
                else:
                    try:
                        current = self._current(wid)
                    except SessionExpired:
                        if not self._relogin():
                            return
                        continue
                    except Exception as exc:
                        log.warn(
                            f"刷新讲座状态失败：{type(exc).__name__}，"
                            f"{self.settings.poll_seconds:g} 秒后重试"
                        )
                        time.sleep(self.settings.poll_seconds)
                        continue

                    if current is None:
                        missing += 1
                        if missing >= MAX_MISSING:
                            log.warn(f"目标讲座已从列表消失，放弃：{lecture.title}")
                            return
                        if missing == 1:
                            log.warn(
                                f"目标讲座暂不在列表中，每 {self.settings.poll_seconds:g} 秒继续查找"
                            )
                        time.sleep(self.settings.poll_seconds)
                        continue
                    missing = 0
                    if current.reserved:
                        self._succeed(current)
                        return
                    if current.reserve_end:
                        end_at = current.reserve_end
                    if current.full:
                        if time.monotonic() - last_full_log >= FULL_LOG_INTERVAL:
                            log.info(
                                f"{tag} 名额已满（{current.seat_text}），"
                                f"每 {self.settings.poll_seconds:g} 秒检查一次，等待他人取消……"
                            )
                            last_full_log = time.monotonic()
                        burst = 0
                        time.sleep(self.settings.poll_seconds)
                        continue

                if self.dry_run:
                    log.info(f"[试运行] 命中可提交状态（{current.seat_text}），不实际提交")
                    return

                code = cached
                cached = None
                busy_attempt = 0
                while True:
                    try:
                        if not code:
                            code = self._fresh_code(
                                wid, allow_rescue=code_misses >= STRICT_CODE_ATTEMPTS
                            )
                        if not code:
                            # 识别不可靠：直接换一张，不浪费一次提交机会
                            code_misses += 1
                            if code_misses >= CODE_MISS_LIMIT:
                                log.error(
                                    f"连续 {code_misses} 次取不到可信验证码，放弃：{lecture.title}"
                                )
                                self._notify("captcha_locked", f"讲座：{lecture.title}")
                                return
                            continue
                        result = self.client.submit(wid, code)
                    except SessionExpired:
                        if not self._relogin():
                            return
                        code = None
                        break
                    except Exception as exc:
                        log.warn(f"提交异常：{type(exc).__name__}")
                        result = FAILED

                    code = None
                    attempts += 1

                    if result in (SUCCESS, DUPLICATE):
                        self._succeed(current)
                        return
                    if result == EXPIRED:
                        log.warn("提交时会话已失效，重新登录后继续")
                        if not self._relogin():
                            return
                        break
                    if result == FULL:
                        burst = 0
                        full_until = time.monotonic() + full_wait
                        log.info(
                            f"{tag} 名额已满，{full_wait:g} 秒后再探"
                            f"（期间只等待，不提交）"
                        )
                        full_wait = min(full_wait * 2, FULL_MAX_WAIT)
                        break
                    if result == NOT_OPEN:
                        # 不跳出内层：跳出会回到外层刷新列表，白白浪费一个来回。
                        # 这里只重新取码+提交，把开放瞬间的每一次机会都抓住。
                        not_open += 1
                        attempts -= 1  # 「尚未开放」不是失败，别让它吃掉重试额度
                        delay = NOT_OPEN_DELAYS[min(not_open - 1, len(NOT_OPEN_DELAYS) - 1)]
                        if not_open <= 3 or not_open % 20 == 0:
                            log.info(
                                f"{tag} 服务器尚未开放预约，{delay:.2f} 秒后再试"
                                f"（第 {not_open} 次）"
                            )
                        time.sleep(delay)
                        if end_at and datetime.now(SHANGHAI) > end_at:
                            return
                        continue
                    if result == BUSY:
                        delays = self.settings.busy_delays
                        delay = delays[min(busy_attempt, len(delays) - 1)]
                        delay += random.uniform(0, self.settings.busy_jitter)
                        busy_attempt += 1
                        log.info(f"{tag} 服务器繁忙，{delay:.2f} 秒后重试")
                        time.sleep(delay)
                        if end_at and datetime.now(SHANGHAI) > end_at:
                            return
                        continue
                    if result == CAPTCHA_WRONG:
                        captcha_failures += 1
                        log.warn(
                            f"{tag} 验证码不正确（第 {captcha_failures} 次），立即换一张重试"
                        )
                        if captcha_failures >= CAPTCHA_FAIL_LIMIT:
                            break  # 交给外层统一收尾
                        continue
                    break  # FAILED：交给外层 burst 逻辑

                if attempts and self.settings.max_attempts and attempts >= self.settings.max_attempts:
                    log.warn(f"已达最大尝试次数 {self.settings.max_attempts}，放弃：{lecture.title}")
                    return
                if captcha_failures >= CAPTCHA_FAIL_LIMIT:
                    log.error(f"连续 {captcha_failures} 次验证码失败，放弃：{lecture.title}")
                    self._notify("captcha_locked", f"讲座：{lecture.title}")
                    return

                if burst > 1:
                    burst -= 1
                    log.info(f"{tag} 提交未成功，立即重试（剩余 {burst} 次快速尝试）")
                    time.sleep(0.01)
                else:
                    burst = 0
                    log.info(
                        f"{tag} 提交未成功，{self.settings.poll_seconds:g} 秒后继续候补"
                    )
                    time.sleep(self.settings.poll_seconds)
        finally:
            self._finish(lecture)

    # ── 结果与通知 ──────────────────────────────────────────

    def _succeed(self, lecture: Lecture) -> None:
        log.info(f"预约成功：{lecture.title}")
        self.store.record_success(lecture)
        detail = [
            f"讲座：{lecture.title}",
            f"时间：{lecture.start:%Y-%m-%d %H:%M}" if lecture.start else "时间：未知",
            f"地点：{lecture.location or '未知'}",
            f"形式：{'线上' if lecture.online else '线下'}",
            f"累计成功：{self.store.success_count()} 场",
        ]
        self._notify("reservation_succeeded", "\n".join(detail))

    def _daily_report(self) -> None:
        if not self.store.report_pending(self.settings.report_hour):
            return
        successes = self.store.data.get("successes", {})
        lines = [f"当前已成功预约 {len(successes)} 场："]
        for item in successes.values():
            lines.append(f"  · {item.get('title')}（{item.get('start') or '时间未知'}）")
        pending_ids = sorted(self._pending)
        lines.append(f"仍在等待的目标：{len(pending_ids)} 个")
        if self.mailer and self.mailer.send("daily_report", "\n".join(lines)):
            self.store.mark_reported()

    def _notify(self, event: str, body: str = "") -> None:
        if self.mailer is None:
            return
        ok = self.mailer.send(event, body)
        self.store.record_mail(event, "ok" if ok else "failed")
