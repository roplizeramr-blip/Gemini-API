# Deploy on Railway

This repository uses the root Dockerfile, listens on Railway's injected PORT, and exposes /health.

## Railway setup

1. Create a new Railway project and add the GitHub repository: roplizeramr-blip/Gemini-API

2. Keep the service at the repository root. Railway will detect the root Dockerfile.

3. Add a Railway Volume to the service and mount it at /data.

Persistent data is stored at:
- /data/gateway/gateway.sqlite3
- /data/gateway/uploads/
- /data/gemini/cookies/

4. Add these Variables in the Railway service:

| Variable | Required | Value |
|---|---|---|
| GATEWAY_API_KEY | Yes | A long random secret for your gateway |
| GEMINI_SECURE_1PSID | Yes | Current Gemini Web __Secure-1PSID |
| GEMINI_SECURE_1PSIDTS | Optional | Current Gemini Web __Secure-1PSIDTS, when present |
| GEMINI_COOKIE_PATH | Yes | /data/gemini/cookies |
| GATEWAY_DATA_DIR | Yes | /data/gateway |
| GEMINI_AUTO_REFRESH | Recommended | true |
| GEMINI_REFRESH_INTERVAL | Optional | 600 |
| GEMINI_TIMEOUT | Optional | 450 |
| MAX_UPLOAD_MB | Optional | 50 |
| CORS_ORIGINS | Optional | * or your frontend origins |
| GEMINI_PROXY | Optional | Proxy URL if your network requires one |

Do not commit Google session cookies or GATEWAY_API_KEY to GitHub.

5. Set the Railway deployment Healthcheck Path to /health.

6. Generate a Railway public domain.

7. Keep this service at one replica. The gateway keeps one Gemini Web session in memory and uses a local SQLite database on the attached volume.

## Test

Health: GET https://YOUR-DOMAIN/health

Authenticated model discovery: GET https://YOUR-DOMAIN/v1/models

Example:

~~~bash
curl "https://YOUR-DOMAIN/v1/models" \\
  -H "Authorization: Bearer YOUR_GATEWAY_API_KEY"
~~~

Chat:

~~~bash
curl "https://YOUR-DOMAIN/v1/chat/completions" \\
  -H "Authorization: Bearer YOUR_GATEWAY_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "gemini-web-default",
    "messages": [{"role": "user", "content": "Hello from Railway"}]
  }'
~~~

Streaming uses the same endpoint with "stream": true and returns Server-Sent Events.

## Exposed API

- GET /health
- GET /ready
- GET /v1/models
- POST /v1/chat/completions
- POST /v1/images/generations
- POST /v1/files
- GET /v1/files/{file_id}
- DELETE /v1/files/{file_id}
- GET /api/models
- GET /api/account
- GET /api/conversations
- GET /api/conversations/{conversation_id}
- DELETE /api/conversations/{conversation_id}
- POST /api/generate
- POST /api/research

The gateway resolves Gemini Web models dynamically from the upstream client instead of embedding a fixed model list.

## Architecture note

The deployed service currently wraps the reverse-engineered Gemini Web client in this repository. It is therefore Gemini-Web/session based, not the official Gemini API/Interactions API. This distinction is intentional: it preserves Web-only functionality exposed by the underlying library. An official Gemini API/Interactions provider can be added later as a separate backend without changing the public gateway contract.

## License

The repository is AGPL-3.0. Keep the license and required source notices when distributing or operating modified versions.
