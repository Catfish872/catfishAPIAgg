"""Application entrypoint.

The large application body lives in app_core.py. This file keeps startup wiring thin
and attaches compatibility endpoints that wrap the existing proxy implementation.
"""

import uvicorn

import app_core
from chat_image_content_compat import install_chat_image_content_compat
from responses_adapter import register_responses_endpoint


app = app_core.app

install_chat_image_content_compat(app_core)

register_responses_endpoint(
    app,
    app_core.proxy_chat_completions,
    app_core.verify_key,
    log_message=app_core.log_message,
)

PROJECT_NAME = app_core.PROJECT_NAME
PORT = app_core.PORT


if __name__ == "__main__":
    if not app_core.ADMIN_KEY:
        print("=" * 50)
        print("!!! 启动警告: 环境变量 'ADMIN_KEY' 未设置 !!!")
        print("!!! 请在启动前设置: export ADMIN_KEY='your_secret_key' !!!")
        print("!!! 为方便测试，将使用 'admin' 作为临时密钥 !!!")
        print("=" * 50)
        app_core.ADMIN_KEY = "admin"

    print(f"--- 正在以开发模式启动 {PROJECT_NAME} ---")
    print(f"--- 管理密钥 (ADMIN_KEY): {app_core.ADMIN_KEY} ---")
    print(f"--- 访问 http://0.0.0.0:{PORT} ---")

    uvicorn.run(app, host="0.0.0.0", port=PORT)
