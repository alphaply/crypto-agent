import time
import random
import io
import base64
from flask import Blueprint, request, jsonify, session
from PIL import Image, ImageDraw, ImageFont
from routes.utils import logger, _chat_password

auth_bp = Blueprint('auth', __name__)

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = 900 

def generate_captcha_text(length=4):
    chars = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
    return ''.join(random.choices(chars, k=length))

@auth_bp.route('/api/chat/captcha', methods=['GET'])
def get_captcha():
    text = generate_captcha_text()
    session['captcha_answer'] = text.upper()
    
    width, height = 120, 40
    img = Image.new('RGB', (width, height), color=(245, 247, 250))
    draw = ImageDraw.Draw(img)
    
    for _ in range(5):
        draw.line([(random.randint(0, width), random.randint(0, height)), 
                   (random.randint(0, width), random.randint(0, height))], 
                  fill=(random.randint(150, 200), random.randint(150, 200), random.randint(150, 200)), width=1)
    
    try:
        font = ImageFont.load_default(size=24)
    except:
        font = ImageFont.load_default()
        
    for i, char in enumerate(text):
        draw.text((10 + i*25, 5 + random.randint(-5, 5)), char, font=font, 
                  fill=(random.randint(20, 100), random.randint(20, 100), random.randint(20, 100)))
    
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    img_b64 = base64.b64encode(buf.getvalue()).decode()
    
    return jsonify({
        "success": True, 
        "image": f"data:image/png;base64,{img_b64}",
        "id": random.randint(1000, 9999)
    })

@auth_bp.route('/api/chat/auth', methods=['POST'])
def chat_auth():
    now = time.time()
    lock_until = session.get('lock_until', 0)
    if now < lock_until:
        remain = int(lock_until - now)
        return jsonify({"success": False, "message": f"尝试次数过多，请在 {remain} 秒后再试"}), 429

    data = request.json or {}
    password = data.get("password", "")
    captcha = (data.get("captcha", "")).upper()
    
    expected_captcha = session.get('captcha_answer')
    if not expected_captcha:
        return jsonify({"success": False, "message": "验证码已过期，请刷新"}), 400
    
    if captcha != expected_captcha:
        session.pop('captcha_answer', None)
        return jsonify({"success": False, "message": "验证码错误"}), 403
    
    session.pop('captcha_answer', None)

    expected = _chat_password()
    if not expected:
        return jsonify({"success": False, "message": "服务端未配置密码"}), 500
        
    if password != expected:
        fails = session.get('failed_attempts', 0) + 1
        session['failed_attempts'] = fails
        if fails >= MAX_FAILED_ATTEMPTS:
            session['lock_until'] = now + LOCKOUT_DURATION
            session['failed_attempts'] = 0
            return jsonify({"success": False, "message": "错误次数过多，账号已锁定 15 分钟"}), 429
        time.sleep(fails * 0.5) 
        return jsonify({"success": False, "message": f"密码错误 (剩余 {MAX_FAILED_ATTEMPTS - fails} 次尝试)"}), 401
    
    session["chat_authed"] = True
    session['failed_attempts'] = 0
    session.pop('lock_until', None)
    return jsonify({"success": True})
