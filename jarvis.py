#!/usr/bin/env python3
import sys
import os
import signal
import subprocess
import threading
import time
import sounddevice as sd
import numpy as np
from faster_whisper import WhisperModel
import evdev
from evdev import ecodes

# --- Mindful Configuration ---
MODEL_SIZE = os.environ.get("JARVIS_MODEL", "Systran/faster-whisper-base.en")
DEVICE = "cpu"
COMPUTE_TYPE = "int8"
CPU_THREADS = int(os.environ.get("JARVIS_THREADS", 4))
SAMPLE_RATE = 16000

# Keep models air-gapped inside this repo
LOCAL_MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
os.makedirs(LOCAL_MODEL_DIR, exist_ok=True)


def notify(message):
    try:
        subprocess.run(["notify-send", "-t", "800", "Jarvis", message], check=False)
    except FileNotFoundError:
        pass


def play_sound(sound_type):
    sound_file = (
        f"{os.environ.get('HOME')}/.local/share/voice_assistant/mic_{sound_type}.wav"
    )
    if os.path.exists(sound_file):
        subprocess.Popen(["aplay", "-q", sound_file], stderr=subprocess.DEVNULL)


def type_text(text):
    """
    Direct, Wayland-safe injection.
    We drop delay to 1ms and hold to 1ms.
    This gives 500 characters per second: blazingly fast but still visually typed out character-by-character.
    """
    if not text:
        return
    try:
        subprocess.run(
            ["ydotool", "type", "-d", "1", "-H", "1", text + " "], check=True
        )
    except Exception as e:
        print(f"Error typing text: {e}")
        notify("❌ Error: ydotool failed. Is ydotoold running?")


class JarvisMonolith:
    def __init__(self):
        print(f"Loading {MODEL_SIZE} into RAM from {LOCAL_MODEL_DIR}...")
        self.model = WhisperModel(
            MODEL_SIZE,
            device=DEVICE,
            compute_type=COMPUTE_TYPE,
            cpu_threads=CPU_THREADS,
            download_root=LOCAL_MODEL_DIR,
        )
        print("Model loaded. Ready.")

        self.keyboards = self.find_keyboards()
        if not self.keyboards:
            print(
                "WARNING: No keyboards detected via evdev. Are you in the 'input' group?"
            )
            sys.exit(1)

        print("Listening on:")
        for kb in self.keyboards:
            print(f" - {kb.name} ({kb.path})")

        self.ctrl_held = False
        self.is_recording = False
        self.audio_buffer = []
        self.stream = None
        self.lock = threading.Lock()
        self.keep_running = True

    def find_keyboards(self):
        keyboards = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                caps = dev.capabilities()
                if ecodes.EV_KEY in caps and ecodes.KEY_SPACE in caps[ecodes.EV_KEY]:
                    keyboards.append(dev)
            except Exception:
                pass
        return keyboards

    def audio_callback(self, indata, frames, time, status):
        if status:
            print(status, file=sys.stderr)
        with self.lock:
            if self.is_recording:
                self.audio_buffer.append(indata.copy())

    def start_recording(self):
        with self.lock:
            if self.is_recording:
                return
            self.is_recording = True
            self.audio_buffer = []

        play_sound("on")
        notify("🔴 Listening...")
        print("\n[Start Recording]")

        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            callback=self.audio_callback,
        )
        self.stream.start()

    def stop_recording_and_transcribe(self):
        with self.lock:
            if not self.is_recording:
                return
            self.is_recording = False

        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        play_sound("off")
        notify("⏳ Processing...")
        print("[Stop Recording. Transcribing...]")

        with self.lock:
            if not self.audio_buffer:
                notify("❌ No audio.")
                return
            audio_data_int16 = np.concatenate(self.audio_buffer, axis=0).flatten()

        if len(audio_data_int16) < SAMPLE_RATE * 0.3:
            print("Audio too short, ignoring.")
            return

        audio_data_float32 = audio_data_int16.astype(np.float32) / 32768.0

        try:
            start_time = time.time()
            segments, _ = self.model.transcribe(audio_data_float32, beam_size=5)
            text = " ".join([s.text for s in segments]).strip()

            latency = (time.time() - start_time) * 1000
            print(f"Result ({latency:.0f}ms): {text}")

            if text:
                type_text(text)
                notify(f"✅ {text}")
            else:
                notify("⚠️ Could not hear anything.")
        except Exception as e:
            print(f"Transcription error: {e}")
            notify("❌ Failed.")

    def handle_event(self, event):
        if event.type == ecodes.EV_KEY:
            key_event = evdev.categorize(event)

            # Use strict string matching for the keycodes to avoid hasattr errors
            try:
                # Keycodes can be a single string or a list of strings
                keycodes = (
                    [key_event.keycode]
                    if isinstance(key_event.keycode, str)
                    else key_event.keycode
                )
            except AttributeError:
                return

            if any("CTRL" in k for k in keycodes):
                if key_event.keystate == key_event.key_down:
                    self.ctrl_held = True
                elif key_event.keystate == key_event.key_up:
                    self.ctrl_held = False
                    if self.is_recording:
                        threading.Thread(
                            target=self.stop_recording_and_transcribe
                        ).start()

            if any("KEY_SPACE" in k for k in keycodes):
                if key_event.keystate == key_event.key_down:
                    if self.ctrl_held and not self.is_recording:
                        self.start_recording()
                elif key_event.keystate == key_event.key_up:
                    if self.is_recording:
                        threading.Thread(
                            target=self.stop_recording_and_transcribe
                        ).start()

    def listen_loop(self, device):
        try:
            for event in device.read_loop():
                if not self.keep_running:
                    break
                self.handle_event(event)
        except Exception as e:
            print(f"Device {device.name} disconnected: {e}")

    def run(self):
        print("\nJarvis Active. Hold 'Ctrl + Space' on ANY keyboard to talk.")
        threads = []
        for kb in self.keyboards:
            t = threading.Thread(target=self.listen_loop, args=(kb,), daemon=True)
            t.start()
            threads.append(t)

        try:
            while self.keep_running:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down gracefully...")
            self.keep_running = False


if __name__ == "__main__":
    try:
        app = JarvisMonolith()
        app.run()
    except Exception as e:
        print(f"Fatal Error: {e}", file=sys.stderr)
        sys.exit(1)
