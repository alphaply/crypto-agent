import multiprocessing
import subprocess
import sys
import time

def run_scheduler():
    """启动策略调度服务"""
    print("--- [System] Starting Main Scheduler... ---")
    # 使用 sys.executable 确保使用当前环境的 Python 解释器
    subprocess.run([sys.executable, "main_scheduler.py"])

def run_dashboard():
    """启动 Gradio 监控面板"""
    print("--- [System] Starting Gradio Dashboard... ---")
    subprocess.run([sys.executable, "dashboard.py"])

if __name__ == "__main__":
    # 创建两个进程
    p1 = multiprocessing.Process(target=run_scheduler)
    p2 = multiprocessing.Process(target=run_dashboard)

    try:
        p1.start()
        p2.start()
        
        # 保持主进程运行
        while True:
            time.sleep(1)
            if not p1.is_alive() or not p2.is_alive():
                print("⚠️ One of the services has stopped. Shutting down...")
                p1.terminate()
                p2.terminate()
                break
                
    except KeyboardInterrupt:
        print("\n--- [System] Stopping all services... ---")
        p1.terminate()
        p2.terminate()