# Ankur AI

A minimal browser-based voice conversation app powered by Gemini Live.

## Deploy on Render

1. Extract the ZIP, then copy the contents of `ankur-ai-cloud` into the root of a GitHub repository. The repository root must contain `main.py`, `persona.py`, `render.yaml`, and the `static` folder (with `static/index.html` inside it).
2. In Render, choose **New → Blueprint** and connect that repository.
3. When prompted, enter your Gemini API key for `GEMINI_API_KEY` and create a private passcode for `ACCESS_CODE`.
4. Select **Apply**. Render builds and starts the app using the included configuration.
5. Open the `onrender.com` URL shown by Render. Enter your passcode and start a call.

The app uses the `Charon` male voice by default. The Gemini API key remains on the server and is not sent to the browser.

## Run locally on Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:GEMINI_API_KEY="your_gemini_api_key"
$env:ACCESS_CODE="choose_a_private_passcode"
$env:LIVE_VOICE="Charon"
uvicorn main:app --reload --port 8080
```

Then open `http://localhost:8080`.

## Configuration

- `GEMINI_API_KEY` — required Gemini API key.
- `ACCESS_CODE` — optional passcode protecting the app. Recommended for any public deployment.
- `LIVE_VOICE` — defaults to `Charon`.
- `LIVE_MODEL` — defaults to the currently working `gemini-3.8-live` model.
- `MAX_SESSION_SECONDS` — defaults to 600 seconds.
- `GREET` — defaults to `1`; set to `0` to disable the opening greeting.

The Render free instance can take around a minute to wake after a long idle period. An active voice call uses a WebSocket connection.
