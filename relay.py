# relay.py — text bridge between ElevenLabs Agent <-> your backend /process_input
import os, json, asyncio, requests, websockets
from dotenv import load_dotenv, find_dotenv

# --- env ---
load_dotenv(find_dotenv(usecwd=True), override=True)
XI_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
AGENT_ID    = os.getenv("ELEVENLABS_AGENT_ID", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:5000/process_input")

if not XI_API_KEY or not AGENT_ID:
    raise SystemExit("Missing ELEVENLABS_API_KEY or ELEVENLABS_AGENT_ID in .env")

URI = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={AGENT_ID}"

# Final transcript event names we care about
FINAL_TYPES = {
    "transcript.final",
    "transcription.final",
    "conversation.transcript.final",
    "conversation.item.completed",
}

def extract_final_text(evt: dict) -> str:
    t = (evt.get("text") or evt.get("transcript") or "").strip()
    if t:
        return t
    item = evt.get("item") or {}
    if isinstance(item, dict):
        t = (item.get("transcript") or item.get("text") or "").strip()
        if t:
            return t
    return ""

def session_id_from(evt: dict) -> str:
    # Try to keep per-call session IDs stable if the event carries one
    return (
        evt.get("conversation_id")
        or (evt.get("conversation") or {}).get("id")
        or "relay"
    )

async def run_bridge():
    headers = [("xi-api-key", XI_API_KEY), ("X-Requested-With", "python")]
    async with websockets.connect(URI, extra_headers=headers, ping_interval=30) as ws:
        print("✅ Relay connected to 11Labs Agent (text-only). Waiting for transcripts…")

        # Optional: tell agent we’re ready (keeps session alive)
        await ws.send(json.dumps({
            "type": "response.create",
            "response": {"conversation": True, "instructions": "Bridge online."}
        }))

        while True:
            msg = await ws.recv()
            try:
                data = json.loads(msg)
            except Exception:
                continue

            evt_type = data.get("type") or ""
            if evt_type not in ("ping_event", "audio", "audio_event", "response.audio.delta", "agent_response"):
                print("WS type:", evt_type)

            if evt_type in FINAL_TYPES:
                text = extract_final_text(data)
                if not text:
                    continue
                sid = session_id_from(data)
                print(f"👂 USER[{sid}] (final): {text}")

                # Call your backend
                try:
                    r = requests.post(
                        BACKEND_URL,
                        json={
                            "session_id": sid,
                            "text": text,
                            "is_final": True,
                            "lang": "sv-SE",
                        },
                        timeout=30,
                    )
                    r.raise_for_status()
                    reply = (r.json() or {}).get("response", "") or ""
                except Exception as e:
                    print("[ERR] backend failed:", e)
                    reply = "Förlåt, jag hade ett tekniskt problem. Kan du upprepa?"

                print(f"🤖 BACKEND REPLY[{sid}]: {reply}")

                # Tell the Agent to speak the backend reply
                await ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"text": reply, "conversation": True}
                }))
                print("➡️ Sent response.create")

if __name__ == "__main__":
    asyncio.run(run_bridge())