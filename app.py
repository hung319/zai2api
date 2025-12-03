# -*- coding: utf-8 -*-
"""
Z.ai 2 API (Final Stable)
Modified by CezDev:
- Added Uptime Page at /
- Fixed Empty Output (Disabled Buffering)
- Enhanced Error Handling in Stream
"""

import os, json, re, requests, logging, uuid, base64, sys
from datetime import datetime
from flask import Flask, request, Response, jsonify, make_response

from dotenv import load_dotenv
load_dotenv()

# --- CẤU HÌNH ---
BASE = str(os.getenv("BASE", "https://chat.z.ai"))
PORT = int(os.getenv("PORT", "8080"))
MODEL = str(os.getenv("MODEL", "GLM-4.5"))
TOKEN = str(os.getenv("TOKEN", "")).strip()
DEBUG_MODE = str(os.getenv("DEBUG", "false")).lower() == "true"
THINK_TAGS_MODE = str(os.getenv("THINK_TAGS_MODE", "reasoning"))
ANONYMOUS_MODE = str(os.getenv("ANONYMOUS_MODE", "true")).lower() == "true"
SERVER_API_KEY = str(os.getenv("API_KEY", "")).strip()

# Khởi tạo thời gian start server
START_TIME = datetime.now()

# Tiktoken Setup
cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tiktoken') + os.sep
os.environ["TIKTOKEN_CACHE_DIR"] = cache_dir

try:
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
except Exception as e:
    logging.warning(f"Tiktoken init warning: {e}")
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-FE-Version": "prod-fe-1.0.76",
    "sec-ch-ua": '"Not;A=Brand";v="99", "Edge";v="139"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Origin": BASE,
}

# Logger
logging.basicConfig(level=logging.DEBUG if DEBUG_MODE else logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

def debug(msg, *args):
    if DEBUG_MODE: log.debug(msg, *args)

app = Flask(__name__)

# --- AUTH CHECK ---
def check_auth():
    if not SERVER_API_KEY:
        return None
    auth_header = request.headers.get("Authorization", "")
    if not auth_header or not auth_header.startswith("Bearer ") or auth_header.split(" ")[1] != SERVER_API_KEY:
        return make_response(jsonify({"error": {"message": "Invalid API Key", "type": "auth_error"}}), 401)
    return None

# --- STREAM PROCESSOR ---
class StreamProcessor:
    def __init__(self):
        self.phase_bak = "thinking"

    def format_chunk(self, data):
        data = data.get("data", "")
        if not data: return None
        phase = data.get("phase", "other")
        content = data.get("delta_content") or data.get("edit_content") or ""
        
        # Nếu content rỗng, trả về None để skip (trừ khi cần keep-alive logic)
        if not content: return None
        
        content_bak = content
        
        # --- Logic Clean Tags ---
        if phase == "thinking" or (phase == "answer" and "summary>" in content):
            content = re.sub(r"(?s)<details[^>]*?>.*?</details>", "", content)
            content = content.replace("</thinking>", "").replace("<Full>", "").replace("</Full>", "")

            if phase == "thinking":
                content = re.sub(r'\n*<summary>.*?</summary>\n*', '\n\n', content)

            content = re.sub(r"<details[^>]*>\n*", "<reasoning>\n\n", content)
            content = re.sub(r"\n*</details>", "\n\n</reasoning>", content)

            if phase == "answer":
                match = re.search(r"(?s)^(.*?</reasoning>)(.*)$", content)
                if match:
                    before, after = match.groups()
                    if after.strip():
                        if self.phase_bak == "thinking":
                            content = "\n\n</reasoning>\n\n" + after.lstrip('\n')
                        elif self.phase_bak == "answer":
                            # Chỉ clear nếu Z.ai gửi lại toàn bộ text (duplicate). 
                            # Tuy nhiên, nếu Z.ai gửi delta, việc clear sẽ làm mất chữ.
                            # Logic an toàn: Nếu content sau khi clean giống hệt content trước đó thì skip
                            pass 
                    else:
                        content = "\n\n</reasoning>"
            
            # --- Logic Thay thế Tags ---
            if THINK_TAGS_MODE == "reasoning":
                if phase == "thinking": content = re.sub(r'\n>\s?', '\n', content)
                content = re.sub(r'\n*<summary>.*?</summary>\n*', '', content)
                content = re.sub(r"<reasoning>\n*", "", content)
                content = re.sub(r"\n*</reasoning>", "", content)
            elif THINK_TAGS_MODE == "think":
                if phase == "thinking": content = re.sub(r'\n>\s?', '\n', content)
                content = re.sub(r'\n*<summary>.*?</summary>\n*', '', content)
                content = re.sub(r"<reasoning>", "<think>", content)
                content = re.sub(r"</reasoning>", "</think>", content)
            elif THINK_TAGS_MODE == "strip":
                content = re.sub(r'\n*<summary>.*?</summary>\n*', '', content)
                content = re.sub(r"<reasoning>\n*", "", content)
                content = re.sub(r"</reasoning>", "", content)
            elif THINK_TAGS_MODE == "details":
                if phase == "thinking": content = re.sub(r'\n>\s?', '\n', content)
                content = re.sub(r"<reasoning>", "<details type=\"reasoning\" open><div>", content)
                thoughts = ""
                if phase == "answer":
                    safe_ctx = content_bak
                    summary_match = re.search(r"(?s)<summary>.*?</summary>", safe_ctx)
                    duration_match = re.search(r'duration="(\d+)"', safe_ctx)
                    if summary_match:
                        thoughts = f"\n\n{summary_match.group()}"
                    elif duration_match:
                        thoughts = f'\n\n<summary>Thought for {duration_match.group(1)} seconds</summary>'
                content = re.sub(r"</reasoning>", f"</div>{thoughts}</details>", content)
            else:
                content = re.sub(r"</reasoning>", "</reasoning>\n\n", content)

        self.phase_bak = phase

        if repr(content):
            if phase == "thinking" and THINK_TAGS_MODE == "reasoning":
                return {"role": "assistant", "reasoning_content": content}
            return {"role": "assistant", "content": content}
        return None

# --- UTILS CLASS ---
class utils:
    @staticmethod
    class request:
        @staticmethod
        def chat(data, chat_id):
            debug("Chat Request: %s", json.dumps(data))
            return requests.post(
                f"{BASE}/api/chat/completions", 
                json=data, 
                headers={**BROWSER_HEADERS, "Authorization": f"Bearer {utils.request.token()}", "Referer": f"{BASE}/c/{chat_id}"}, 
                stream=True, 
                timeout=120
            )
        @staticmethod
        def image(data_url, chat_id):
            try:
                if ANONYMOUS_MODE or not data_url.startswith("data:"): return None
                header, encoded = data_url.split(",", 1)
                mime_type = header.split(";")[0].split(":")[1] if ":" in header else "image/jpeg"
                image_data = base64.b64decode(encoded)
                filename = str(uuid.uuid4())
                
                response = requests.post(
                    f"{BASE}/api/v1/files/", 
                    files={"file": (filename, image_data, mime_type)}, 
                    headers={**BROWSER_HEADERS, "Authorization": f"Bearer {utils.request.token()}", "Referer": f"{BASE}/c/{chat_id}"}, 
                    timeout=60
                )
                if response.status_code == 200:
                    result = response.json()
                    return f"{result.get('id')}_{result.get('filename')}"
                else: raise Exception(response.text)
            except Exception as e:
                debug("Upload failed: %s", e)
            return None
        @staticmethod
        def id(prefix = "msg") -> str:
            return f"{prefix}-{int(datetime.now().timestamp()*1e9)}"
        @staticmethod
        def token() -> str:
            if not ANONYMOUS_MODE: return TOKEN
            try:
                r = requests.get(f"{BASE}/api/v1/auths/", headers=BROWSER_HEADERS, timeout=8)
                token = r.json().get("token")
                if token: return token
            except Exception as e:
                debug("Token fetch failed: %s", e)
            return TOKEN
        @staticmethod
        def response(resp):
            resp.headers.update({
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            })
            return resp
    @staticmethod
    class response:
        @staticmethod
        def parse(stream):
            for line in stream.iter_lines(decode_unicode=True):
                if not line: continue
                if line.startswith("data: "):
                    try: 
                        yield json.loads(line[6:])
                    except: continue
        @staticmethod
        def count(text):
            return len(enc.encode(text))

# --- ROUTES ---

# [NEW] Uptime Page
@app.route("/", methods=["GET"])
def index():
    uptime = datetime.now() - START_TIME
    return jsonify({
        "status": "online",
        "service": "Z.ai OpenAI Proxy",
        "uptime": str(uptime).split('.')[0],
        "model": MODEL,
        "auth": "ENABLED" if SERVER_API_KEY else "DISABLED",
        "python": sys.version.split()[0]
    })

@app.route("/v1/models", methods=["GET", "POST", "OPTIONS"])
def models():
    if request.method == "OPTIONS": return utils.request.response(make_response())
    auth_err = check_auth()
    if auth_err: return auth_err
    
    try:
        def format_model_name(name: str) -> str:
            if not name: return ""
            parts = name.split('-')
            if len(parts) == 1: return parts[0].upper()
            formatted = [parts[0].upper()]
            for p in parts[1:]:
                if not p: formatted.append("")
                elif p.isdigit(): formatted.append(p)
                elif any(c.isalpha() for c in p): formatted.append(p.capitalize())
                else: formatted.append(p)
            return "-".join(formatted)

        def is_english_letter(ch: str) -> bool:
            return 'A' <= ch <= 'Z' or 'a' <= ch <= 'z'

        headers = {**BROWSER_HEADERS, "Authorization": f"Bearer {utils.request.token()}"}
        r = requests.get(f"{BASE}/api/models", headers=headers, timeout=10).json()
        models_list = []
        for m in r.get("data", []):
            if not m.get("info", {}).get("is_active", True): continue
            model_id, model_name = m.get("id"), m.get("name")
            if model_id.startswith(("GLM", "Z")): model_name = model_id
            if not model_name or not is_english_letter(model_name[0]):
                model_name = format_model_name(model_id)
            models_list.append({
                "id": model_id,
                "object": "model",
                "name": model_name,
                "created": m.get("info", {}).get("created_at", int(datetime.now().timestamp())),
                "owned_by": "z.ai"
            })
        return utils.request.response(jsonify({"object":"list","data":models_list}))
    except Exception as e:
        return utils.request.response(jsonify({"error": f"Fetch models failed: {str(e)}"})), 500

@app.route("/v1/chat/completions", methods=["GET", "POST", "OPTIONS"])
def OpenAI_Compatible():
    if request.method == "OPTIONS": return utils.request.response(make_response())
    auth_err = check_auth()
    if auth_err: return auth_err

    odata = request.get_json(force=True, silent=True) or {}
    chat_id = utils.request.id("chat")
    model = odata.get("model", MODEL)
    messages = odata.get("messages", [])
    
    features = { "enable_thinking": True }
    if "features" in odata: features.update(odata["features"])
    
    stream = odata.get("stream", False)
    include_usage = stream and odata.get("stream_options", {}).get("include_usage", False)

    for message in messages:
        if isinstance(message.get("content"), list):
            for content_item in message["content"]:
                if content_item.get("type") == "image_url":
                    url = content_item.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        file_url = utils.request.image(url, chat_id)
                        if file_url: content_item["image_url"]["url"] = file_url

    data = {
        "messages": messages,
        "model": model,
        "stream": True,
        "chat_id": chat_id,
        "features": features
    }
    if "temperature" in odata: data["temperature"] = odata["temperature"]
    if "top_p" in odata: data["top_p"] = odata["top_p"]

    try:
        response = utils.request.chat(data, chat_id)
    except Exception as e:
        return utils.request.response(make_response(f"Upstream request failed: {e}", 502))

    prompt_tokens = utils.response.count("".join(
        c if isinstance(c, str) else (c.get("text", "") if isinstance(c, dict) and c.get("type") == "text" else "")
        for m in messages
        for c in ([m["content"]] if isinstance(m.get("content"), str) else (m.get("content") or []))
    ))

    if stream:
        def stream_generator():
            completion_str = ""
            completion_tokens = 0
            processor = StreamProcessor()

            try:
                for data in utils.response.parse(response):
                    is_done = data.get("data", {}).get("done", False)
                    delta = processor.format_chunk(data)
                    finish_reason = "stop" if is_done else None

                    if delta:
                        chunk = {
                            "id": utils.request.id('chatcmpl'),
                            "object": "chat.completion.chunk",
                            "created": int(datetime.now().timestamp()),
                            "model": model,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                        if "content" in delta: completion_str += delta["content"]
                        if "reasoning_content" in delta: completion_str += delta["reasoning_content"]
                        completion_tokens = utils.response.count(completion_str)
                    
                    if is_done:
                        end_chunk = {
                            "id": utils.request.id('chatcmpl'),
                            "object": "chat.completion.chunk",
                            "created": int(datetime.now().timestamp()),
                            "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                        }
                        yield f"data: {json.dumps(end_chunk)}\n\n"
                        break
            except Exception as stream_e:
                # Trả về lỗi trong stream nếu có sự cố
                err_chunk = {
                    "error": {"message": f"Stream error: {str(stream_e)}", "type": "stream_exception"}
                }
                yield f"data: {json.dumps(err_chunk)}\n\n"

            if include_usage:
                usage_chunk = {
                    "id": utils.request.id('chatcmpl'),
                    "object": "chat.completion.chunk",
                    "created": int(datetime.now().timestamp()),
                    "model": model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens
                    }
                }
                yield f"data: {json.dumps(usage_chunk)}\n\n"

            yield "data: [DONE]\n\n"

        # [KEY FIX] Thêm headers để ngăn Nginx/Flask buffering
        return Response(stream_generator(), mimetype="text/event-stream", headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        })
    else:
        # Non-stream support
        processor = StreamProcessor()
        contents = {"content": [], "reasoning_content": []}
        
        for odata in utils.response.parse(response):
            if odata.get("data", {}).get("done"): break
            delta = processor.format_chunk(odata)
            if delta:
                if "content" in delta: contents["content"].append(delta["content"])
                if "reasoning_content" in delta: contents["reasoning_content"].append(delta["reasoning_content"])

        final_msg = {"role": "assistant"}
        full_text = ""
        if contents["reasoning_content"]:
            final_msg["reasoning_content"] = "".join(contents["reasoning_content"])
            full_text += final_msg["reasoning_content"]
        if contents["content"]:
            final_msg["content"] = "".join(contents["content"])
            full_text += final_msg["content"]
        
        c_tokens = utils.response.count(full_text)

        return utils.request.response(jsonify({
            "id": utils.request.id("chatcmpl"),
            "object": "chat.completion",
            "created": int(datetime.now().timestamp()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": final_msg,
                "message": final_msg,
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": c_tokens,
                "total_tokens": prompt_tokens + c_tokens
            }
        }))

if __name__ == "__main__":
    log.info("---------------------------------------------------------------------")
    log.info("Z.ai 2 API (Final Stable)")
    log.info(f"API Key: {'SET' if SERVER_API_KEY else 'NONE'}")
    log.info(f"Port: {PORT}")
    log.info("---------------------------------------------------------------------")
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=DEBUG_MODE)
