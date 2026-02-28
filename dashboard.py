import os
import threading
from flask import Flask
from routes.utils import logger, get_scheduler_status
from main_scheduler import run_smart_scheduler

# 导入蓝图
from routes.main import main_bp
from routes.auth import auth_bp
from routes.config import config_bp
from routes.stats import stats_bp
from routes.chat import chat_bp

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.getenv("ADMIN_PASSWORD", "dev-secret"))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# 注册蓝图
app.register_blueprint(main_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(config_bp)
app.register_blueprint(stats_bp)
app.register_blueprint(chat_bp)

if __name__ == '__main__':
    # 启动后台调度器
    if get_scheduler_status():
        scheduler_thread = threading.Thread(target=run_smart_scheduler, daemon=True)
        scheduler_thread.start()
        logger.info("✅ 后台智能调度器已启动")
    else:
        logger.info("⚠️ 调度器已在配置中禁用，仅运行 Web 服务")

    # 运行 Flask
    port = int(os.getenv("PORT", 7860))
    app.run(host='0.0.0.0', port=port, debug=False)
