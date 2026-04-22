from fastapi import FastAPI, Request, Header, HTTPException
import requests
import os
import hmac
import hashlib
import base64
import re
import json # 引入 json 模組以解析串流資料

app = FastAPI()

LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.getenv('DIFY_API_KEY')

@app.post("/webhook")
async def callback(request: Request, x_line_signature: str = Header(None)):
    body = await request.body()
    body_str = body.decode("utf-8")

    # 1. 驗證這條訊息真的是 LINE 官方傳來的 (資安防護 HMAC-SHA256)
    if LINE_SECRET and x_line_signature:
        hash_val = hmac.new(LINE_SECRET.encode('utf-8'), body_str.encode('utf-8'), hashlib.sha256).digest()
        signature = base64.b64encode(hash_val).decode('utf-8')
        if signature != x_line_signature:
            raise HTTPException(status_code=400, detail="Invalid signature")

    # 2. 解析訊息並處理
    data = await request.json()
    for event in data.get('events', []):
        if event.get('type') == 'message' and event.get('message', {}).get('type') == 'text':
            user_message = event['message']['text']
            reply_token = event['replyToken']
            user_id = event['source'].get('userId', 'unknown_user')

            # --- 步驟 A：呼叫 Dify 大腦 (改用 Streaming 模式以支援 Agent) ---
            try:
                dify_res = requests.post(
                    "https://api.dify.ai/v1/chat-messages",
                    headers={"Authorization": f"Bearer {DIFY_API_KEY}"},
                    json={
                        "inputs": {},
                        "query": user_message,
                        "response_mode": "streaming", # Agent 必須使用串流模式
                        "user": user_id
                    },
                    stream=True # 啟用 requests 的串流讀取
                )
                
                # 如果 Dify 拒絕連線 (例如密碼錯或模式不支援)，抓出錯誤訊息
                if dify_res.status_code != 200:
                    error_data = dify_res.json()
                    error_msg = error_data.get('message', str(error_data))
                    answer = f"⚠️ Dify 大腦回報錯誤：\n{error_msg}\n\n(請根據此錯誤檢查 Dify 設定)"
                else:
                    # 成功連線！開始拼湊串流回傳的字串
                    answer = ""
                    for line in dify_res.iter_lines():
                        if line:
                            decoded_line = line.decode('utf-8')
                            if decoded_line.startswith('data:'):
                                data_str = decoded_line[5:].strip()
                                try:
                                    json_data = json.loads(data_str)
                                    # 兼容 Agent 模式 (agent_message) 與 Chatflow 模式 (message)
                                    event_type = json_data.get('event')
                                    if event_type in ['message', 'agent_message']:
                                        # 抓取答案片段並累加
                                        answer += json_data.get('answer', '')
                                except json.JSONDecodeError:
                                    pass
                                    
                    # 如果拼湊完還是空的，給個備用訊息
                    if not answer:
                        answer = "Dify 處理完畢，但未產生文字回應 (可能只回傳了思考過程，請檢查 Agent 設定)。"
                        
            except Exception as e:
                answer = f"Vercel 伺服器連線例外錯誤：{str(e)}"

            # --- 步驟 B：超連結轉按鈕邏輯 (Flex Message) ---
            # 尋找所有類似 [標題](https://網址) 的格式
            links = re.findall(r'\[([^\]]+)\]\((https?://[^\)]+)\)', answer)
            
            if links:
                # 將原文中的 Markdown 網址格式移除，只保留純文字標題，避免畫面充斥密密麻麻的網址
                clean_text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'【\1】 (請點擊下方按鈕)', answer)
                
                # 製作 Flex Message 按鈕陣列 (LINE 限制不能太多，此處取前 4 個連結)
                buttons = []
                for title, url in links[:4]:
                    buttons.append({
                        "type": "button",
                        "style": "primary",
                        "margin": "sm",
                        "action": {
                            "type": "uri",
                            "label": title[:20], # LINE 規定按鈕文字最多 20 字，作為代表性文字
                            "uri": url
                        }
                    })
                
                # 組裝完整的 Flex Message 卡片
                messages = [{
                    "type": "flex",
                    "altText": "助理傳送了一個連結給您",
                    "contents": {
                        "type": "bubble",
                        "body": {
                            "type": "box",
                            "layout": "vertical",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": clean_text,
                                    "wrap": True
                                }
                            ] + buttons
                        }
                    }
                }]
            else:
                # 若沒有超連結，就傳送一般純文字訊息
                messages = [{"type": "text", "text": answer}]

            # --- 步驟 C：傳回 LINE ---
            requests.post(
                "https://api.line.me/v2/bot/message/reply",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
                },
                json={
                    "replyToken": reply_token,
                    "messages": messages
                }
            )
    return 'OK'
