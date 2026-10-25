"""命令行入口。

常用：
    uv run main.py --check      只读自检：登录、接口、列表、验证码，全程不提交
    uv run main.py --test-mail  发一封测试邮件
    uv run main.py --dry-run    跑到「可以提交」为止，但不真的提交
    uv run main.py              正式运行，到点自动抢
    uv run main.py --status     查看已预约记录
"""

from __future__ import annotations

import argparse
import json
import sys

from seu_lecture import __version__, captcha, log
from seu_lecture.app import Runner, clock_offset
from seu_lecture.client import ApiError, LectureClient, LoginError, SessionExpired
from seu_lecture.config import ConfigError, Settings
from seu_lecture.mailer import Mailer
from seu_lecture.mailer import build as build_mailer
from seu_lecture.model import Lecture, select
from seu_lecture.state import StateError, Store


def _prepare(settings: Settings, *, lock: bool) -> tuple[Store, Mailer | None]:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    log.setup(settings.state_dir / "logs")
    store = Store(settings.state_dir, settings.username, lock=lock)
    return store, build_mailer(settings.smtp)


def cmd_check(settings: Settings) -> int:
    print(f"东南大学讲座预约 v{__version__} 自检\n")
    log.info("配置摘要：\n" + settings.describe())
    client = LectureClient(
        settings.username, settings.password, settings.endpoints,
        use_proxy=settings.use_proxy,
    )

    print("\n[1/5] 登录统一身份认证 …")
    try:
        client.login()
    except LoginError as exc:
        log.error(f"登录失败：{exc}")
        return 1

    print("\n[2/5] 探测讲座接口 …")
    try:
        profile = client.probe()
    except (ApiError, SessionExpired) as exc:
        log.error(f"接口不可用：{exc}")
        return 1

    print("\n[3/5] 拉取讲座列表 …")
    try:
        rows = client.list_lectures()
    except Exception as exc:
        log.error(f"列表请求失败：{type(exc).__name__} {exc}")
        return 1
    log.info(f"接口 {profile.name} 返回 {len(rows)} 条讲座")

    parsed = [item for item in (Lecture.parse(row) for row in rows) if item]
    targets = [item for item in select(rows, settings)]
    if targets:
        log.info(f"按当前筛选条件命中 {len(targets)} 个目标：")
        for lecture in targets:
            log.info(f"  · {lecture}")
    else:
        log.info("当前筛选条件下没有可预约目标。列表中前几条供核对：")
        for lecture in parsed[:10]:
            log.info(f"  · {lecture.title}｜{lecture.category or '无分类'}｜"
                     f"{lecture.seat_text}｜已约={lecture.reserved}")

    print("\n[4/5] 校准服务器时钟 …")
    anchor = (rows[0].get("HD_WID") or rows[0].get("WID")) if rows else None
    if anchor:
        clock_offset(client, anchor)
    else:
        log.info("没有可用讲座，跳过时钟校准")

    print("\n[5/5] 验证码识别 …")
    captcha.warmup()
    try:
        image = client.captcha()
    except Exception as exc:
        log.warn(f"获取验证码失败：{type(exc).__name__}")
        image = None
    if image:
        code = captcha.recognize(image)
        if code:
            log.info(f"验证码识别成功：{code}（识别有误属正常，正式运行会自动重试）")
        else:
            log.warn("验证码识别失败，正式运行时仍会继续尝试")
    else:
        log.warn("没有取到验证码图片，可能该接口需要特定讲座上下文")

    print("\n自检结束，未提交任何预约。")
    if settings.smtp is None:
        print("（未配置邮件，如需通知请填写 .env 中的 SMTP_*）")
    return 0


def cmd_test_mail(settings: Settings) -> int:
    mailer = build_mailer(settings.smtp)
    if mailer is None:
        log.error("未配置邮件。请填写 .env 中的 SMTP_USERNAME / SMTP_PASSWORD")
        return 1
    return 0 if mailer.send("test", "如果你收到这封邮件，说明通知配置正确。") else 1


def cmd_status(settings: Settings) -> int:
    store, _ = _prepare(settings, lock=False)
    data = store.data
    successes = data.get("successes", {})
    print(f"状态文件：{store.path}")
    print(f"已成功预约 {len(successes)} 场")
    for item in successes.values():
        print(f"  · {item.get('title')}｜{item.get('start') or '时间未知'}｜记录于 {item.get('at')}")
    if not successes:
        print("  （暂无记录）")
    mail = data.get("mail") or {}
    if mail:
        print(f"最近邮件：{mail.get('event')} @ {mail.get('at')}（{mail.get('status')}）")
    print("\n原始状态：")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    store.close()
    return 0


def cmd_run(settings: Settings, *, dry_run: bool) -> int:
    store, mailer = _prepare(settings, lock=True)
    try:
        return Runner(settings, store, mailer, dry_run=dry_run).run()
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seu-lecture",
        description="东南大学讲座自动预约（账号密码登录，无需手动 Cookie）",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只读自检，不提交预约")
    group.add_argument("--test-mail", action="store_true", help="发送一封测试邮件")
    group.add_argument("--status", action="store_true", help="查看已预约记录")
    parser.add_argument("--dry-run", action="store_true", help="跑到可提交为止但不真的提交")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    try:
        # 只有单独测邮件时才允许不填账号密码
        settings = Settings.load(require_credentials=not args.test_mail)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if args.check:
        return cmd_check(settings)
    if args.test_mail:
        return cmd_test_mail(settings)
    if args.status:
        return cmd_status(settings)

    log.setup(settings.state_dir / "logs")
    try:
        return cmd_run(settings, dry_run=args.dry_run)
    except StateError as exc:
        log.error(str(exc))
        return 2
    except KeyboardInterrupt:
        log.info("已手动停止")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
