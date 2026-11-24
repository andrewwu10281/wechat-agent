"""微信接入层（官方后端版）：用 openclaw-weixin 的官方 HTTP 协议接入微信。

原理（与 wcferry 完全不同）：
    - 直连腾讯官方微信后端 https://ilinkai.weixin.qq.com
    - 扫码登录 + 长轮询收发消息，不注入微信客户端、不碰 DLL
    - 无「版本必须匹配」的坑、无 Hook 封号风险
    - 大脑仍然用 agent.py（DeepSeek + 通用工具），接入层与大脑完全解耦

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
import hashlib
import json
import logging  #打日志（LOG.info(...)）
import os       #拼文件路径、判断文件在不在、用看图软件打开二维码
import random
import secrets
import sys      #获取命令行参数/向标准输出写内容
import time
from urllib.parse import quote
import requests  #发 HTTP 请求（收消息、发消息)
from agent import run, compact_history
from tools.context import set_current_user, get_current_user
from tools import media as media_tools
from tools.general import peek_pending_approval, arm_pending_approval, deny_pending_approval
from tools.todos import due_reminders, mark_reminded


MAX_QR_REFRESH = 3
# 历史条数硬上限（真正的控制是 token 预算：超预算会把早期对话压成摘要）
MAX_HISTORY = 200
DEBUG_MSG = os.getenv("DEBUG_MSG", "") == "1"   # 设 DEBUG_MSG=1 时打印收到的原始消息（调试群聊用）
LOG = logging.getLogger("wechat-bot") #在模块顶层创建，文件里所有函数都能直接引用LOG

# 审批用词表：用户回复这些词视为"同意执行刚才那条危险命令"
_NEGATIVE = ("不要", "不用", "取消", "算了", "拒绝", "别", "不同意", "不是", "no", "cancel", "n")
_AFFIRMATIVE = {"同意", "可以", "确认", "确定", "批准", "好的", "好", "行", "执行吧", "干吧",
                "yes", "ok", "approve", "y", "嗯"}


def _is_negative(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(k in t for k in _NEGATIVE)


def _is_affirmative(text: str) -> bool:
    """判断用户是不是在同意执行刚被拦下的危险命令（否定优先，避免"不同意"被当成同意）。"""
    t = (text or "").strip().lower().rstrip("。.!！~ ")
    if not t or _is_negative(t):
        return False
    if t in _AFFIRMATIVE:
        return True
    return any(k in t for k in ("同意", "确认", "批准"))



# bot.py 干的事：

# 1. 看看本地有没有存着"通行证"（.wcbot_session.json）
#    ├─ 有  → 直接用
#    └─ 没有 → 扫码登录，领一张，存起来

# 2. 开始无限循环：
#    a. 问腾讯："有新消息吗？"（最多等 35 秒）
#    b. 有消息 → 把文字取出来
#    c. 过滤：是不是用户发的？是不是自己发的？是不是重复的？
#    d. 交给大脑：run(文字, 这个用户的历史)
#    e. 拿到回答
#    f. 把回答发回腾讯（带上通行证和 context_token）
#    g. 回到 a



# ---------------------------------------------------------------------------
# 常量：与官方插件 openclaw-weixin 当前版本（2.4.9）保持一致
# ---------------------------------------------------------------------------


# 二维码登录的固定入口 1._fetch_qrcode()：获取二维码时用。2.login_via_qr()：作为初始 base_url
FIXED_BASE_URL = "https://ilinkai.weixin.qq.com"   

# “3”表示个人 bot，在_fetch_qrcode() 里拼 URL
BOT_TYPE = "3"   

 # 上报的渠道版本（package.json 里的 version）
CHANNEL_VERSION = "2.4.9"                         

# 上报的应用 ID，在_common_headers() 里拼 HTTP 头，服务器靠它识别“这是 bot 类应用
ILINK_APP_ID = "bot"                               


BOT_AGENT = "wechat-agent/1.0.0"                   # 观测标识，仅日志归因用，不参与鉴权

#把版本号编码好
def _enc_client_version(version: str) -> str:
    """'2.4.9' -> iLink-App-ClientVersion：按 0x00MMNNPP 编码后转十进制。"""
    major, minor, patch = (version.split(".") + ["0", "0", "0"])[:3]
    return str(((int(major) & 0xFF) << 16) | ((int(minor) & 0xFF) << 8) | (int(patch) & 0xFF))


ILINK_APP_CLIENT_VERSION = _enc_client_version(CHANNEL_VERSION)  # 2.4.9 -> "132105"


#计算凭证文件和二维码文件的路径并固定，避免频繁扫码登陆
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wcbot_session.json")
QR_PNG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wcbot_qr.png")






# ---------------------------------------------------------------------------
# 请求头 / 公共字段
#这一块有四个函数，都在拼接发给微信服务器的 HTTP 请求里要带的东西
# ---------------------------------------------------------------------------

#协议要求
def _random_wechat_uin() -> str:
    """X-WECHAT-UIN 头：随机 uint32 -> 十进制字符串 -> base64。"""
    return base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode("ascii")


#所有请求都要带的两个头：我是哪个应用、我是什么版本。
def _common_headers() -> dict:
    return {
        "iLink-App-Id": ILINK_APP_ID, #告诉服务器“我是 bot 类应用”
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION, #告诉服务器“我模拟的是 2.4.9 版协议”
    }


#在common基础上加鉴权和UIN（二维码轮询GET请求只用common)
def _post_headers(token: str = "") -> dict:
    h = {
        "AuthorizationType": "ilink_bot_token", #鉴权类型
        "X-WECHAT-UIN": _random_wechat_uin(),  #随机生成 X-WECHAT-UIN
        **_common_headers(), #把两个公共头合并进来,**是把字典展开的写法
    }
    if token:
        h["Authorization"] = f"Bearer {token}"  #拿到凭证就加一条
    return h
    



#公共字段，每次请求告诉服务器“渠道版本”和“客户端标识”
def _base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}
    #区别：请求头里的 iLink-App-ClientVersion 是给 HTTP 层/网关看的（快速过滤），请求体里的 channel_version 是给业务逻辑层看的（详细处理）




# ---------------------------------------------------------------------------
# 二维码登录
# ---------------------------------------------------------------------------


#向腾讯要二维码
def _fetch_qrcode() -> dict:
    url = f"{FIXED_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}"
    resp = requests.post(url, headers=_post_headers(), json={"local_token_list": []}, timeout=20)
    resp.raise_for_status() #如果服务器返回错误状态码就抛异常，不让程序装成功
    return resp.json()  # {"qrcode": ...二维码id, "qrcode_img_content": ...通常是链接}


#问"扫了没、确认没"
def _poll_qrcode_status(qrcode: str, base_url: str, verify_code: str | None = None) -> dict:
    url = f"{base_url}/ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"#quote把二维码id做url编码
    if verify_code:
        url += f"&verify_code={quote(verify_code, safe='')}"
    # GET 只带应用头，不带鉴权头；35s 长轮询（还没登陆，没有token)
    try:
        resp = requests.get(url, headers=_common_headers(), timeout=35)
        resp.raise_for_status()
        return resp.json() #把回信解析成字典返回
    except requests.exceptions.Timeout: #超时
        return {"status": "wait"}
    except requests.exceptions.RequestException:  #网络错误
        return {"status": "wait"}
        #这两种情况不应该让用户重新扫码。把异常转成 wait 状态，外层循环会自然重试


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
    data = _fetch_qrcode()  #要二维码
    qrcode = data.get("qrcode", "")  #取出编号，没有就返回空字符串
    qr_url = data.get("qrcode_img_content", "") or qrcode #取二维码内容；如果为空，就用编号代替
    if not qrcode:  #编号都没有，说明服务器没给成功，直接报错
        raise RuntimeError(f"获取二维码失败，服务器返回：{data}")#抛出异常
    _show_qrcode(qr_url)
    return qrcode, qr_url

#状态分支
def login_via_qr() -> dict:
    """扫码登录，返回凭证 dict（bot_token / ilink_bot_id / baseurl / ilink_user_id）。"""
    print("正在获取二维码…")
    qrcode, _ = _fetch_and_show_qr()
    base_url = FIXED_BASE_URL
    verify_code: str | None = None
    refresh_count = 0

    while True:
        try:
            st = _poll_qrcode_status(qrcode, base_url, verify_code) #调轮询
        except Exception as e:  #接住内层没接的所有异常，保证循环不崩溃
            print(f"\n轮询二维码状态出错：{e}，稍后重试…")
            time.sleep(2)
            continue

        status = st.get("status", "")

        if status == "wait":  #还没扫
            sys.stdout.write(".")
            sys.stdout.flush() #强制立即刷新，否则点可能积在缓冲区不显示
        elif status == "scaned":  #扫了，等你手机确认
            print("\n已扫码，请在手机上确认…")
            verify_code = None
        elif status == "need_verifycode":  #要输验证码
            verify_code = input("请输入手机微信显示的验证码数字：").strip()
            continue
        elif status == "verify_code_blocked":  #验证码输错太多次
            print("\n⛔ 验证码错误次数过多。")
            verify_code = None
            refresh_count += 1
            if refresh_count > MAX_QR_REFRESH:
                raise RuntimeError("验证码多次错误，登录失败，请稍后再试。")
            qrcode, _ = _fetch_and_show_qr()
            base_url = FIXED_BASE_URL
        elif status == "expired":  #二维码过期
            refresh_count += 1
            if refresh_count > MAX_QR_REFRESH:
                raise RuntimeError("二维码多次过期，登录失败，请稍后再试。")
            print("\n二维码已过期，正在刷新…")
            qrcode, _ = _fetch_and_show_qr()
            base_url = FIXED_BASE_URL
        elif status == "scaned_but_redirect":  #扫码了，但服务器要求切换 host
            host = st.get("redirect_host")
            if host:
                base_url = f"https://{host}"
                print(f"\n（已切换到 {host} 继续轮询…）")
        elif status == "binded_redirect":  #已被别处绑定
            raise RuntimeError("该 bot 账号已绑定到其它实例。请确认没有别处在运行它，或换一个微信小号。")
        elif status == "confirmed":  #扫码确认成功，服务器返回凭证
            if not st.get("ilink_bot_id"):
                raise RuntimeError("登录已确认，但服务器未返回 ilink_bot_id。")
            return {
                "bot_token": st.get("bot_token"),#鉴权凭证，后续所有请求的Authorization: Bearer <token>
                "ilink_bot_id": st.get("ilink_bot_id"),#bot 唯一 ID，用于识别自己、去重
                "baseurl": st.get("baseurl") or FIXED_BASE_URL,#登录后的服务器地址
                "ilink_user_id": st.get("ilink_user_id"),#授权你的用户 ID
                #这个字典返回给 main()，然后 save_session() 把它写进 .wcbot_session.json
            }
        else:
            LOG.warning("未知二维码状态：%s（%s）", status, st)

        time.sleep(1)




# ---------------------------------------------------------------------------
# 凭证持久化
# ---------------------------------------------------------------------------

#存写凭证
def save_session(sess: dict) -> None:
    #with是上下文管理器，保证文件用完后自动关闭，即使中间出错也会关
    with open(SESSION_FILE, "w", encoding="utf-8") as f:  #把文件对象赋给变量 f
        json.dump(sess, f, ensure_ascii=False, indent=2) #把字典sess序列化成 JSON 字符串并写入文件 f（允许中文原样写入，格式化缩进两格）


#读凭证
def load_session() -> dict | None:
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            sess = json.load(f) #从文件对象读取并反序列化成 Python 对象
        if sess.get("bot_token") and sess.get("baseurl"): #鉴权凭证和服务器地址（.get，字段不存在返回None）
            return sess
    except Exception as e:
        LOG.warning("读取本地凭证失败：%s", e)
    return None  #容错，程序降级到重新扫码






# ---------------------------------------------------------------------------
# Bot API：长轮询 + 发消息
# ---------------------------------------------------------------------------


#收消息
#cursor（get_updates_buf）：增量游标，告诉服务器上次读到哪，避免重复和遗漏。异常时返回原 cursor，游标不前进。
def get_updates(sess: dict, cursor: str, timeout_ms: int) -> dict:
    url = f"{sess['baseurl']}/ilink/bot/getupdates" #凭证里记的服务器地址
    body = {"get_updates_buf": cursor, "base_info": _base_info()}
    
    try:
        resp = requests.post(
            url,
            headers=_post_headers(sess["bot_token"]),
            json=body,
            timeout=timeout_ms / 1000 + 5, #多留5秒缓冲，防止服务器刚要返回，客户端先超时
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        return {"ret": 0, "msgs": [], "get_updates_buf": cursor}
    except requests.exceptions.RequestException as e:
        LOG.warning("getUpdates 网络错误：%s", e)
        return {"ret": 0, "msgs": [], "get_updates_buf": cursor}
    



#把回复打包发给腾讯
def send_text(sess: dict, to_user_id: str, text: str, context_token: str = "") -> dict:
    url = f"{sess['baseurl']}/ilink/bot/sendmessage"
    msg = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": secrets.token_hex(16),
        "message_type": 2,   # Bot发的
        "message_state": 2,  # 完成
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    body = {"msg": msg, "base_info": _base_info()}
    resp = requests.post(url, headers=_post_headers(sess["bot_token"]), json=body, timeout=20) #发消息立即返回，无需等待
    resp.raise_for_status()
    data = resp.json()
    if data.get("ret") not in (0, None):  #ret 是业务返回码，0 或 None 表示成功
        raise RuntimeError(f"sendMessage ret={data.get('ret')} errmsg={data.get('errmsg')}")
    return data
#区别：get_updates 的 ret 非 0 时，主循环有专门处理（errcode == -14 等），所以不在函数内抛。send_text 是“一次性操作”，失败就该抛，让调用方处理。


#发文件：与官方 openclaw-weixin 2.4.10 完全一致的三步协议
#   ① POST {base}/ilink/bot/getuploadurl 申请上传参数（filekey/aeskey/明文大小/md5）
#   ② 用 AES-128-ECB(PKCS#7) 加密文件，POST 密文到微信 CDN；
#      响应头 x-encrypted-param 就是下载参数（不是响应体里的 url）
#   ③ sendmessage 放一个 type=4 的 file_item，用 media.encrypt_query_param 引用 CDN
#   旧实现把文件 multipart POST 到 /ilink/bot/upload，该接口不存在，所以一直 404。
#   任何一步失败都抛异常，由 tools/media.py 捕获并降级成"文件卡片"，不会拖垮整轮对话。
CDN_BASE_URL = os.getenv("ILINK_CDN_BASE_URL", "https://novac2c.cdn.weixin.qq.com/c2c")


def _aes_ecb_padded_size(size: int) -> int:
    """PKCS#7 填充后的密文长度：ceil((n+1)/16)*16（与官方 aesEcbPaddedSize 一致）。"""
    return ((size + 1 + 15) // 16) * 16


def _encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB + PKCS#7，微信 CDN 要求的加密方式。"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as e:  # 缺依赖时给出可执行的提示，而不是让整轮对话崩掉
        raise RuntimeError("缺少 cryptography 依赖：.venv\\Scripts\\pip install cryptography") from e
    pad = 16 - len(plaintext) % 16
    padded = plaintext + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(padded) + enc.finalize()


def _cdn_upload_url(upload_param: str, filekey: str) -> str:
    """没有 upload_full_url 时，按官方格式拼 CDN 上传地址。"""
    return (f"{CDN_BASE_URL}/upload?encrypted_query_param={quote(upload_param, safe='')}"
            f"&filekey={quote(filekey, safe='')}")


def _build_file_item(name: str, size: int, encrypt_query_param: str, aes_key_b64: str,
                     item_type: int = 4, item_key: str = "file_item",
                     cipher_size: int = 0) -> dict:
    """构造媒体消息的一个 item（纯函数，方便离线测试）。

    type=4/file_item 用 file_name + len（明文大小）；图片/视频的 item 用 *_size（密文大小）。
    """
    item = {
        "media": {
            "encrypt_query_param": encrypt_query_param,
            "aes_key": aes_key_b64,
            "encrypt_type": 1,
        },
    }
    if item_type == 4:        # file_item
        item["file_name"] = name
        item["len"] = str(size)
    elif item_type == 2:      # image_item
        item["mid_size"] = cipher_size
    elif item_type == 5:      # video_item
        item["video_size"] = cipher_size
    return {"type": item_type, item_key: item}


def _get_upload_url(sess: dict, to_user_id: str, plaintext: bytes,
                    filekey: str, aeskey: bytes, media_type: int = 3) -> dict:
    """① 申请上传参数。media_type: 1=图片 2=视频 3=文件 4=语音。"""
    body = {
        "filekey": filekey,
        "media_type": media_type,
        "to_user_id": to_user_id,
        "rawsize": len(plaintext),
        "rawfilemd5": hashlib.md5(plaintext).hexdigest(),
        "filesize": _aes_ecb_padded_size(len(plaintext)),
        "no_need_thumb": True,   # 文件/视频不需要缩略图；图片走缩略图时才置 False
        "aeskey": aeskey.hex(),
        "base_info": _base_info(),
    }
    resp = requests.post(f"{sess['baseurl']}/ilink/bot/getuploadurl",
                         headers=_post_headers(sess["bot_token"]), json=body, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("ret") not in (0, None):
        raise RuntimeError(f"getUploadUrl ret={data.get('ret')} errmsg={data.get('errmsg')}")
    return data


def _upload_ciphertext(ciphertext: bytes, upload_url: str, attempts: int = 3) -> str:
    """② POST 密文到 CDN，返回下载参数；4xx 立即失败，5xx/网络错误重试。"""
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            resp = requests.post(upload_url,
                                 headers={"Content-Type": "application/octet-stream"},
                                 data=ciphertext, timeout=120)
        except requests.RequestException as e:
            last_err = e
        else:
            if resp.status_code == 200:
                param = resp.headers.get("x-encrypted-param")
                if not param:
                    raise RuntimeError("CDN 返回 200 但缺少 x-encrypted-param 响应头")
                LOG.info("CDN 上传成功：%d 字节密文", len(ciphertext))
                return param
            detail = resp.headers.get("x-error-message") or resp.text[:300]
            if 400 <= resp.status_code < 500:   # 参数过期/被拒，重试无意义
                raise RuntimeError(f"CDN 上传失败（HTTP {resp.status_code}）：{detail}")
            last_err = RuntimeError(f"CDN 上传失败（HTTP {resp.status_code}）：{detail}")
        time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"CDN 上传重试 {attempts} 次仍失败：{last_err}")


def send_file_message(sess: dict, to_user_id: str, local_path: str,
                      display_name: str = "", context_token: str = "") -> str:
    name = display_name or os.path.basename(local_path)
    with open(local_path, "rb") as f:
        plaintext = f.read()
    rawsize = len(plaintext)

    media_type = int(os.getenv("ILINK_MEDIA_TYPE", "3"))       # 3=文件
    item_type = int(os.getenv("ILINK_FILE_ITEM_TYPE", "4"))    # 4=file_item
    item_key = os.getenv("ILINK_FILE_ITEM_KEY", "file_item")

    filekey = secrets.token_hex(16)          # 32 个 hex 字符
    aeskey = secrets.token_bytes(16)         # AES-128 key

    # ① 申请上传参数
    up = _get_upload_url(sess, to_user_id, plaintext, filekey, aeskey, media_type=media_type)
    upload_url = (up.get("upload_full_url") or "").strip()
    if not upload_url and up.get("upload_param"):
        upload_url = _cdn_upload_url(up["upload_param"], filekey)
    if not upload_url:
        raise RuntimeError(f"getUploadUrl 没有返回 upload_param/upload_full_url：{str(up)[:300]}")

    # ② 加密后上传到 CDN
    ciphertext = _encrypt_aes_ecb(plaintext, aeskey)
    LOG.info("发送文件 %s：明文 %d 字节，密文 %d 字节", name, rawsize, len(ciphertext))
    download_param = _upload_ciphertext(ciphertext, upload_url)

    # ③ 发文件消息：aes_key 与官方一致，base64(hex 字符串)，不是 base64(原始 16 字节)
    aes_key_b64 = base64.b64encode(aeskey.hex().encode("ascii")).decode("ascii")
    item = _build_file_item(name, rawsize, download_param, aes_key_b64,
                            item_type=item_type, item_key=item_key,
                            cipher_size=len(ciphertext))
    msg = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": secrets.token_hex(16),
        "message_type": 2,
        "message_state": 2,
        "item_list": [item],
    }
    if context_token:
        msg["context_token"] = context_token
    resp = requests.post(f"{sess['baseurl']}/ilink/bot/sendmessage",
                         headers=_post_headers(sess["bot_token"]),
                         json={"msg": msg, "base_info": _base_info()}, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if data.get("ret") not in (0, None):
        raise RuntimeError(f"sendmessage(file) ret={data.get('ret')} errmsg={data.get('errmsg')}")
    return f"已发送文件 {name}（{rawsize / 1024:.1f} KB）"


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
#收到消息之后，怎么处理、怎么调 agent、怎么维护历史
# ---------------------------------------------------------------------------

#从消息里抠出文字
def _extract_text(msg: dict) -> str:
    for item in msg.get("item_list", []): #没有就返回空列表   
        if item.get("type") == 1 and item.get("text_item", {}).get("text"):
            return item["text_item"]["text"]
    return ""


def _dump_raw(msg: dict) -> None:
    """调试用：把收到的原始消息打印出来，方便观察群消息/字段结构。"""
    try:
        raw = json.dumps(msg, ensure_ascii=False)
    except Exception:
        raw = repr(msg)
    LOG.info(
        "原始消息 | type=%s from=%s to=%s has_ctx=%s | %s",
        msg.get("message_type"), msg.get("from_user_id"),
        msg.get("to_user_id"), bool(msg.get("context_token")), raw[:3000],
    )


#处理一条消息的完整流程：过滤 → 去重 → 调 agent → 更新历史 → 发回复
def _handle_msg(sess: dict, msg: dict, context_tokens: dict,
                histories: dict, seen_ids: set) -> None:
    if DEBUG_MSG:
        _dump_raw(msg)

    # 只处理用户发来的消息（message_type==1），忽略 bot 自己发的2
    if msg.get("message_type") != 1:
        return
    text = _extract_text(msg).strip()
    if not text:
        return
    from_user = msg.get("from_user_id", "")
    ctx = msg.get("context_token", "")
    if not from_user:
        return

    # 忽略自己发出的消息，避免自回复回环
    if from_user == sess.get("ilink_bot_id"):
        return

    # 幂等去重：同一条消息被重复投递（长轮询重试或者书签回退）时只处理一次
    mid = msg.get("msg_id") or msg.get("server_id") or msg.get("client_id")
    if mid:
        if mid in seen_ids:
            LOG.info("跳过重复消息 %s", mid)
            return
        seen_ids.add(mid)
        if len(seen_ids) > 2000:
            seen_ids.clear()

    LOG.info("收到消息来自 %s：%s", from_user, text)
    if ctx:
        context_tokens[from_user] = ctx

    #告诉待办工具"当前是谁"，保证每个人只读写自己的待办
    set_current_user(from_user)

    #审批流：如果上一条危险命令还在等确认，看这次回复是不是"同意/拒绝"
    # 审批流（文件）：上一条"发文件"还在等确认，看这次回复是不是同意/拒绝
    file_pending = media_tools.peek_send_pending()
    if file_pending:
        if _is_affirmative(text):
            path = media_tools.arm_send_file()
            LOG.info("用户已批准发送文件：%s", path)
            text = (f"用户已经明确同意发送这个文件，请立刻调用 send_file(path=\"{path}\") "
                    f"把它发出去，不要换成别的文件。")
        elif _is_negative(text):
            media_tools.deny_send_file()
            LOG.info("用户拒绝发送文件")
            text = "用户拒绝发送刚才那个文件，请不要再尝试，并说明已经取消。"
        else:
            media_tools.deny_send_file()
            LOG.info("用户没答复文件审批，已清除待确认")

    #审批流：如果上一条危险命令还在等确认，看这次回复是不是"同意/拒绝"
    pending = peek_pending_approval()
    if pending:
        if _is_affirmative(text):
            cmd = arm_pending_approval()
            LOG.info("用户已批准执行：%s", cmd)
            text = f"用户已经明确同意执行这条命令，请立刻执行：{cmd}"
        elif _is_negative(text):
            deny_pending_approval()
            LOG.info("用户拒绝执行待确认命令")
            text = "用户拒绝执行刚才那条有风险的操作，请不要再尝试，并说明已经取消。"
        else:
            # 用户说的是别的：当作放弃那条待确认命令，避免"以后某句话意外变成同意"
            deny_pending_approval()
            LOG.info("用户没答复审批，已清除待确认命令")

    #调 agent：先按 token 预算压缩历史（超预算把早期对话压成摘要），再带上历史 + 摘要
    state = histories.get(from_user) or {"messages": [], "summary": ""}
    try:
        msgs, summary = compact_history(state.get("messages", []), state.get("summary", ""))
        answer = run(text, msgs, summary)
    except Exception as e:
        LOG.exception("调用 agent 失败")
        answer = f"抱歉，出了点问题：{e}"
    else:
        # 把这一轮追加进历史；条数硬上限兜底（token 预算才是主要控制）
        msgs = msgs + [
            {"role": "user", "content": text},
            {"role": "assistant", "content": [{"type": "text", "text": answer}]},
        ]
        histories[from_user] = {"messages": msgs[-MAX_HISTORY:], "summary": summary}

    #如果这轮模型又提出了危险操作/要发文件，明确提示用户需要确认
    if peek_pending_approval():
        answer += "\n\n⚠️ 这条操作有风险，需要你确认：回复「同意」我就执行，回复其他内容视为取消。"
    elif media_tools.peek_send_pending():
        answer += "\n\n⚠️ 要把文件发出去，需要你确认：回复「同意」我就发，回复其他内容视为取消。"

    #发送前最后兜一次空回复：微信端发一条空白消息既没意义，也容易触发风控
    if not (answer or "").strip():
        LOG.warning("回答为空，改用兜底文案")
        answer = "（这条我没能给出有效回答，可以再问一次或说得更具体一点。）"

    send_text(sess, from_user, answer, ctx or context_tokens.get(from_user, ""))
    LOG.info("已回复 %s", from_user)






# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def _send_due_reminders(sess: dict) -> None:
    """检查到点的待办，主动发微信提醒（每条只提醒一次）。"""
    for t in due_reminders():
        try:
            send_text(sess, t["user_id"], f"⏰ 提醒：{t['text']}")
            mark_reminded(t["id"])
            LOG.info("已发送提醒：%s", t["text"])
        except Exception:
            LOG.exception("发送提醒失败")


def main() -> None:
    #配置日志输出格式和级别
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    #命令行带 --relogin 就强制重新扫码；否则先读本地凭证
    force_relogin = "--relogin" in sys.argv
    sess = None if force_relogin else load_session()

    #没有凭证（首次使用 / 凭证失效 / --relogin）就扫码登录，并把新凭证存下来
    if not sess:
        print("首次使用（或重新登录），需要扫码连接微信…")
        sess = login_via_qr()
        save_session(sess)
        print(f"\n✅ 登录成功！bot id: {sess['ilink_bot_id']}")
    else:
        print(f"✅ 已使用本地凭证，bot id: {sess.get('ilink_bot_id')}")

    #告诉腾讯"我上线了"（失败不影响收发）
    _notify(sess, "notifystart")
    print("\n===== 微信机器人已启动，开始接收消息（Ctrl+C 退出）=====\n")

    #初始化运行状态：书签、等待时长、每个用户的历史 / context_token、已处理消息 id
    cursor = ""
    timeout_ms = 35_000
    context_tokens: dict = {}  # user_id -> 最近一次 context_token
    histories: dict = {}       # user_id -> {"messages": [...], "summary": "..."}
    seen_ids: set = set()      # 已处理过的消息 id，用于去重

    #把"真正发文件"的能力注册给工具层（tools/media.py 不 import bot，避免循环依赖）
    def _file_transport(abs_path: str, display_name: str) -> str:
        # 默认发给"当前正在对话的用户"；如果配了 ILINK_SEND_TO（比如摸到了文件传输助手的 id），优先用它
        uid = os.getenv("ILINK_SEND_TO", "").strip() or get_current_user()
        return send_file_message(sess, uid, abs_path, display_name,
                                 context_tokens.get(uid, ""))

    media_tools.set_transport(_file_transport)

    try:
        #主循环：一直长轮询，收到消息就处理
        while True:
            #每轮先检查有没有到点的待办，有就主动提醒
            _send_due_reminders(sess)
            try:
                resp = get_updates(sess, cursor, timeout_ms)  #打一通电话等消息
            except KeyboardInterrupt:
                break

            if resp.get("ret") not in (0, None):
                errcode = resp.get("errcode")
                LOG.warning("getUpdates ret=%s errcode=%s errmsg=%s", resp.get("ret"), errcode, resp.get("errmsg"))
                # -14：账号会话被暂停，休息更久再试
                time.sleep(60 if errcode == -14 else 2)
                continue

            #更新书签（读到哪了），下次带着它去，避免重复/遗漏
            buf = resp.get("get_updates_buf")
            if buf:
                cursor = buf

            #服务端建议的等待时长，限制在 10~60 秒
            if resp.get("longpolling_timeout_ms"):
                timeout_ms = max(10_000, min(60_000, int(resp["longpolling_timeout_ms"])))

            #这一轮带回来的每条消息逐条处理；单条出错不影响其他消息
            for msg in resp.get("msgs", []):
                try:
                    _handle_msg(sess, msg, context_tokens, histories, seen_ids)
                except Exception:
                    LOG.exception("处理消息时出错")
    finally:
        #无论正常退出还是 Ctrl+C，都告诉腾讯"我下线了"
        _notify(sess, "notifystop")
        print("\n已停止。")


if __name__ == "__main__":
    main()
