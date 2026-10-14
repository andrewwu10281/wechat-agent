"""微信接入层（官方后端版）：用 openclaw-weixin 的官方 HTTP 协议接入微信。

原理（与 wcferry 完全不同）：
    - 直连腾讯官方微信后端 https://ilinkai.weixin.qq.com
    - 扫码登录 + 长轮询收发消息，不注入微信客户端、不碰 DLL
    - 无「版本必须匹配」的坑、无 Hook 封号风险
    - 大脑仍然用 agent.py（DeepSeek + 天气/算数/Scholar 三工具），一个字没改

用法：
    1. 安装依赖： .venv\\Scripts\\pip install -r requirements.txt
    2. 首次运行： .venv\\Scripts\\python bot.py
       -> 弹出二维码，用手机微信「扫一扫」扫码并确认
    3. 登录成功后自动长轮询，别人给小号发消息就自动回复
    4. 换号 / 凭证失效： .venv\\Scripts\\python bot.py --relogin

凭证保存在同目录 .wcbot_session.json（已加入 .gitignore，勿提交）。
协议细节参照官方插件 openclaw-weixin（Tencent/openclaw-weixin）。
"""
import base64
import json
import logging
import os
import random
import secrets
import sys
import time
from urllib.parse import quote

import requests

from agent import run

# ---------------------------------------------------------------------------
# 常量：与官方插件 openclaw-weixin 当前版本（2.4.9）保持一致
# ---------------------------------------------------------------------------
FIXED_BASE_URL = "https://ilinkai.weixin.qq.com"   # 二维码登录的固定入口
BOT_TYPE = "3"                                     # 个人 bot
CHANNEL_VERSION = "2.4.9"                          # 上报的渠道版本（package.json 里的 version）
ILINK_APP_ID = "bot"                               # 上报的应用 ID
BOT_AGENT = "wechat-agent/1.0.0"                   # 观测标识，仅日志归因用，不参与鉴权


def _enc_client_version(version: str) -> str:
    """'2.4.9' -> iLink-App-ClientVersion：按 0x00MMNNPP 编码后转十进制。"""
    major, minor, patch = (version.split(".") + ["0", "0", "0"])[:3]
    return str(((int(major) & 0xFF) << 16) | ((int(minor) & 0xFF) << 8) | (int(patch) & 0xFF))


ILINK_APP_CLIENT_VERSION = _enc_client_version(CHANNEL_VERSION)  # 2.4.9 -> "132105"

SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wcbot_session.json")
QR_PNG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wcbot_qr.png")
MAX_QR_REFRESH = 3

LOG = logging.getLogger("wechat-bot")


# ---------------------------------------------------------------------------
# 请求头 / 公共字段
# ---------------------------------------------------------------------------
def _random_wechat_uin() -> str:
    """X-WECHAT-UIN 头：随机 uint32 -> 十进制字符串 -> base64。"""
    return base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode("ascii")


def _common_headers() -> dict:
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }


def _post_headers(token: str = "") -> dict:
    h = {
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
        **_common_headers(),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


# ---------------------------------------------------------------------------
# 二维码登录
# ---------------------------------------------------------------------------
def _fetch_qrcode() -> dict:
    url = f"{FIXED_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}"
    resp = requests.post(url, headers=_post_headers(), json={"local_token_list": []}, timeout=20)
    resp.raise_for_status()
    return resp.json()  # {"qrcode": ..., "qrcode_img_content": ...}


def _poll_qrcode_status(qrcode: str, base_url: str, verify_code: str | None = None) -> dict:
    url = f"{base_url}/ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
    if verify_code:
        url += f"&verify_code={quote(verify_code, safe='')}"
    # GET 只带应用头，不带鉴权头；35s 长轮询
    try:
        resp = requests.get(url, headers=_common_headers(), timeout=35)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        return {"status": "wait"}
    except requests.exceptions.RequestException:
        return {"status": "wait"}


def _show_qrcode(content: str) -> None:
    """展示二维码：存 PNG 用图片查看器打开 + 打印备用链接。"""
    print("\n请用手机微信「扫一扫」扫描下面的二维码登录：\n")
    try:
        import qrcode
        img = qrcode.make(content)
        img.save(QR_PNG_FILE)
        try:
            os.startfile(QR_PNG_FILE)  # Windows：用图片查看器打开，扫码更稳
        except Exception:
            pass
        print("（二维码图片已生成，若未自动弹出，可手动打开 .wcbot_qr.png）")
    except Exception as e:
        print(f"（二维码图片生成失败：{e}，请用下面的链接）")
    print(f"\n备用链接（微信里打开或扫码）：{content}\n")


def _fetch_and_show_qr() -> tuple[str, str]:
    data = _fetch_qrcode()
    qrcode = data.get("qrcode", "")
    qr_url = data.get("qrcode_img_content", "") or qrcode
    if not qrcode:
        raise RuntimeError(f"获取二维码失败，服务器返回：{data}")
    _show_qrcode(qr_url)
    return qrcode, qr_url


def login_via_qr() -> dict:
    """扫码登录，返回凭证 dict（bot_token / ilink_bot_id / baseurl / ilink_user_id）。"""
    print("正在获取二维码…")
    qrcode, _ = _fetch_and_show_qr()
    base_url = FIXED_BASE_URL
    verify_code: str | None = None
    refresh_count = 0

    while True:
        try:
            st = _poll_qrcode_status(qrcode, base_url, verify_code)
        except Exception as e:
            print(f"\n轮询二维码状态出错：{e}，稍后重试…")
            time.sleep(2)
            continue

        status = st.get("status", "")

        if status == "wait":
            sys.stdout.write(".")
            sys.stdout.flush()
        elif status == "scaned":
            print("\n已扫码，请在手机上确认…")
            verify_code = None
        elif status == "need_verifycode":
            verify_code = input("请输入手机微信显示的验证码数字：").strip()
            continue
        elif status == "verify_code_blocked":
            print("\n⛔ 验证码错误次数过多。")
            verify_code = None
            refresh_count += 1
            if refresh_count > MAX_QR_REFRESH:
                raise RuntimeError("验证码多次错误，登录失败，请稍后再试。")
            qrcode, _ = _fetch_and_show_qr()
            base_url = FIXED_BASE_URL
        elif status == "expired":
            refresh_count += 1
            if refresh_count > MAX_QR_REFRESH:
                raise RuntimeError("二维码多次过期，登录失败，请稍后再试。")
            print("\n二维码已过期，正在刷新…")
            qrcode, _ = _fetch_and_show_qr()
            base_url = FIXED_BASE_URL
        elif status == "scaned_but_redirect":
            host = st.get("redirect_host")
            if host:
                base_url = f"https://{host}"
                print(f"\n（已切换到 {host} 继续轮询…）")
        elif status == "binded_redirect":
            raise RuntimeError("该 bot 账号已绑定到其它实例。请确认没有别处在运行它，或换一个微信小号。")
        elif status == "confirmed":
            if not st.get("ilink_bot_id"):
                raise RuntimeError("登录已确认，但服务器未返回 ilink_bot_id。")
            return {
                "bot_token": st.get("bot_token"),
                "ilink_bot_id": st.get("ilink_bot_id"),
                "baseurl": st.get("baseurl") or FIXED_BASE_URL,
                "ilink_user_id": st.get("ilink_user_id"),
            }
        else:
            LOG.warning("未知二维码状态：%s（%s）", status, st)

        time.sleep(1)


# ---------------------------------------------------------------------------
# 凭证持久化
# ---------------------------------------------------------------------------
def save_session(sess: dict) -> None:
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(sess, f, ensure_ascii=False, indent=2)


def load_session() -> dict | None:
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            sess = json.load(f)
        if sess.get("bot_token") and sess.get("baseurl"):
            return sess
    except Exception as e:
        LOG.warning("读取本地凭证失败：%s", e)
    return None


# ---------------------------------------------------------------------------
# Bot API：长轮询 + 发消息
# ---------------------------------------------------------------------------
def get_updates(sess: dict, cursor: str, timeout_ms: int) -> dict:
    url = f"{sess['baseurl']}/ilink/bot/getupdates"
    body = {"get_updates_buf": cursor, "base_info": _base_info()}
    # 客户端超时比服务端建议的多留 5s，避免在「无消息」返回前自己先断开
    try:
        resp = requests.post(
            url,
            headers=_post_headers(sess["bot_token"]),
            json=body,
            timeout=timeout_ms / 1000 + 5,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        return {"ret": 0, "msgs": [], "get_updates_buf": cursor}
    except requests.exceptions.RequestException as e:
        LOG.warning("getUpdates 网络错误：%s", e)
        return {"ret": 0, "msgs": [], "get_updates_buf": cursor}


def send_text(sess: dict, to_user_id: str, text: str, context_token: str = "") -> dict:
    url = f"{sess['baseurl']}/ilink/bot/sendmessage"
    msg = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": secrets.token_hex(16),
        "message_type": 2,   # Bot
        "message_state": 2,  # 完成
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    body = {"msg": msg, "base_info": _base_info()}
    resp = requests.post(url, headers=_post_headers(sess["bot_token"]), json=body, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("ret") not in (0, None):
        raise RuntimeError(f"sendMessage ret={data.get('ret')} errmsg={data.get('errmsg')}")
    return data


def _notify(sess: dict, endpoint: str) -> None:
    """启动/停止通知，尽力而为，失败不影响收发。"""
    try:
        requests.post(
            f"{sess['baseurl']}/ilink/bot/msg/{endpoint}",
            headers=_post_headers(sess["bot_token"]),
            json={"base_info": _base_info()},
            timeout=10,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 消息处理
# ---------------------------------------------------------------------------
def _extract_text(msg: dict) -> str:
    for item in msg.get("item_list", []):
        if item.get("type") == 1 and item.get("text_item", {}).get("text"):
            return item["text_item"]["text"]
    return ""


def _handle_msg(sess: dict, msg: dict, context_tokens: dict) -> None:
    # 只处理用户发来的消息（message_type==1），忽略 bot 自己发的（2）
    if msg.get("message_type") != 1:
        return
    text = _extract_text(msg).strip()
    if not text:
        return
    from_user = msg.get("from_user_id", "")
    ctx = msg.get("context_token", "")
    if not from_user:
        return

    LOG.info("收到消息来自 %s：%s", from_user, text)
    if ctx:
        context_tokens[from_user] = ctx

    try:
        answer = run(text)
    except Exception as e:
        LOG.exception("调用 agent 失败")
        answer = f"抱歉，出了点问题：{e}"

    send_text(sess, from_user, answer, ctx or context_tokens.get(from_user, ""))
    LOG.info("已回复 %s", from_user)


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    force_relogin = "--relogin" in sys.argv
    sess = None if force_relogin else load_session()

    if not sess:
        print("首次使用（或重新登录），需要扫码连接微信…")
        sess = login_via_qr()
        save_session(sess)
        print(f"\n✅ 登录成功！bot id: {sess['ilink_bot_id']}")
    else:
        print(f"✅ 已使用本地凭证，bot id: {sess.get('ilink_bot_id')}")

    _notify(sess, "notifystart")
    print("\n===== 微信机器人已启动，开始接收消息（Ctrl+C 退出）=====\n")

    cursor = ""
    timeout_ms = 35_000
    context_tokens: dict = {}  # user_id -> 最近一次 context_token

    try:
        while True:
            try:
                resp = get_updates(sess, cursor, timeout_ms)
            except KeyboardInterrupt:
                break

            if resp.get("ret") not in (0, None):
                errcode = resp.get("errcode")
                LOG.warning("getUpdates ret=%s errcode=%s errmsg=%s", resp.get("ret"), errcode, resp.get("errmsg"))
                # -14：账号会话被暂停，休息更久再试
                time.sleep(60 if errcode == -14 else 2)
                continue

            buf = resp.get("get_updates_buf")
            if buf:
                cursor = buf
            if resp.get("longpolling_timeout_ms"):
                timeout_ms = max(10_000, min(60_000, int(resp["longpolling_timeout_ms"])))

            for msg in resp.get("msgs", []):
                try:
                    _handle_msg(sess, msg, context_tokens)
                except Exception:
                    LOG.exception("处理消息时出错")
    finally:
        _notify(sess, "notifystop")
        print("\n已停止。")


if __name__ == "__main__":
    main()
