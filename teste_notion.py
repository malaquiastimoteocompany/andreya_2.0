import os
import requests

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

# Teste Base 1
r = requests.post("https://api.notion.com/v1/pages", headers=headers, json={
    "parent": {"database_id": "ebd18b31-bad2-4faa-a812-ff6580dd4930"},
    "properties": {
        "Scan ID": {"title": [{"text": {"content": "TESTE-CONEXAO"}}]}
    }
})
print(f"Base 1: {r.status_code}")
print(r.text)

# Teste Base 2
r2 = requests.post("https://api.notion.com/v1/pages", headers=headers, json={
    "parent": {"database_id": "ef42937a-58a3-4957-8a90-30d78e8ff8db"},
    "properties": {
        "Token": {"title": [{"text": {"content": "TESTE"}}]}
    }
})
print(f"Base 2: {r2.status_code}")
print(r2.text)
