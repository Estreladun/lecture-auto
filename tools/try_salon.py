"""手动预约沙龙/非讲座类活动（调试用，不参与正式运行）。

正式运行时这些活动会被筛选条件挡掉——典型情况是地点不在你设定的校区，
比如「江苏大剧院」不符合 `SEU_PREFERRED_CAMPUS=九龙湖校区`。
本脚本绕过筛选，直接对目标走一遍完整预约流程，用来验证接口和验证码链路。

不动 `select()` 和 `_rob()` 的任何逻辑，正式运行的行为不受影响。

用法：
    uv run python tools/try_salon.py                # 列出来，交互确认后再提交
    uv run python tools/try_salon.py --dry          # 只取码识别，不提交
    uv run python tools/try_salon.py --yes          # 不确认直接提交
    uv run python tools/try_salon.py --wid <WID>    # 指定某一场
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from seu_lecture import captcha, log  # noqa: E402
from seu_lecture.client import LectureClient, SessionExpired  # noqa: E402
from seu_lecture.config import Settings  # noqa: E402
from seu_lecture.model import SHANGHAI, Lecture  # noqa: E402
from seu_lecture.state import Store  # noqa: E402
from seu_lecture import client as api  # noqa: E402

# 判断「非讲座类活动」的线索，命中分类名或标题即可
ACTIVITY_HINTS = ("沙龙", "购票", "工作坊", "开放日")

RESULT_TEXT = {
    api.SUCCESS: "预约成功",
    api.DUPLICATE: "已经约过（服务端判定重复）",
    api.FULL: "名额已满",
    api.BUSY: "服务器繁忙，稍后再试",
    api.EXPIRED: "会话已失效",
    api.CAPTCHA_WRONG: "验证码不正确",
    api.FAILED: "提交失败",
}


def collect(rows: list[dict], wid: str | None, client: LectureClient) -> list[Lecture]:
    found: list[Lecture] = []
    seen: set[str] = set()
    for row in rows:
        lecture = Lecture.parse(row)
        if lecture is None or lecture.wid in seen:
            continue
        seen.add(lecture.wid)
        if wid:
            if lecture.wid != wid:
                continue
        else:
            text = f"{lecture.category} {lecture.title}"
            if not any(hint in text for hint in ACTIVITY_HINTS):
                continue
        # 列表不带已约人数，补一次详情，好让余量显示成 x/y 而不是「未知」
        if row.get("YYRS") in (None, ""):
            extra = client.detail(lecture.wid)
            if extra:
                merged = Lecture.parse({**row, **extra})
                if merged:
                    lecture = merged
        found.append(lecture)
    return found


def show(lectures: list[Lecture]) -> None:
    for index, lecture in enumerate(lectures, 1):
        mark = ""
        if lecture.reserved:
            mark = "（已预约）"
        elif lecture.full:
            mark = "（名额已满）"
        print(f"\n[{index}] {lecture.title}{mark}")
        print(f"    分类：{lecture.category or '无'}")
        print(f"    地点：{lecture.location or '未知'}   形式：{'线上' if lecture.online else '线下'}")
        print(f"    名额：{lecture.seat_text}   预约窗口：", end="")
        if lecture.reserve_start and lecture.reserve_end:
            print(f"{lecture.reserve_start:%m-%d %H:%M} ~ {lecture.reserve_end:%m-%d %H:%M}")
        else:
            print("未知")
        print(f"    WID：{lecture.wid}")


def main() -> int:
    parser = argparse.ArgumentParser(description="手动预约沙龙类活动（调试用）")
    parser.add_argument("--wid", help="指定讲座 WID，跳过自动识别")
    parser.add_argument("--dry", action="store_true", help="只取码识别，不提交")
    parser.add_argument("--yes", action="store_true", help="跳过确认，直接提交")
    args = parser.parse_args()

    settings = Settings.load()
    client = LectureClient(
        settings.username, settings.password, settings.endpoints,
        use_proxy=settings.use_proxy,
        stage2_code=settings.stage2_code,
        fingerprint=settings.device_fingerprint,
    )

    print("登录 …")
    client.login()
    try:
        client.probe()
    except SessionExpired:
        # 同一账号频繁登录会被踢下线，重登一次即可
        print("会话失效，重新登录 …")
        client.login()
        client.probe()
    rows = client.list_lectures()

    targets = collect(rows, args.wid, client)
    if not targets:
        print(f"没有找到沙龙类活动（线索：{'、'.join(ACTIVITY_HINTS)}）")
        print(f"当前列表共 {len(rows)} 条，可用 --wid 指定具体某一场")
        return 1

    show(targets)

    lecture = targets[0] if len(targets) == 1 else None
    if lecture is None:
        choice = input(f"\n选择要测试的序号（1-{len(targets)}）：").strip()
        if not choice.isdigit() or not 1 <= int(choice) <= len(targets):
            print("序号无效")
            return 1
        lecture = targets[int(choice) - 1]

    if lecture.reserved:
        print("\n这一场你已经有预约了，无需再抢")
        return 0
    if lecture.reserve_end and lecture.reserve_end < datetime.now(SHANGHAI):
        print("\n预约已截止")
        return 0

    print(f"\n目标：{lecture.title}")

    if args.dry:
        print("\n[dry] 取验证码并识别，不提交 …")
        captcha.warmup()
        image = client.captcha()
        code = captcha.recognize(image, rescue=True)
        print(f"[dry] 识别结果：{code or '未解出'}")
        return 0

    if not args.yes:
        answer = input("确认提交预约？输入 yes 继续：").strip().lower()
        if answer != "yes":
            print("已取消")
            return 0

    captcha.warmup()
    print("\n取验证码 …")
    try:
        image = client.captcha()
    except SessionExpired:
        print("会话失效，重新登录")
        client.login()
        image = client.captcha()

    code = captcha.recognize(image, rescue=True)
    if not code:
        print("验证码解不出四位数字，放弃本次测试")
        return 1
    print(f"识别结果：{code}")

    print("提交 …")
    result = client.submit(lecture.wid, code)
    print(f"\n结果：{RESULT_TEXT.get(result, result)}（{result}）")

    if result == api.CAPTCHA_WRONG:
        print("验证码被拒。可以再跑一次试试，或检查模型是否需要补样本。")
    elif result in (api.SUCCESS, api.DUPLICATE):
        # 写本地记录，让 --status 和正式运行保持一致；lock=False 避免占用运行锁
        store = Store(settings.state_dir, settings.username, lock=False)
        store.record_success(lecture)
        print(f"已写入本地记录，共 {store.success_count()} 场")
        print("链路完整跑通。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
