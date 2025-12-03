#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
Z.ai 2 API (Refactored by CezDev)
- Standard: OpenAI Only (Removed Anthropic logic).
- Fix: Restored Z.ai specific request formatting (Signatures, Image Upload).
- Fix: Stream buffering issue & Dynamic Random IP.
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

# Config
class cfg:
    class source:
        protocol = str(os.getenv("PROTOCOL", "https:"))
        host = str(os.getenv("BASE", "chat.z.ai"))
        token = str(os.getenv("TOKEN", "")).strip()
    class api:
        port = int(os.getenv("PORT", "8080"))
        debug = str(os.getenv("DEBUG", "false")).lower() == "true"
        debug_msg = str(os.getenv("DEBUG_MSG", "false")).lower() == "true"
        think = str(os.getenv("THINK_TAGS_MODE", "reasoning"))
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

    headers = {
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

cfg.headers["Origin"] = f"{cfg.source.protocol}//{cfg.source.host}"
cfg.headers["Referer"] = f"{cfg.source.protocol}//{cfg.source.host}/"

# tiktoken
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

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# Auth
def check_auth():
    if not cfg.api.key: return None
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        return make_response(jsonify({"error": "Unauthorized", "message": "Missing Authorization Header"}), 401)
    token = auth_header.split(" ")[1] if auth_header.startswith("Bearer ") else auth_header
    if token != cfg.api.key:
        return make_response(jsonify({"error": "Unauthorized", "message": "Invalid API Key"}), 401)
    return None

phaseBak = "thinking"

class utils:
    @staticmethod
    class request:
        @staticmethod
        def get_headers_with_ip():
            """Sinh header kèm IP ngẫu nhiên"""
            current_ip = generate_random_ip()
            return {
                **cfg.headers,
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
                "Referer": f"{cfg.source.protocol}//{cfg.source.host}/c/{chat_id}"
            })

            if userId:
                params["user_id"] = userId
                last_user_message = ""
                # Logic lấy message cuối cùng để ký signature (bắt buộc phải có)
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

            body = {"file": (filename, image_data, mime_type)}
            
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
            if cfg.headers.get("Cookie"): return cfg.headers["Cookie"]
            url = f"{cfg.source.protocol}//{cfg.source.host}"
            headers, _ = utils.request.get_headers_with_ip()
            response = requests.get(url, headers=headers, proxies=cfg.network.proxies)
            if response.status_code in (200, 301, 302, 401, 403):
                set_cookie_headers = response.headers.get_all('Set-Cookie') if hasattr(response.headers, 'get_all') else response.headers.get('Set-Cookie')
                if set_cookie_headers:
                    if isinstance(set_cookie_headers, str): set_cookie_headers = [set_cookie_headers]
                    cookies = [sc.split(';')[0].strip() for sc in set_cookie_headers if '=' in sc.split(';')[0].strip()]
                    if cookies:
                        cfg.headers["Cookie"] = "; ".join(cookies)
                        return cfg.headers["Cookie"]
            else:
                raise Exception(f"fetch cookie fail: {response.status_code}")

        _user_cache = {}
        @staticmethod
        def user():
            current_token = None if cfg.api.anon else cfg.source.token
            if current_token and current_token in utils.request._user_cache:
                return {"id": utils.request._user_cache[current_token].get("id"), "token": current_token}

            headers, _ = utils.request.get_headers_with_ip()
            headers["Content-Type"] = "application/json"
            if not cfg.api.anon: headers["Authorization"] = f"Bearer {cfg.source.token}"
            
            response = requests.get(f"{cfg.source.protocol}//{cfg.source.host}/api/v1/auths/", headers=headers, proxies=cfg.network.proxies)
            if response.status_code == 200:
                data = response.json()
                userId = data.get("id")
                userToken = data.get("token") if cfg.api.anon else cfg.source.token
                if userToken and userId:
                    utils.request._user_cache[userToken] = {"id": userId, "name": data.get("name")}
                return {"id": userId, "token": userToken}
            else:
                raise Exception(f"fetch user info fail: {response.text}")

        @staticmethod
        def signature(prarms: Dict, content: str) -> Dict:
            for param in ["timestamp", "requestId", "user_id"]:
                if param not in prarms or not prarms.get(param): raise ValueError(f"need prarm: {param}")
            def _hmac_sha256(key: bytes, msg: bytes): return hmac.new(key, msg, hashlib.sha256).hexdigest()

            request_time = int(prarms.get("timestamp", datetime.now().timestamp() * 1000))
            signature_expire = request_time // (5 * 60 * 1000)
            signature_1 = _hmac_sha256(b"key-@@@@)))()((9))-xxxx&&&%%%%%", str(signature_expire).encode('utf-8'))

            content = base64.b64encode(content.encode('utf-8')).decode('ascii')
            signature_prarms = str(','.join([f"{k},{prarms[k]}" for k in sorted(prarms.keys())]))
            signature_2_plaintext = f"{signature_prarms}|{content}|{str(request_time)}"
            signature_2 = _hmac_sha256(signature_1.encode('utf-8'), signature_2_plaintext.encode('utf-8'))

            return {"signature": signature_2, "timestamp": request_time}

        _models_cache = {}
        @staticmethod
        def models() -> Dict:
            if utils.request._models_cache: return utils.request._models_cache
            current_token = utils.request.user().get('token') if cfg.api.anon else cfg.source.token
            headers, _ = utils.request.get_headers_with_ip()
            headers.update({"Authorization": f"Bearer {current_token}", "Content-Type": "application/json"})
            
            response = requests.get(f"{cfg.source.protocol}//{cfg.source.host}/api/models", headers=headers, proxies=cfg.network.proxies)
            if response.status_code == 200:
                data = response.json()
                models = []
                # Simple Model Mapping Logic to keep file small but functional
                for m in data.get("data", []):
                    if not m.get("info", {}).get("is_active", True): continue
                    model_id = m.get("id")
                    # Smart Name Logic
                    name = m.get("name", model_id)
                    models.append({
                        "id": model_id, "object": "model", "name": name,
                        "owned_by": "z.ai", 
                        "orignal": {"id": model_id, "info": m.get("info", {})}
                    })
                result = {"object": "list", "data": models}
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

        # [RESTORED] Logic format request quan trọng từ file gốc
        @staticmethod
        def format(data: Dict):
            odata = {**data.copy()}
            new_messages = []
            chat_id = odata.get("chat_id")
            model = odata.get("model", cfg.model.default)

            # Map model name
            if hasattr(cfg.model, 'mapping') and model:
                for source_id, mapped_id in cfg.model.mapping.items():
                    if mapped_id == model and model != source_id:
                        model = source_id
                        break

            # Convert System Prompt to Message
            if "system" in odata:
                systems = odata["system"]
                content = systems if isinstance(systems, str) else "\n\n".join([item.get("text", "") for item in systems])
                new_messages.append({"role": "system", "content": content.lstrip('\n')})
                del odata["system"]

            # Process Messages & Images
            for message in odata.get("messages", []):
                role = message.get("role")
                content = message.get("content", [])
                new_message = {"role": role}

                if isinstance(content, str):
                    new_message["content"] = content
                    new_messages.append(new_message)
                    continue

                if isinstance(content, list):
                    new_content = []
                    for item in content:
                        type_ = item.get("type")
                        if type_ == "text":
                            new_content.append(item)
                        elif type_ in ["image_url", "image"]:
                            media_url = item.get("image_url", {}).get("url") or ""
                            if media_url.startswith("data:"):
                                try:
                                    uploaded_url = utils.request.image(media_url, chat_id)
                                    if uploaded_url: media_url = uploaded_url
                                except Exception as e:
                                    log.error(f"Image upload failed: {e}")
                                    continue
                            new_content.append({"type": "image_url", "image_url": {"url": media_url}})
                    
                    if new_content:
                        new_message["content"] = new_content
                        new_messages.append(new_message)

            result = {
                **odata,
                "model": model,
                "messages": new_messages,
                "stream": True,
                "features": {"enable_thinking": False, **odata.get("features", {})},
            }
            return result

    class response:
        @staticmethod
        def parse(stream):
            # [FIX] chunk_size=None để fix lỗi buffering
            try:
                for line in stream.iter_lines(chunk_size=None): 
                    if not line: continue
                    if not line.startswith(b"data: "): continue
                    try: 
                        yield json.loads(line[6:].decode("utf-8", "ignore"))
                    except: continue
            except Exception as e:
                log.error(f"Stream error: {e}")

        @staticmethod
        def format(data):
            # [RESTORED] Logic xử lý tag thinking/reasoning
            data = data.get("data", "")
            if not data: return None
            phase = data.get("phase", "other")
            content = data.get("delta_content") or data.get("edit_content") or ""
            if not content: return None
            
            global phaseBak
            
            # Clean Z.ai specific tags
            if phase == "thinking" or (phase == "answer" and "summary>" in content):
                 content = re.sub(r"(?s)<details[^>]*?>.*?</details>", "", content)
                 content = content.replace("</thinking>", "").replace("<Full>", "").replace("</Full>", "")

            if phase == "thinking":
                content = re.sub(r'\n*<summary>.*?</summary>\n*', '\n\n', content)
                content = re.sub(r"<details[^>]*>\n*", "", content) # Strip details for OpenAI mode

            phaseBak = phase

            if phase == "tool_call": return {"tool_call": content}
            
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
    return utils.request.response(jsonify({
        "status": "ok",
        "mode": "OpenAI Only",
        "generated_ip_sample": generate_random_ip(),
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
        
        # Format request dùng logic chuẩn Z.ai
        data = {
            **utils.request.format(odata),
            "chat_id": id,
            "id": utils.request.id(),
        }
        
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
            reasoning = []
            for raw_chunk in utils.response.parse(response):
                if raw_chunk.get("data", {}).get("done"): break
                delta = utils.response.format(raw_chunk)
                if not delta: continue
                if "content" in delta: content.append(delta["content"])
                if "reasoning_content" in delta: reasoning.append(delta["reasoning_content"])
            
            final_msg = {"role": "assistant", "content": "".join(content)}
            if reasoning: final_msg["reasoning_content"] = "".join(reasoning)

            return utils.request.response(jsonify({
                "id": id,
                "object": "chat.completion",
                "choices": [{"index": 0, "message": final_msg, "finish_reason": "stop"}]
            }))

    except Exception as e:
        log.error(traceback.format_exc())
        return utils.request.response(jsonify({"error": 500, "message": str(e)})), 500

if __name__ == "__main__":
    log.info(f"Services Started on {cfg.api.port} (OpenAI Only)")
    if cfg.api.debug:
        app.run(host="0.0.0.0", port=cfg.api.port, threaded=True, debug=True)
    else:
        from gevent import pywsgi
        pywsgi.WSGIServer(('0.0.0.0', cfg.api.port), app).serve_forever()
