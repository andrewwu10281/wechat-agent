"""集中读取 .env 里的配置，其它文件从这里 import。"""
import os
from dotenv import load_dotenv

load_dotenv()  # 自动读取同目录下的 .env

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

QWEATHER_KEY = os.getenv("QWEATHER_KEY", "")      # 可选：和风天气
SCHOLAR_ID = os.getenv("SCHOLAR_ID", "")          # 可选：你自己的 Semantic Scholar 作者 ID
