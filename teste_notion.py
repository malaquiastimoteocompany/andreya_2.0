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
    "parent": {"database_id": "8715f7ab-e1a4-43ee-9201-dfe9927a5090"},
    "properties": {
        "Scan ID": {"title": [{"text": {"content": "TESTE-CONEXAO"}}]}
    }
})
print(f"Base 1: {r.status_code} — {r.json().get('object', r.text[:200])}")

# Teste Base 2
r2 = requests.post("https://api.notion.com/v1/pages", headers=headers, json={
    "parent": {"database_id": "d8d785c1-f500-4a87-8074-59f07831cfbb"},
    "properties": {
        "Token": {"title": [{"text": {"content": "TESTE"}}]}
    }
})
print(f"Base 2: {r2.status_code} — {r2.json().get('object', r2.text[:200])}")
