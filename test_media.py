"""文件发送工具的测试：策略 / 审批 / 审计 / 降级（不碰微信、不联网）。

跑法：
    .venv\\Scripts\\python test_media.py
"""
import json
import os
import shutil
import sys

from tools import general, media

BASE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(BASE, "_tmp_media_test")
REL = "_tmp_media_test"

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [OK]   {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}   {detail}")


def audit_events():
    if not os.path.exists(general.AUDIT_FILE):
        return []
    with open(general.AUDIT_FILE, encoding="utf-8") as f:
        return [json.loads(x)["event"] for x in f.read().splitlines() if x.strip()]


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    general.reset_observed()

    # 审计写到临时目录，别污染真实 audit.log
    orig_audit = general.AUDIT_FILE
    orig_max = media.MAX_FILE_MB
    general.AUDIT_FILE = os.path.join(TMP, "audit.log")
    media._pending.clear()
    media._armed.clear()
    media.set_transport(None)

    ok_rel = f"{REL}/report.txt"
    with open(os.path.join(BASE, ok_rel), "w", encoding="utf-8") as f:
        f.write("hello report\n")
    with open(os.path.join(BASE, REL, ".env"), "w", encoding="utf-8") as f:
        f.write("SECRET=1\n")
    with open(os.path.join(BASE, REL, "id_rsa"), "w", encoding="utf-8") as f:
        f.write("PRIVATE KEY\n")
    with open(os.path.join(BASE, REL, "big.bin"), "wb") as f:
        f.write(b"x" * 4096)

    try:
        print("\n== 1. 永久拒绝：凭证类文件 ==")
        for bad, label in [(f"{REL}/.env", ".env"),
                           (f"{REL}/id_rsa", "id_rsa")]:
            r = media.send_file(bad)
            check(f"{label} 被永久拒绝", "永久禁止发送" in r, r[:80])
            check(f"{label} 不会进待确认", media.peek_send_pending() is None)

        print("\n== 2. 路径与参数检查 ==")
        r = media.send_file("../outside.txt")
        check("越出项目目录被拒", r.startswith("错误") and "超出工作区" in r, r[:80])
        r = media.send_file(f"{REL}/not_exist.txt")
        check("文件不存在有提示", "文件不存在" in r, r[:80])
        r = media.send_file("")
        check("空路径有提示", "路径为空" in r, r[:80])

        print("\n== 3. 大小上限 ==")
        media.MAX_FILE_MB = 0.001          # 临时改成 1KB
        r = media.send_file(f"{REL}/big.bin")
        check("超过上限被拒", "文件太大" in r, r[:80])
        media.MAX_FILE_MB = orig_max

        print("\n== 4. 审批流：先拦下、登记待确认 ==")
        r = media.send_file(ok_rel)
        check("第一次调用被拦下并要求确认", "数据外泄风险" in r, r[:80])
        pend = media.peek_send_pending()
        check("登记了待确认", pend is not None and pend.get("sig"), pend)
        check("待确认里记录了相对路径", pend and pend.get("rel", "").endswith("report.txt"), pend)

        print("\n== 5. 批准 → 只放行一次 ==")
        path = media.arm_send_file()
        check("批准后拿到路径", bool(path) and path.endswith("report.txt"), path)
        check("批准后待确认被清空", media.peek_send_pending() is None)
        r = media.send_file(ok_rel)
        check("批准后放行（走降级卡片）", "[文件]" in r and "sha256" in r, r[:120])
        r2 = media.send_file(ok_rel)
        check("同一次批准只能用一次", "数据外泄风险" in r2, r2[:80])
        media.deny_send_file()

        print("\n== 6. 拒绝与绕过 ==")
        media.send_file(ok_rel)
        check("再次登记待确认", media.peek_send_pending() is not None)
        media.deny_send_file()
        check("拒绝后清空", media.peek_send_pending() is None)
        # 批准 A 之后改发 B：指纹不同，必须重新审批
        media.send_file(ok_rel)
        media.arm_send_file()
        r = media.send_file(f"{REL}/big.bin")
        check("批准 A 不等于批准 B（指纹绑定）", "数据外泄风险" in r, r[:80])
        media.deny_send_file()

        print("\n== 7. 传输层：可插拔 + 失败降级 ==")
        calls = []

        def fake_ok(p, name):
            calls.append((p, name))
            return f"（假传输成功：{name}）"

        media.set_transport(fake_ok)
        media.send_file(ok_rel)
        media.arm_send_file()
        r = media.send_file(ok_rel)
        check("注册的传输函数被调用", bool(calls) and calls[-1][1] == "report.txt", calls)
        check("传输结果原样返回", "假传输成功" in r, r[:80])

        def fake_fail(p, name):
            raise RuntimeError("upload 返回 404")

        media.set_transport(fake_fail)
        media.send_file(ok_rel)
        media.arm_send_file()
        r = media.send_file(ok_rel, note="这是说明")
        check("传输失败降级为卡片", "[文件]" in r and "sha256" in r, r[:120])
        check("降级卡片里带上失败原因", "upload" in r, r[:160])
        check("note 被附上", "这是说明" in r, r[:160])
        media.set_transport(None)

        print("\n== 8. 审计 ==")
        events = audit_events()
        check("记录拦截", "send_file-blocked" in events, events[:10])
        check("记录待审批", "send_file-needs-approval" in events, events[:10])
        check("记录批准", "send_file-approved" in events, events[:10])
        check("记录实际发送", "send_file" in events, events[:10])
        check("记录降级", "send_file-fallback" in events, events[:10])

        print("\n== 9. 文件消息 item 的构造（离线，不联网）==")
        from bot import _build_file_item, _aes_ecb_padded_size, _cdn_upload_url, _encrypt_aes_ecb
        item = _build_file_item("a.txt", 12, "enc-param", "a2V5",
                                item_type=4, item_key="file_item")
        media_ref = item["file_item"]["media"]
        check("默认 type=4 + file_item", item["type"] == 4 and "file_item" in item, item)
        check("带上文件名/明文大小/len 字符串",
              item["file_item"]["file_name"] == "a.txt"
              and item["file_item"]["len"] == "12", item)
        check("media 带 CDN 参数/加密方式/aes_key",
              media_ref["encrypt_query_param"] == "enc-param"
              and media_ref["encrypt_type"] == 1
              and media_ref["aes_key"] == "a2V5", media_ref)
        item2 = _build_file_item("a.png", 300, "enc2", "a2V5",
                                 item_type=2, item_key="image_item", cipher_size=320)
        check("可切成图片形态（type=2 / image_item / mid_size）",
              item2["type"] == 2 and "image_item" in item2
              and item2["image_item"]["mid_size"] == 320, item2)
        check("PKCS#7 密文长度与官方一致",
              [_aes_ecb_padded_size(n) for n in (0, 15, 16, 1000)] == [16, 16, 32, 1008],
              [_aes_ecb_padded_size(n) for n in (0, 15, 16, 1000)])
        u = _cdn_upload_url("a+b/c==", "f" * 32)
        check("CDN 上传地址按官方格式编码",
              "/c2c/upload?encrypted_query_param=a%2Bb%2Fc%3D%3D&filekey=" in u
              and ("f" * 32) in u, u)
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        ct = _encrypt_aes_ecb(plain, key)
        check("AES-128-ECB 与 NIST 向量一致（含 PKCS#7 填充）",
              ct[:16].hex() == "69c4e0d86a7b0430d8cdb78070b4c55a" and len(ct) == 32,
              ct.hex())
    finally:
        general.AUDIT_FILE = orig_audit
        media.MAX_FILE_MB = orig_max
        media.set_transport(None)
        media._pending.clear()
        media._armed.clear()
        shutil.rmtree(TMP, ignore_errors=True)
        general.reset_observed()

    print("\n" + "=" * 46)
    print(f"通过 {_passed} 项，失败 {_failed} 项")
    print("=" * 46)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
