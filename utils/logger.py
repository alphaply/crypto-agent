# logger.py
import logging
import sys
import os
from logging.handlers import RotatingFileHandler
import pytz
from datetime import datetime

# 设置时区
TZ_CN = pytz.timezone('Asia/Shanghai')

class LocalTimeFormatter(logging.Formatter):
    """重写 Formatter 以使用指定时区的时间"""
    def converter(self, timestamp):
        dt = datetime.fromtimestamp(timestamp, tz=TZ_CN)
        return dt.timetuple()

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=TZ_CN)
        if datefmt:
            s = dt.strftime(datefmt)
        else:
            s = dt.strftime("%Y-%m-%d %H:%M:%S")
        return s

def setup_logger(name, log_file='app.log', level=logging.INFO):
    """
    配置并返回一个 logger 实例
    """
    logger = logging.getLogger(name)
    
    if not logger.handlers:
        logger.setLevel(level)

        # 1. 定义格式: [时间] [级别] [模块] 消息
        log_format = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
        formatter = LocalTimeFormatter(log_format)

        # 2. 控制台输出 (StreamHandler)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # 3. 文件输出 (RotatingFileHandler) - 每个文件最大 10MB，保留 5 个备份
        file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8', delay=True)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger