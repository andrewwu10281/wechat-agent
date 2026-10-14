"""验证 wcferry 能否成功注入已安装的微信版本（不登录，只测注入）。"""
import time

from wcferry import Wcf

print("正在初始化 wcferry（启动 wcf.exe 并注入微信）...")
wcf = Wcf(block=False)  # block=False：不等待登录，注入成功即返回
print("✅ wcf 初始化成功 —— 注入成功，说明微信版本匹配（3.9.12.51）")

time.sleep(3)
try:
    qr = wcf.get_qrcode()
    print("二维码字符串长度:", len(qr) if qr else 0)
    print("当前是否已登录:", wcf.is_login())
except Exception as e:
    print("获取二维码出错（可能已登录或无二维码）:", e)

wcf.cleanup()
print("测试结束，已回收资源")
