import os
import json
import base64
import asyncio
import numpy as np
import sounddevice as sd
import requests
import websockets
from dotenv import load_dotenv, find_dotenv


# --- load env ---
load_dotenv(override=True)

dotenv_path = find_dotenv(usecwd=True)
print(".env path:", dotenv_path or "(not found)")
loaded = load_dotenv(dotenv_path=dotenv_path, override=True)
print(".env loaded:", loaded)
XI_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:5000/process_input")


def _short(s: str | None) -> str:
    return ((s or "")[:10] + "…") if s else "(missing)"


print("🔑 ELEVENLABS_API_KEY:", _short(XI_API_KEY))
print("🧠 ELEVENLABS_AGENT_ID:", AGENT_ID or "(missing)")
print("🌐 BACKEND_URL:", BACKEND_URL)

# --- audio capture settings ---
SAMPLE_RATE = 16000  # 16 kHz mono 16-bit PCM is typically accepted
CHANNELS = 1
CHUNK_MS = 200  # send ~200ms frames
CHUNK_SAMPLES = int(SAMPLE_RATE * (CHUNK_MS / 1000.0))

_sent = 0
_pushed = 0

out_stream = None # speaker for the agent audio

INPUT_DEVICE_INDEX = 2
OUTPUT_DEVICE_INDEX = None

# Shared queue for PCM frames
audio_q: asyncio.Queue[np.ndarray] = asyncio.Queue()


# Mic callback pushes audio into the queue
def _on_audio(indata, frames, time, status):
    # indata is float32 [-1,1]; convert to int16 PCM
    pcm16 = np.clip(indata[:, 0], -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype(np.int16)

    # split into ~CHUNK_SAMPLES chunks
    for start in range(0, len(pcm16), CHUNK_SAMPLES):
        chunk = pcm16[start:start + CHUNK_SAMPLES]
        if len(chunk) > 0:
            audio_q.put_nowait(chunk.copy())

    global _pushed  # >>> ADDED
    _pushed += 1
    if _pushed % 5 == 0:
        print(f"mic->queue chunk #{_pushed} ({len(chunk)} samples)")


async def ws_sender(ws):
    global _sent
    commit_every = int(1000 / CHUNK_MS)  # ~once per second
    n = 0

    # rolling energy of the current segment (simple mean abs of int16)
    energy = 0.0
    FRAMES_IN_SEG = 0

    # heuristic threshold: tweak if needed
    # (typical speech chunks averaged over 1s land well above 80–150)
    ENERGY_THRESHOLD = 60.0

    while True:
        chunk = await audio_q.get()             # np.int16, shape (CHUNK_SAMPLES,)
        # accumulate energy
        energy += float(np.mean(np.abs(chunk)))
        FRAMES_IN_SEG += 1

        # append audio
        payload = base64.b64encode(chunk.tobytes()).decode("ascii")
        await ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": payload
        }))
        _sent += 1
        if _sent % 5 == 0:
            print(f"✅ sent append #{_sent}")

        n += 1
        if n % commit_every == 0:
            # commit the segment we just sent
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            print(f"✅ sent commit  | energy={energy:.1f} over {FRAMES_IN_SEG} frames")

            # only request a response if we actually had voice energy
            if energy >= ENERGY_THRESHOLD:
                await ws.send(json.dumps({
                    "type": "response.create",
                    "response": { "conversation": True }
                }))
                print("➡️ requested agent response (voice detected)")
            else:
                print("🤫 skipped response.create (silence)")

            # reset accumulators for next 1s window
            energy = 0.0
            FRAMES_IN_SEG = 0


async def ws_receiver(ws):
    """
    Verbose receiver:
    - prints EVERY incoming message (first 300 chars)
    - plays agent audio to your speakers
    - on final transcript events: call your backend, then ask 11Labs to speak the reply
    """
    global out_stream  # <-- we added this global earlier

    FINAL_TYPES = {
        "transcript.final",
        "transcription.final",
        "conversation.transcript.final",
        "conversation.item.completed",   # some APIs emit final text in items
    }

    while True:
        msg = await ws.recv()
        # raw debug (trim so the terminal doesn't explode)
        print("WS IN raw:", (msg[:300] if isinstance(msg, str) else str(msg)[:300]))

        # try to parse JSON
        try:
            data = json.loads(msg)
        except Exception as e:
            print("[WARN] non-JSON frame:", e)
            continue

        evt_type = data.get("type")
        print("WS IN type:", evt_type)

        # ▶ AUDIO FROM AGENT — handle multiple payload shapes
        b64 = (
            # common shapes
            data.get("audio_base_64")
            or (data.get("audio_event") or {}).get("audio_base_64")
            or data.get("audio_base64")
            # some APIs send deltas as base64 in "delta"
            or (data.get("delta") if evt_type in ("audio.delta", "response.audio.delta") else None)
        )

        if evt_type in ("audio", "audio_event", "audio.delta", "response.audio.delta") and b64:
            try:
                # strip any data: prefix if present
                if "," in b64 and b64.strip().startswith("data:"):
                    b64 = b64.split(",", 1)[1]

                pcm_bytes = base64.b64decode(b64)

                # detect server-advertised sample rate if present; default 16k
                sr = 16000
                fmt = (data.get("audio_format") or {})  # some payloads include this
                try:
                    sr = int(fmt.get("sample_rate_hz") or sr)
                except Exception:
                    pass

                # (re)open speaker with matching samplerate if needed
                global out_stream
                if (out_stream is None) or (getattr(out_stream, "samplerate", None) != sr):
                    if out_stream:
                        try:
                            out_stream.stop(); out_stream.close()
                        except Exception:
                            pass
                    out_stream = sd.OutputStream(samplerate=sr, channels=1, dtype="float32")
                    out_stream.start()
                    print(f"🔊 speaker ready @ {sr} Hz")

                # int16 -> float32 [-1,1] and play
                pcm16 = np.frombuffer(pcm_bytes, dtype=np.int16)
                audio = (pcm16.astype(np.float32)) / 32768.0
                out_stream.write(audio)
                # debug
                print(f"▶ played {len(pcm_bytes)} bytes ({len(pcm16)} samples @ {sr} Hz)")
            except Exception as e:
                print("[WARN] failed to play agent audio:", e)
            continue

        # ▶ AUDIO FROM AGENT (several possible shapes)
        #   1) {"type":"audio","audio_base_64":"..."}
        #   2) {"type":"audio_event","audio_event":{"audio_base_64":"..."}}
        #   3) {"type":"audio.delta","audio_base_64":"..."}   (some SDKs)
        b64 = (
            data.get("audio_base_64")
            or (data.get("audio_event") or {}).get("audio_base_64")
            or data.get("audio_base64")        # seen in some payloads
        )
        if evt_type in ("audio", "audio_event", "audio.delta") and b64:
            try:
                pcm_bytes = base64.b64decode(b64)
                pcm16 = np.frombuffer(pcm_bytes, dtype=np.int16)

                if out_stream is None:
                    # 16 kHz, mono, 16-bit PCM — matches session.update you send
                    out_stream = sd.OutputStream(samplerate=16000, channels=1, dtype="int16")
                    out_stream.start()

                out_stream.write(pcm16)
            except Exception as e:
                print("[WARN] failed to play agent audio:", e)
            continue  # handled this message

        # (Optional) Print agent text messages
        if evt_type == "agent_response":
            resp = data.get("agent_response") or data.get("response")
            if resp:
                print("🗣️ Agent:", resp)
            # do not 'continue' here necessarily; some agent responses may also carry transcripts

        # ▶ FINAL TRANSCRIPT → call your backend → ask 11Labs to speak reply
        if evt_type in FINAL_TYPES:
            text = (
                data.get("text")
                or data.get("transcript")
                or (data.get("item") or {}).get("transcript")
                or ""
            ).strip()

            if not text:
                print("[INFO] final event but no text field present")
                continue

            print(f"👂 Heard (final): {text}")

            # Send to your backend
            try:
                r = requests.post(
                    BACKEND_URL,
                    json={
                        "session_id": "test1",
                        "text": text,
                        "is_final": True,
                        "lang": "sv-SE",
                    },
                    timeout=20,
                )
                r.raise_for_status()
                reply = (r.json() or {}).get("response", "")
            except Exception as e:
                print("[ERR] backend call failed:", e)
                reply = text  # fallback so agent still speaks

            print(f"🤖 Backend reply: {reply}")

            # Tell 11Labs to speak it
            await ws.send(json.dumps({
                "type": "response.create",
                "response": {"text": reply}
            }))
            print("➡️  Sent response.create to 11Labs")


async def main():
    if not XI_API_KEY or not AGENT_ID:
        raise RuntimeError("Missing ELEVENLABS_API_KEY or ELEVENLABS_AGENT_ID in .env")

    # Conversational AI websocket (Agent-based)
    uri = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={AGENT_ID}"

    async with websockets.connect(
        uri,
        extra_headers=[("xi-api-key", XI_API_KEY), ("X-Requested-With", "python")],
        ping_interval=30,
        close_timeout=5,
    ) as ws:
        print("WS connected, sending session.update...")
        # 1) Tell 11Labs what audio format we will stream
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "input_audio_format": {
                    "codec": "pcm_s16le",
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": 16000,
                    "channels": 1
                },

                "output_audio_format": {
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": 16000,
                    "channels": 1
                }
            }
        }))
        print("Sent session.update")

        # 2) Kick off the agent turn (keeps the convo alive)
        await ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "conversation": True,
                "instructions": "Hej! Jag är ansluten från Python."
            }
        }))
        print("kickoff response sent.")

        # 3) Prime ~200ms silence so session doesn't idle-close
        silence = (np.zeros(CHUNK_SAMPLES, dtype=np.int16)).tobytes()
        await ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(silence).decode("ascii")
        }))
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        print("commit")

        try:
            print("default devices:", sd.default.device)
            print("all input-capable devices:")
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) > 0:
                    sr = int(d.get("default_samplerate") or 0)
                    print(f".  [{i}] {d['name']} in:{d['max_input_channels']}  sr:{sr}")
        except Exception as e:
            print("mic devices enumeration failed:", e)

        # --- start mic + tasks exactly as you already have ---
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=_on_audio,
            blocksize=CHUNK_SAMPLES,
            device=(INPUT_DEVICE_INDEX, None)   # (input_index, output_index) → (2, None)
        )
        stream.start()
        try:
            sender_t = asyncio.create_task(ws_sender(ws))
            recv_t = asyncio.create_task(ws_receiver(ws))

            done, pending = await asyncio.wait(
                {sender_t, recv_t},
                return_when=asyncio.FIRST_EXCEPTION
            )

            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc:
                    raise exc
        finally:
            stream.stop()
            stream.close()
            if out_stream is not None:
                try:
                    out_stream.stop()
                    out_stream.close()
                except Exception:
                    pass
                    


if __name__ == "__main__":
    asyncio.run(main())
