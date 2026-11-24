"""文件发送的端到端探测器：验证微信 ilink 的真实"发文件"链路。

协议来自官方 openclaw-weixin 2.4.10（不是猜的）：
    ① POST {baseurl}/ilink/bot/getuploadurl，申请 filekey / aeskey / 上传地址
    ② 用 AES-128-ECB(PKCS#7) 加密文件，POST 密文到微信 CDN；
       响应头 x-encrypted-param 的值就是下载参数（不是响应体里的 url）
    ③ sendmessage 发 type=4 的 file_item，media.encrypt_query_param 引用 CDN 参数

用法：
    1) 只看计划（不联网）：
       .venv\\Scripts\\python probe_send_file.py
    2) 真的发一次（请确认凭证有效；文件会出现在你自己的微信里）：
       .venv\\Scripts\\python probe_send_file.py --send README.md
    3) 换媒体类型（默认 3=文件；图片=1、视频=2）：
       .venv\\Scripts\\python probe_send_file.py --send a.png --media-type 1

⚠️ 低频试错：一次就够，不要循环跑。多跑可能触发风控。
"""
import os
import sys

from bot import CDN_BASE_URL, _aes_ecb_padded_size, load_session, send_file_message


def main() -> int:
    sess = load_session()
    if not sess:
        print("没有本地凭证（.wcbot_session.json），先跑一次 bot.py 扫码登录。")
        return 1

    def _opt(name, default):
        if name in sys.argv:
            i = sys.argv.index(name)
            if i + 1 < len(sys.argv):
                return sys.argv[i + 1]
        return default

    media_type = int(_opt("--media-type", os.getenv("ILINK_MEDIA_TYPE", "3")))
    item_type = int(_opt("--type", os.getenv("ILINK_FILE_ITEM_TYPE", "4")))
    item_key = _opt("--key", os.getenv("ILINK_FILE_ITEM_KEY", "file_item"))
    os.environ["ILINK_MEDIA_TYPE"] = str(media_type)
    os.environ["ILINK_FILE_ITEM_TYPE"] = str(item_type)
    os.environ["ILINK_FILE_ITEM_KEY"] = item_key

    if "--send" not in sys.argv:
        print("当前是【只读模式】，不会发任何请求。")
        print(f"  baseurl        : {sess['baseurl']}")
        print(f"  bot id         : {sess.get('ilink_bot_id')}")
        print(f"  to_user_id     : {sess.get('ilink_user_id')}  （先发给授权用户本人）")
        print(f"  CDN            : {CDN_BASE_URL}")
        print(f"  media_type     : {media_type}    （1=图片 2=视频 3=文件）")
        print(f"  item           : type={item_type} key={item_key}")
        print(f"  密文长度示例   : 1000 字节明文 -> {_aes_ecb_padded_size(1000)} 字节密文")
        print("\n要真发一次：python probe_send_file.py --send <文件相对路径>")
        return 0

    idx = sys.argv.index("--send")
    if idx + 1 >= len(sys.argv):
        print("用法：python probe_send_file.py --send <文件相对路径>")
        return 1
    rel = sys.argv[idx + 1]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)
    if not os.path.isfile(path):
        print(f"文件不存在：{path}")
        return 1

    print(f"尝试发送：{rel}（{os.path.getsize(path)} 字节）")
    try:
        result = send_file_message(sess, sess.get("ilink_user_id", ""), path,
                                   os.path.basename(path))
        print("成功：", result)
        print("\n如果手机上真的收到了文件，说明 getuploadurl + CDN + file_item 三步都对了。")
        return 0
    except Exception as e:
        print("失败：", e)
        print("\n排查顺序：")
        print("  1) getuploadurl 是否返回 upload_param/upload_full_url？")
        print("  2) CDN 返回的 HTTP 状态/响应头 x-error-message 是什么？4xx 说明参数过期或被拒")
        print("  3) sendmessage 的 ret 是多少？非 0 说明 file_item 字段不对")
        return 1


if __name__ == "__main__":
    sys.exit(main())
