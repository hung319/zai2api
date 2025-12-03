#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
Z.ai 2 API (Refactored by CezDev)
- Standard: OpenAI Only (Removed Anthropic).
- Fix: Stream buffering issue.
- Feature: Dynamic Random IP Spoofing per Request.
"""

from gevent import monkey
monkey.patch_all()

import os, re, json, base64, urllib.parse, requests, hashlib, hmac, uuid, traceback, logging, random
from datetime import datetime
from flask import Flask, request, Response, jsonify, make_response
from typing import Any, Dict, List, Union, Optional

from dotenv import load_dotenv
load_dotenv()

# --- Helper tạo IP ngẫu nhiên ---
def generate_random_ip():
    return f"{random.randint(1, 255)}.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(0, 255)}"

# Cấu hình
class cfg:
    class source:
        protocol = str(os.getenv("PROTOCOL", "https:"))
        host = str(os.getenv("BASE", "chat.z.ai"))
        token = str(os.getenv("TOKEN", "")).strip()
    class api:
        port = int(os.getenv("PORT", "8080"))
        debug = str(os.getenv("DEBUG", "false")).lower() == "true"
        debug_msg = str(os.getenv("DEBUG_MSG", "false")).lower() == "true"
        think = str(os.getenv("THINK_TAGS_MODE", "reasoning")) # 'reasoning' or 'ignore'
        anon = str(os.getenv("ANONYMOUS_MODE", "true")).lower() == "true"
        key = str(os.getenv("API_KEY", "")).strip() 

    class network:
        proxy_url = str(os.getenv("PROXY_URL", "")).strip()
        proxies = {
            "http": proxy_url,
            "https": proxy_url
        } if proxy_url else None

    class model:
        default = str(os.getenv("MODEL", "glm-4.6"))
        mapping = {}

    # Headers cơ bản (chưa có IP)
    base_headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Microsoft Edge";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
        "X-FE-Version": "prod-fe-1.0.111",
    }

# Update Origin/Referer dynamic
cfg.base_headers["Origin"] = f"{cfg.source.protocol}//{cfg.source.host}"
cfg.base_headers["Referer"] = f"{cfg.source.protocol}//{cfg.source.host}/"

# tiktoken setup
cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tiktoken') + os.sep
os.environ["TIKTOKEN_CACHE_DIR"] = cache_dir
try:
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
except:
    class MockEnc:
        def encode(self, text): return [0] * len(text)
    enc = MockEnc()

# Logger
logging.basicConfig(
    level=logging.DEBUG if cfg.api.debug_msg else logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

# Flask App
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# Auth Check
def check_auth():
    if not cfg.api.key:
        return None 
    
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        return make_response(jsonify({"error": "Unauthorized", "message": "Missing Authorization Header"}), 401)
    
    token = auth_header
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
    
    if token != cfg.api.key:
        return make_response(jsonify({"error": "Unauthorized", "message": "Invalid API Key"}), 401)
    return None

class utils:
    @staticmethod
    class request:
        @staticmethod
        def get_headers_with_ip():
            """Sinh headers mới kèm IP ngẫu nhiên cho mỗi request"""
            current_ip = generate_random_ip()
            return {
                **cfg.base_headers,
                "X-Forwarded-For": current_ip,
                "X-Real-IP": current_ip
            }, current_ip

        @staticmethod
        def chat(data, chat_id):
            timestamp = int(datetime.now().timestamp() * 1000)
            requestId = str(uuid.uuid4())

            user = utils.request.user()
            userToken = user.get("token")
            userId = user.get("id")

            params = {
                "timestamp": timestamp,
                "requestId": requestId,
            }
            
            headers, current_ip = utils.request.get_headers_with_ip()
            headers.update({
                "Authorization": f"Bearer {userToken}",
                "Content-Type": "application/json",
                "Referer": f"{cfg.source.protocol}//{cfg.source.host}/c/{chat_id}",
            })

            if userId:
                params["user_id"] = userId
                last_user_message = ""
                for message in data.get("messages", []):
                    if message.get("role") and message.get("content"):
                        content = message.get("content")
                        if isinstance(content, str):
                            last_user_message = content
                        if isinstance(content, list):
                            texts = []
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    texts.append(item.get("text", ""))
                                    break
                            last_user_message = "".join(texts)

                signatures = utils.request.signature({
                    "requestId": requestId,
                    "timestamp": timestamp,
                    "user_id": userId,
                }, last_user_message)
                headers["X-Signature"] = signatures.get("signature")
                params["signature_timestamp"] = signatures.get("timestamp")
                data["signature_prompt"] = last_user_message

            log.debug("Sending Chat Request with IP: %s", current_ip)
            
            url = f"{cfg.source.protocol}//{cfg.source.host}/api/chat/completions"
            if params:
                query_string = urllib.parse.urlencode(params)
                url = f"{url}?{query_string}"

            return requests.post(url, json=data, headers=headers, stream=True, proxies=cfg.network.proxies)

        @staticmethod
        def image(data_url, chat_id):
            if cfg.api.anon or not data_url.startswith("data:"):
                return None

            header, encoded = data_url.split(",", 1)
            mime_type = header.split(";")[0].split(":")[1] if ":" in header else "image/jpeg"

            image_data = base64.b64decode(encoded)
            filename = str(uuid.uuid4())

            body = {
                "file": (filename, image_data, mime_type)
            }
            
            headers, current_ip = utils.request.get_headers_with_ip()
            headers.update({
                 "Authorization": f"Bearer {utils.request.user().get('token')}",
                 "Referer": f"{cfg.source.protocol}//{cfg.source.host}/c/{chat_id}"
            })

            log.debug("Uploading Image with IP: %s", current_ip)
            response = requests.post(f"{cfg.source.protocol}//{cfg.source.host}/api/v1/files/", files=body, headers=headers, proxies=cfg.network.proxies)

            if response.status_code == 200:
                result = response.json()
                return f"{result.get('id')}_{result.get('filename')}"
            else:
                raise Exception(f"image upload fail: {response.text}")

        @staticmethod
        def id(prefix = "msg") -> str:
            return f"{str(uuid.uuid4())}"
        
        @staticmethod
        def cookies():
            if cfg.base_headers.get("Cookie"):
                return cfg.base_headers["Cookie"]

            url = f"{cfg.source.protocol}//{cfg.source.host}"
            headers, current_ip = utils.request.get_headers_with_ip()
            
            log.debug("Fetching Cookies with IP: %s", current_ip)
            response = requests.get(url, headers=headers, proxies=cfg.network.proxies)

            if response.status_code in (200, 301, 302, 401, 403):
                set_cookie_headers = response.headers.get_all('Set-Cookie') if hasattr(response.headers, 'get_all') else response.headers.get('Set-Cookie')
                if set_cookie_headers:
                    if isinstance(set_cookie_headers, str):
                        set_cookie_headers = [set_cookie_headers]
                    cookies = []
                    for sc in set_cookie_headers:
                        cookie_part = sc.split(';')[0].strip()
                        if '=' in cookie_part:
                            cookies.append(cookie_part)

                    if cookies:
                        cfg.base_headers["Cookie"] = "; ".join(cookies)
                        return cfg.base_headers["Cookie"]
            else:
                raise Exception(f"fetch cookie fail: {response.status_code}")

        _user_cache = {}
        @staticmethod
        def user():
            current_token = None if cfg.api.anon else cfg.source.token

            if current_token and current_token in utils.request._user_cache:
                return {"id": utils.request._user_cache[current_token].get("id"), "token": current_token}

            headers, current_ip = utils.request.get_headers_with_ip()
            headers["Content-Type"] = "application/json"
            
            if not cfg.api.anon:
                headers["Authorization"] = f"Bearer {cfg.source.token}"
            
            log.debug("Fetching User Info with IP: %s", current_ip)

            response = requests.get(f"{cfg.source.protocol}//{cfg.source.host}/api/v1/auths/", headers=headers, proxies=cfg.network.proxies)
            if response.status_code == 200:
                data = response.json()
                userId = data.get("id")
                userToken = data.get("token") if cfg.api.anon else cfg.source.token

                if userToken and userId:
                    utils.request._user_cache[userToken] = {
                        "id": userId,
                        "name": data.get("name")
                    }
                return {"id": userId, "token": userToken}
            else:
                raise Exception(f"fetch user info fail: {response.text}")

        @staticmethod
        def signature(prarms: Dict, content: str) -> Dict:
            for param in ["timestamp", "requestId", "user_id"]:
                if param not in prarms or not prarms.get(param):
                    raise ValueError(f"need prarm: {param}")

            def _hmac_sha256(key: bytes, msg: bytes):
                return hmac.new(key, msg, hashlib.sha256).hexdigest()

            request_time = int(prarms.get("timestamp", datetime.now().timestamp() * 1000))
            signature_expire = request_time // (5 * 60 * 1000)
            signature_1_plaintext = str(signature_expire)
            signature_1 = _hmac_sha256(b"key-@@@@)))()((9))-xxxx&&&%%%%%", signature_1_plaintext.encode('utf-8'))

            content = base64.b64encode(content.encode('utf-8')).decode('ascii')
            signature_prarms = str(','.join([f"{k},{prarms[k]}" for k in sorted(prarms.keys())]))
            signature_2_plaintext = f"{signature_prarms}|{content}|{str(request_time)}"
            signature_2 = _hmac_sha256(signature_1.encode('utf-8'), signature_2_plaintext.encode('utf-8'))

            return {
                "signature": signature_2,
                "timestamp": request_time
            }

        _models_cache = {}
        @staticmethod
        def models() -> Dict:
            if utils.request._models_cache:
                return utils.request._models_cache

            current_token = utils.request.user().get('token') if cfg.api.anon else cfg.source.token
            
            headers, current_ip = utils.request.get_headers_with_ip()
            headers.update({
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json"
            })
            
            log.debug("Fetching Models with IP: %s", current_ip)
            
            response = requests.get(f"{cfg.source.protocol}//{cfg.source.host}/api/models", headers=headers, proxies=cfg.network.proxies)
            
            if response.status_code == 200:
                data = response.json()
                models_list = []
                for m in data.get("data", []):
                    if not m.get("info", {}).get("is_active", True): continue
                    model_id = m.get("id")
                    model_name = m.get("name")
                    models_list.append({
                        "id": model_id,
                        "object": "model",
                        "name": model_name
                    })
                result = {"object": "list", "data": models_list}
                utils.request._models_cache = result
                return result 
            else:
                return {"object": "list", "data": [{"id": cfg.model.default, "object": "model"}]}


        @staticmethod
        def response(resp):
            resp.headers.update({
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            })
            return resp

        @staticmethod
        def format(data: Dict):
            # Only keeping necessary OpenAI format logic
            odata = {**data.copy()}
            return odata

    class response:
        @staticmethod
        def parse(stream):
            # [FIX] Stream Fix: disable buffering and handle exceptions
            try:
                for line in stream.iter_lines(chunk_size=None): 
                    if not line: continue
                    if not line.startswith(b"data: "): 
                        continue
                        
                    try: 
                        decoded_line = line[6:].decode("utf-8", "ignore")
                        data = json.loads(decoded_line)
                        yield data
                    except json.JSONDecodeError:
                        continue 
                    except Exception as e:
                        log.debug(f"Parse line error: {e}")
                        continue
            except Exception as e:
                log.error(f"Stream connection broken: {e}")

        @staticmethod
        def format(data):
            # [CLEANUP] Only OpenAI formatting logic
            data = data.get("data", "")
            if not data: return None
            phase = data.get("phase", "other")
            content = data.get("delta_content") or data.get("edit_content") or ""
            if not content: return None
            
            if phase == "thinking" or (phase == "answer" and "summary>" in content):
                 content = re.sub(r"(?s)<details[^>]*?>.*?</details>", "", content)
                 content = content.replace("</thinking>", "").replace("<Full>", "").replace("</Full>", "")

            if phase == "tool_call": return {"tool_call": content}
            
            # OpenAI Standard Return
            if phase == "thinking" and cfg.api.think == "reasoning":
                 return {"role": "assistant", "reasoning_content": content}
            return {"role": "assistant", "content": content}

        @staticmethod
        def count(text):
            return len(enc.encode(text))

# --- ROUTES ---

@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    current_fake_ip = generate_random_ip()
    return utils.request.response(jsonify({
        "status": "ok",
        "generated_ip_sample": current_fake_ip, 
        "mode": "OpenAI Only",
        "timestamp": int(datetime.now().timestamp() * 1000)
    }))

@app.route("/v1/models", methods=["GET", "POST", "OPTIONS"])
def models():
    if request.method == "OPTIONS": return utils.request.response(make_response())
    auth_error = check_auth()
    if auth_error: return utils.request.response(auth_error)

    try:
        data = utils.request.models() 
        return utils.request.response(jsonify(data))
    except Exception as e:
        log.error(traceback.format_exc())
        return utils.request.response(jsonify({"error": 500, "message": str(e)})), 500

@app.route("/v1/chat/completions", methods=["GET", "POST", "OPTIONS"])
def OpenAI_Compatible():
    try:
        if request.method == "OPTIONS": return utils.request.response(make_response())
        auth_error = check_auth()
        if auth_error: return utils.request.response(auth_error)

        odata = request.get_json(force=True, silent=True) or {}
        id = utils.request.id("chat")
        stream = odata.get("stream", False)
        
        data = odata 
        data["chat_id"] = id
        
        response = utils.request.chat(data, id)
        
        if response.status_code != 200:
             return utils.request.response(jsonify({"error": response.text})), response.status_code

        if stream:
            def generate_stream():
                for raw_chunk in utils.response.parse(response):
                    delta = utils.response.format(raw_chunk)
                    if delta:
                        yield f"data: {json.dumps({
                            'id': id,
                            'object': 'chat.completion.chunk',
                            'created': int(datetime.now().timestamp()),
                            'model': data.get('model'),
                            'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]
                        }, ensure_ascii=False)}\n\n"
                
                yield f"data: {json.dumps({
                    'id': id, 
                    'object': 'chat.completion.chunk',
                    'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]
                })}\n\n"
                yield "data: [DONE]\n\n"

            return Response(generate_stream(), mimetype="text/event-stream")
        else:
            content = []
            for raw_chunk in utils.response.parse(response):
                delta = utils.response.format(raw_chunk)
                if delta and "content" in delta: content.append(delta["content"])
            
            return utils.request.response(jsonify({
                "id": id,
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(content)}, "finish_reason": "stop"}]
            }))

    except Exception as e:
        log.error(traceback.format_exc())
        return utils.request.response(jsonify({"error": 500, "message": str(e)})), 500

if __name__ == "__main__":
    log.info(f"Services Started on {cfg.api.port} (OpenAI Only Mode)")
    if cfg.api.debug:
        app.run(host="0.0.0.0", port=cfg.api.port, threaded=True, debug=True)
    else:
        from gevent import pywsgi
        pywsgi.WSGIServer(('0.0.0.0', cfg.api.port), app).serve_forever()
