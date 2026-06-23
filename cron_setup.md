# Configuração cron-job.org — Andreya v2

Cada cron job envia um POST para a GitHub API que dispara o workflow `scanner.yml`.
Usar GitHub Actions Schedule nativo é proibido — atrasos de até 10 min são inaceitáveis
para o scan de breakout (30 min) e scan leve (1h).

---

## URL do endpoint (igual para todos os jobs)

```
https://api.github.com/repos/malaquiastimoteocompany/andreya_2.0/actions/workflows/scanner.yml/dispatches
```

## Headers (iguais para todos os jobs)

```
Authorization: Bearer {GITHUB_PAT}
Content-Type: application/json
```

`GITHUB_PAT` — Personal Access Token com permissão `workflow`. Criar em:
GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens
→ Repository: andreya_2.0 → Permissions: Actions: Read & Write

---

## Jobs a criar no cron-job.org

### Timezone de todos os jobs: `Europe/Lisbon`

---

### 1. Scan Pesado — 06h Lisboa

- **Schedule:** `0 6 * * *`
- **Method:** POST
- **Body:**
```json
{"ref": "main", "inputs": {"scan_tipo": "pesado"}}
```

---

### 2. Scan Pesado — 10h Lisboa

- **Schedule:** `0 10 * * *`
- **Body:**
```json
{"ref": "main", "inputs": {"scan_tipo": "pesado"}}
```

---

### 3. Scan Pesado — 13h Lisboa

- **Schedule:** `0 13 * * *`
- **Body:**
```json
{"ref": "main", "inputs": {"scan_tipo": "pesado"}}
```

---

### 4. Scan Pesado — 18h Lisboa

- **Schedule:** `0 18 * * *`
- **Body:**
```json
{"ref": "main", "inputs": {"scan_tipo": "pesado"}}
```

---

### 5. Scan Pesado — 22h Lisboa

- **Schedule:** `0 22 * * *`
- **Body:**
```json
{"ref": "main", "inputs": {"scan_tipo": "pesado"}}
```

---

### 6. Scan Leve — a cada hora

- **Schedule:** `5 * * * *`
  (ao minuto :05 para não colidir com scans pesados, que ficam no :00)
- **Body:**
```json
{"ref": "main", "inputs": {"scan_tipo": "leve"}}
```

---

### 7. Scan Breakout — a cada 30 minutos (1ª metade)

- **Schedule:** `10 * * * *`
  (ao minuto :10 — 30 min após o scan leve das :05)
- **Body:**
```json
{"ref": "main", "inputs": {"scan_tipo": "breakout"}}
```

---

### 8. Scan Breakout — a cada 30 minutos (2ª metade)

- **Schedule:** `40 * * * *`
  (ao minuto :40 — 30 min após o breakout das :10)
- **Body:**
```json
{"ref": "main", "inputs": {"scan_tipo": "breakout"}}
```

---

## Resumo dos 8 jobs

| Job | Schedule (Lisboa) | Tipo |
|-----|-------------------|------|
| Pesado 06h | `0 6 * * *`   | pesado |
| Pesado 10h | `0 10 * * *`  | pesado |
| Pesado 13h | `0 13 * * *`  | pesado |
| Pesado 18h | `0 18 * * *`  | pesado |
| Pesado 22h | `0 22 * * *`  | pesado |
| Leve       | `5 * * * *`   | leve |
| Breakout 1 | `10 * * * *`  | breakout |
| Breakout 2 | `40 * * * *`  | breakout |

---

## Sequência de minutos por hora (exemplo)

```
:00 → scan pesado (nas horas 6/10/13/18/22) ou nada
:05 → scan leve
:10 → scan breakout
:35 → scan leve  ← NÃO EXISTE (o leve só corre 1x/hora)
:40 → scan breakout
```

Nota: o scan leve corre 1× por hora (ao :05).
Os dois scans de breakout correm ao :10 e ao :40, espaçados 30 min entre si.

---

## GitHub Secrets a configurar

Ir a: `github.com/malaquiastimoteocompany/andreya_2.0` → Settings → Secrets → Actions

| Secret | Valor |
|--------|-------|
| `MEXC_API_KEY` | API key da MEXC |
| `MEXC_API_SECRET` | API secret da MEXC |
| `TELEGRAM_BOT_TOKEN` | Token do bot Telegram (Andreya) |
| `ANTHROPIC_API_KEY` | API key da Anthropic (Claude) |
| `NOTION_TOKEN` | Integration token do Notion |

Nota: `GITHUB_TOKEN` é automático — não precisas de o configurar.
