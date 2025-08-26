from flask import Blueprint, request, send_file, jsonify
from elevenlabs.client import ElevenLabs
import io
import os
from dotenv import load_dotenv

load_dotenv()

tts_bp = Blueprint('tts', __name__)

# Initialize ElevenLabs client
client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))


@tts_bp.route("/generate-audio", methods=["POST"])
def generate_audio():
    try:
        data = request.get_json()
        text = data.get("text", "").strip() if data else ""
        voice_id = (data or {}).get("voice_id") or os.getenv("ELEVENLABS_VOICE_ID")

        if not text:
            return jsonify({"error": "No text provided"}), 400

        print(f"🎤 Received text for TTS: {text}")

        # Request MP3 audio stream from ElevenLabs
        audio_stream = client.text_to_speech.convert(
            voice_id=voice_id,
            model_id="eleven_multilingual_v2",
            optimize_streaming_latency="0",
            output_format="mp3_44100_128",
            text=text
        )

        # Combine all chunks into a BytesIO
        audio_bytes = io.BytesIO()
        total_size = 0
        for chunk in audio_stream:
            if chunk:
                audio_bytes.write(chunk)
                total_size += len(chunk)

        print(f"✅ Received audio data from ElevenLabs: {total_size} bytes")

        if total_size < 1000:
            print("⚠️ Warning: Audio file is very small, may be corrupted.")

        audio_bytes.seek(0)

        return send_file(
            audio_bytes,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name="output.mp3"
        )

    except Exception as e:
        print(f"❌ Error generating audio: {e}")
        return jsonify({"error": str(e)}), 500
