import os
import httpx
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("FPT_API_KEY")
base_url = os.getenv("FPT_BASE_URL", "https://mkp-api.fptcloud.com").rstrip("/")
model_name = os.getenv("FPT_MODEL_NAME", "gpt-oss-120b")

url = f"{base_url}/chat/completions"
headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
}
payload = {
    "model": model_name,
    "messages": [
        {"role": "system", "content": "You are a concise research extractor. Return JSON only."},
        {"role": "user", "content": "Return a JSON object with key 'status' and value 'ok'."},
    ],
    "temperature": 0.0,
}

print(f"Connecting to: {url} with model: {model_name}...")

try:
    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, headers=headers, json=payload)
        print(f"HTTP Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print("Response:", data["choices"][0]["message"]["content"])
            print("Usage:", data.get("usage", {}))
        else:
            print("Error details:", response.text)
except Exception as e:
    print(f"Connection failed: {e}")
