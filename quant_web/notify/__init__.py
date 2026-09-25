"""
提醒推送（第三版）：站内信（一直有）、微信 PushPlus、微信 Server酱、邮件。

send(title, body, level, kind, code, account_id)：
- 先写站内信（交易账本的 alerts 表，网页右上角能看到）；
- 再按设置推送到外部渠道：级别不低于 notify.min_level 才推；免打扰时段只推"紧急"（urgent）；
- 外部渠道失败只记日志，不影响程序运行；返回每个渠道的结果。
级别：info（一般）< warn（重要）< urgent（紧急，例如跌破止损）。
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime, time
from email.header import Header
from email.mime.text import MIMEText
from typing import Any

log = logging.getLogger("quant_web.notify")

LEVELS: dict[str, int] = {"info": 0, "warn": 1, "urgent": 2}
CHANNELS: dict[str, str] = {"web": "网页站内信", "pushplus": "微信（PushPlus）", "serverchan": "微信（Server酱）", "email": "邮件"}


def _settings() -> dict:
    from .. import settings as settings_mod
    return settings_mod.load().notify.model_dump()


def _quiet(cfg: dict, now: datetime) -> bool:
    try:
        a = time.fromisoformat(cfg.get("quiet_start") or "22:30")
        b = time.fromisoformat(cfg.get("quiet_end") or "07:30")
    except ValueError:
        return False
    t = now.time()
    return (a <= t or t < b) if a > b else (a <= t < b)


def _post(url: str, payload: dict, as_json: bool = True) -> Any:
    from .. import net
    import requests

    kw: dict = {"json": payload} if as_json else {"data": payload}
    last: Exception | None = None
    for proxies in (None, net.system_proxies() if hasattr(net, "system_proxies") else None):
        try:
            r = requests.post(url, timeout=10, proxies=proxies, **kw)
            r.raise_for_status()
            return r.json() if "json" in r.headers.get("content-type", "") else r.text
        except Exception as e:  # noqa: BLE001  直连不行再走代理
            last = e
    raise ConnectionError(str(last))


def send_pushplus(token: str, title: str, body: str) -> dict:
    res = _post("https://www.pushplus.plus/send", {"token": token, "title": title, "content": body or title, "template": "txt"})
    if isinstance(res, dict) and res.get("code") not in (200, None):
        raise ConnectionError(res.get("msg") or "PushPlus 返回错误")
    return {"ok": True}


def send_serverchan(key: str, title: str, body: str) -> dict:
    res = _post(f"https://sctapi.ftqq.com/{key}.send", {"title": title[:32], "desp": body or title}, as_json=False)
    if isinstance(res, dict) and res.get("code") not in (0, None):
        raise ConnectionError(res.get("message") or "Server酱返回错误")
    return {"ok": True}


def send_email(cfg: dict, title: str, body: str) -> dict:
    host, port = cfg.get("email_host"), int(cfg.get("email_port") or 465)
    user, pwd, to = cfg.get("email_user"), cfg.get("email_password"), cfg.get("email_to") or cfg.get("email_user")
    if not (host and user and pwd and to):
        raise ValueError("邮箱设置不完整（服务器、账号、授权码、收件人）")
    msg = MIMEText(body or title, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    return {"ok": True}


def deliver(cfg: dict, channel: str, title: str, body: str) -> dict:
    if channel == "pushplus":
        if not cfg.get("pushplus_token"):
            raise ValueError("没有填写 PushPlus token")
        return send_pushplus(cfg["pushplus_token"], title, body)
    if channel == "serverchan":
        if not cfg.get("serverchan_key"):
            raise ValueError("没有填写 Server酱 SendKey")
        return send_serverchan(cfg["serverchan_key"], title, body)
    if channel == "email":
        return send_email(cfg, title, body)
    raise ValueError(f"不认识的推送渠道「{channel}」")


def send(title: str, body: str = "", level: str = "info", kind: str = "general", code: str | None = None,
         account_id: str | None = None, now: datetime | None = None, cfg: dict | None = None) -> dict:
    """写站内信并按设置推送；返回 {web: alert_id, <渠道>: ok/错误原因}"""
    from ..trading import ledger

    out: dict = {}
    with ledger.connect() as c:
        out["web"] = ledger.alert(c, account_id, code, level, kind, title, body)
    try:
        cfg = cfg or _settings()
    except Exception:  # noqa: BLE001
        return out
    now = now or datetime.now()
    if LEVELS.get(level, 0) < LEVELS.get(cfg.get("min_level") or "warn", 1):
        return out
    if level != "urgent" and _quiet(cfg, now):
        out["quiet"] = True
        return out
    for ch in cfg.get("channels") or []:
        if ch == "web":
            continue
        try:
            deliver(cfg, ch, f"【量化助手】{title}", body)
            out[ch] = "ok"
        except Exception as e:  # noqa: BLE001  推送失败不影响程序
            log.warning("推送到 %s 失败：%s", ch, e)
            out[ch] = f"失败：{e}"
    if any(v == "ok" for k, v in out.items() if k != "web"):
        with ledger.connect() as c:
            c.execute("UPDATE alerts SET sent=? WHERE id=?", (",".join(k for k, v in out.items() if v == "ok"), out["web"]))
    return out


def test_channel(channel: str) -> dict:
    """"发送测试"按钮"""
    cfg = _settings()
    if channel == "web":
        return {"ok": True, "message": "站内信一直可用"}
    try:
        deliver(cfg, channel, "【量化助手】测试消息", f"这是一条测试消息，发送时间 {datetime.now():%Y-%m-%d %H:%M:%S}。收到说明推送设置正确。")
        return {"ok": True, "message": "已发送，请查看手机/邮箱"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}
