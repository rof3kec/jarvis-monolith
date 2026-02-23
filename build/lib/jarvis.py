#!/usr/bin/env python3
import sys
import os
import threading
import time
import winsound
import sounddevice as sd
import numpy as np
from faster_whisper import WhisperModel
import keyboard
import pyperclip
import pyautogui

try:
    from win11toast import notify as win11_notify

    HAS_TOAST = True
except ImportError:
    HAS_TOAST = False

# --- Configuration ---
MODEL_SIZE = os.environ.get("JARVIS_MODEL", "Systran/faster-whisper-base.en")
CPU_THREADS = int(os.environ.get("JARVIS_THREADS", 4))
SAMPLE_RATE = 16000

# Keep models air-gapped inside this repo
LOCAL_MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
os.makedirs(LOCAL_MODEL_DIR, exist_ok=True)


def detect_device():
    """Detect CUDA GPU availability, fall back to CPU."""
    try:
        import torch

        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            print(f"CUDA GPU detected: {name}")
            return "cuda", "float16"
    except ImportError:
        pass
    print("No CUDA GPU detected, using CPU with int8 quantization.")
    return "cpu", "int8"


def notify(message):
    """Show a Windows toast notification, fall back to console."""
    if HAS_TOAST:
        try:
            win11_notify("Jarvis", message, duration="short")
        except Exception:
            pass
    print(f"[Notify] {message}")


def play_beep(kind):
    """Play a beep sound using winsound (no wav files needed)."""
    try:
        if kind == "on":
            winsound.Beep(800, 150)
        elif kind == "off":
            winsound.Beep(400, 150)
    except Exception:
        pass


def type_text(text):
    """Inject text into the active window via clipboard paste."""
    if not text:
        return
    try:
        pyperclip.copy(text + " ")
        time.sleep(0.05)
        pyautogui.hotkey("ctrl", "v")
    except Exception as e:
        print(f"Error typing text: {e}")
        notify("Error: Could not paste text.")


class JarvisMonolith:
    def __init__(self):
        device, compute_type = detect_device()

        print(f"Loading {MODEL_SIZE} (device={device}, compute={compute_type})...")
        model_kwargs = {
            "device": device,
            "compute_type": compute_type,
            "download_root": LOCAL_MODEL_DIR,
        }
        if device == "cpu":
            model_kwargs["cpu_threads"] = CPU_THREADS

        self.model = WhisperModel(MODEL_SIZE, **model_kwargs)
        print("Model loaded. Ready.")

        self.is_recording = False
        self.audio_buffer = []
        self.stream = None
        self.lock = threading.Lock()
        self.keep_running = True

    def audio_callback(self, indata, frames, time_info, status):
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

        play_beep("on")
        notify("Listening...")
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

        play_beep("off")
        notify("Processing...")
        print("[Stop Recording. Transcribing...]")

        with self.lock:
            if not self.audio_buffer:
                notify("No audio captured.")
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
                notify(f"Typed: {text}")
            else:
                notify("Could not hear anything.")
        except Exception as e:
            print(f"Transcription error: {e}")
            notify("Transcription failed.")

    def on_hotkey_press(self):
        """Called when Ctrl+T is pressed down."""
        if not self.is_recording:
            self.start_recording()

    def on_hotkey_release(self, event):
        """Called when either Ctrl or T is released while recording."""
        if self.is_recording:
            threading.Thread(target=self.stop_recording_and_transcribe).start()

    def run(self):
        print("\nJarvis Active. Hold Ctrl+T to talk, release to transcribe.")

        # Register the hotkey press handler
        keyboard.add_hotkey("ctrl+t", self.on_hotkey_press, suppress=True)

        # Watch for key-up on ctrl or t to stop recording
        keyboard.on_release_key("t", self.on_hotkey_release, suppress=False)
        keyboard.on_release_key("ctrl", self.on_hotkey_release, suppress=False)

        try:
            while self.keep_running:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down gracefully...")
            self.keep_running = False
        finally:
            keyboard.unhook_all()


def main():
    try:
        app = JarvisMonolith()
        app.run()
    except Exception as e:
        print(f"Fatal Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
