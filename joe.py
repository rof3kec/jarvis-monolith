#!/usr/bin/env python3
import sys
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
import os
import enum
import re
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
    from winrt.windows.ui.notifications import ToastNotificationManager
    HAS_TOAST = True
except ImportError:
    HAS_TOAST = False

# --- Configuration ---
MODEL_SIZE = os.environ.get("JOE_MODEL", "Systran/faster-whisper-base.en")
CPU_THREADS = int(os.environ.get("JOE_THREADS", 4))
SAMPLE_RATE = 16000
JOE_IMAGE = os.path.abspath("joe.png") if os.path.exists("joe.png") else None

# Wake word / silence detection
WAKE_BUFFER_SECONDS = 2
WAKE_CHECK_INTERVAL = 0.3
WAKE_RMS_THRESHOLD = int(os.environ.get("JOE_WAKE_RMS_THRESHOLD", 120))
SILENCE_THRESHOLD = int(os.environ.get("JOE_SILENCE_THRESHOLD", 250))
SILENCE_DURATION = 3.0
SILENCE_GRACE_PERIOD = 2.0

# Fuzzy regex for Whisper's various spellings of "Hi Joe" / "Hey John"
WAKE_PATTERN = re.compile(
    r"(hi|hey|high|aye)[\s,.\-!?]*(joe|john)",
    re.IGNORECASE,
)

# Fuzzy regex for Whisper's various spellings of "Fuck off"
STOP_PATTERN = re.compile(
    r"(f+u+c*k|frick|freak|fork|folk|luck|duck|buck|truck)[\s,.\-!?]*(off|of|up)?[\s,.\-!?]*",
    re.IGNORECASE,
)

# Keep models air-gapped inside this repo
LOCAL_MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
os.makedirs(LOCAL_MODEL_DIR, exist_ok=True)


class State(enum.Enum):
    SLEEPING = "SLEEPING"
    RECORDING = "RECORDING"
    TRANSCRIBING = "TRANSCRIBING"


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


def notify(message, use_image=False):
    """Show a Windows toast notification, fall back to console."""
    if HAS_TOAST:
        try:
            image_path = JOE_IMAGE if use_image else None
            win11_notify("Joe", message, image=image_path, app_id="Joe")
        except Exception:
            pass
    print(f"[Notify] {message}")


def clear_notifications():
    """Clear all Joe notifications from the action center."""
    if HAS_TOAST:
        try:
            ToastNotificationManager.history.clear_with_id("Joe")
        except Exception:
            pass


SOUND_DIR = os.path.join(os.path.dirname(__file__), "sound")
SOUND_START = os.path.join(SOUND_DIR, "start.mp3")
SOUND_STOP = os.path.join(SOUND_DIR, "stop.mp3")


def play_beep(kind):
    """Play start/stop MP3 sound via Windows MCI, fall back to winsound beep."""
    import ctypes

    sound_file = SOUND_START if kind == "on" else SOUND_STOP
    if os.path.isfile(sound_file):
        try:
            alias = f"joe_{kind}"
            mci = ctypes.windll.winmm.mciSendStringW
            mci(f"close {alias}", None, 0, 0)
            err = mci(f'open "{sound_file}" type mpegvideo alias {alias}', None, 0, 0)
            if err == 0:
                err = mci(f"play {alias}", None, 0, 0)
                if err == 0:
                    return
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


class JoeMonolith:
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

        self.state = State.SLEEPING
        self.lock = threading.Lock()
        self.keep_running = True

        # Circular wake buffer: fixed-size ring of int16 samples (mono)
        self._wake_buf_len = SAMPLE_RATE * WAKE_BUFFER_SECONDS
        self._wake_buf = np.zeros(self._wake_buf_len, dtype=np.int16)
        self._wake_buf_pos = 0

        # Track peak chunk-level RMS during wake listening
        self._wake_chunk_peak_rms = 0.0

        # Dictation buffer: list of int16 chunks, filled during RECORDING
        self._dict_buf = []

        # RMS of the most recent audio callback (used by silence monitor)
        self._last_rms = 0.0

        # Timestamp when silence was first detected during RECORDING
        self._silence_start = None

        # Timestamp when RECORDING began (for grace period)
        self._recording_start = 0.0

        # Open a single persistent mic stream
        print("Opening microphone...")
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                callback=self._audio_callback,
            )
            self.stream.start()
            print("Microphone open.")
        except Exception as e:
            print(f"Error opening microphone: {e}", file=sys.stderr)
            raise

    # ------------------------------------------------------------------
    # Audio callback (runs in sounddevice's C thread -- keep it fast)
    # ------------------------------------------------------------------
    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            print(status, file=sys.stderr)

        samples = indata[:, 0]  # mono int16

        if self.state == State.SLEEPING:
            # Write into circular wake buffer (no lock needed: single writer)
            n = len(samples)
            pos = self._wake_buf_pos
            space = self._wake_buf_len - pos
            if n <= space:
                self._wake_buf[pos : pos + n] = samples
            else:
                self._wake_buf[pos:] = samples[:space]
                self._wake_buf[: n - space] = samples[space:]
            self._wake_buf_pos = (pos + n) % self._wake_buf_len

            # Track per-chunk peak RMS for wake detection
            chunk_rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2))
            if chunk_rms > self._wake_chunk_peak_rms:
                self._wake_chunk_peak_rms = chunk_rms

        elif self.state == State.RECORDING:
            self._dict_buf.append(samples.copy())
            # Compute RMS for silence detection
            rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2))
            self._last_rms = rms

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def _transition_to(self, new_state):
        """Thread-safe state transition with side effects."""
        with self.lock:
            old = self.state
            # Validate transitions
            if old == State.SLEEPING and new_state != State.RECORDING:
                return False
            if old == State.RECORDING and new_state != State.TRANSCRIBING:
                return False
            if old == State.TRANSCRIBING and new_state != State.SLEEPING:
                return False
            self.state = new_state

        # Side effects (outside lock)
        if new_state == State.RECORDING:
            self._dict_buf = []
            self._last_rms = 0.0
            self._silence_start = None
            self._recording_start = time.time()
            play_beep("on")
            notify("joe is listening...", use_image=True)
            print("\n  Activated -- recording dictation")

        elif new_state == State.TRANSCRIBING:
            play_beep("off")
            notify("joe stopped listening...", use_image=True)
            print("  Stopped -- transcribing...")
            threading.Thread(
                target=self._transcribe_dictation, daemon=True
            ).start()

        elif new_state == State.SLEEPING:
            print("  Sleeping -- listening for wake word")

        return True

    # ------------------------------------------------------------------
    # Wake word listener (daemon thread)
    # ------------------------------------------------------------------
    def _wake_word_listener(self):
        while self.keep_running:
            time.sleep(WAKE_CHECK_INTERVAL)

            if self.state == State.SLEEPING:
                # Use peak chunk RMS (not whole-buffer average) for speech detection
                peak_rms = self._wake_chunk_peak_rms
                self._wake_chunk_peak_rms = 0.0  # reset for next interval

                print(f"  [wake] peak_RMS={peak_rms:.0f} (threshold={WAKE_RMS_THRESHOLD})")
                if peak_rms < WAKE_RMS_THRESHOLD:
                    continue

                # Grab a snapshot of the circular buffer
                pos = self._wake_buf_pos
                buf = np.empty(self._wake_buf_len, dtype=np.int16)
                buf[: self._wake_buf_len - pos] = self._wake_buf[pos:]
                buf[self._wake_buf_len - pos :] = self._wake_buf[:pos]

                # Transcribe the wake buffer (fast, beam_size=1)
                audio_f32 = buf.astype(np.float32) / 32768.0
                try:
                    segments, _ = self.model.transcribe(
                        audio_f32, beam_size=1, language="en"
                    )
                    text = " ".join(s.text for s in segments).strip()
                except Exception as e:
                    print(f"  Wake transcription error: {e}")
                    continue

                if text:
                    print(f'  [wake] Heard: "{text}"')

                if text and WAKE_PATTERN.search(text):
                    print(f'  Wake word detected: "{text}"')
                    # Clear the wake buffer so we don't re-trigger
                    self._wake_buf[:] = 0
                    self._wake_buf_pos = 0
                    self._transition_to(State.RECORDING)

            elif self.state == State.RECORDING:
                # Check for stop word
                dict_buf_copy = list(self._dict_buf)
                if not dict_buf_copy:
                    continue

                chunks_to_check = []
                samples_collected = 0
                for chunk in reversed(dict_buf_copy):
                    chunks_to_check.append(chunk)
                    samples_collected += len(chunk)
                    if samples_collected >= SAMPLE_RATE * WAKE_BUFFER_SECONDS:
                        break

                buf = np.concatenate(chunks_to_check[::-1])
                if len(buf) > SAMPLE_RATE * WAKE_BUFFER_SECONDS:
                    buf = buf[-SAMPLE_RATE * WAKE_BUFFER_SECONDS:]

                audio_f32 = buf.astype(np.float32) / 32768.0
                try:
                    segments, _ = self.model.transcribe(
                        audio_f32, beam_size=1, language="en"
                    )
                    text = " ".join(s.text for s in segments).strip()
                except Exception as e:
                    print(f"  Stop word transcription error: {e}")
                    continue

                if text and STOP_PATTERN.search(text):
                    print(f'  Stop word detected: "{text}"')
                    self._transition_to(State.TRANSCRIBING)

    # ------------------------------------------------------------------
    # Silence monitor (daemon thread)
    # ------------------------------------------------------------------
    def _silence_monitor(self):
        while self.keep_running:
            time.sleep(0.1)

            if self.state != State.RECORDING:
                self._silence_start = None
                continue

            # Skip during grace period (let user start speaking)
            elapsed = time.time() - self._recording_start
            if elapsed < SILENCE_GRACE_PERIOD:
                continue

            if self._last_rms < SILENCE_THRESHOLD:
                if self._silence_start is None:
                    self._silence_start = time.time()
                elif time.time() - self._silence_start >= SILENCE_DURATION:
                    print("  Silence detected -- auto-stopping")
                    self._transition_to(State.TRANSCRIBING)
            else:
                self._silence_start = None

    # ------------------------------------------------------------------
    # Ctrl+T handler
    # ------------------------------------------------------------------
    def _on_ctrl_t(self):
        if self.state == State.SLEEPING:
            self._transition_to(State.RECORDING)
        elif self.state == State.RECORDING:
            self._transition_to(State.TRANSCRIBING)

    # ------------------------------------------------------------------
    # Transcription
    # ------------------------------------------------------------------
    def _transcribe_dictation(self):
        if not self._dict_buf:
            notify("No audio captured.")
            self._transition_to(State.SLEEPING)
            return

        audio_int16 = np.concatenate(self._dict_buf).flatten()
        self._dict_buf = []  # free memory

        if len(audio_int16) < SAMPLE_RATE * 0.3:
            print("  Audio too short, ignoring.")
            self._transition_to(State.SLEEPING)
            return

        audio_f32 = audio_int16.astype(np.float32) / 32768.0

        try:
            start_time = time.time()
            segments, _ = self.model.transcribe(audio_f32, beam_size=5)
            text = " ".join(s.text for s in segments).strip()

            # Strip the wake word from the beginning of the transcription
            text = re.sub(r"^(hi|hey|high|aye)[\s,.\-!?]*(joe|john)[\s,.\-!?]*", "", text, flags=re.IGNORECASE).strip()
            # Clean up leftover leading punctuation or whitespace
            text = re.sub(r"^[\s,.\-!?]+", "", text).strip()

            # Strip the stop word from the transcription (anywhere it appears)
            text = STOP_PATTERN.sub("", text).strip()

            latency = (time.time() - start_time) * 1000
            print(f"  Result ({latency:.0f}ms): {text}")

            if text:
                type_text(text)
                time.sleep(0.05)
                pyautogui.press("enter")
        except Exception as e:
            print(f"  Transcription error: {e}")
            notify("Transcription failed.")

        self._transition_to(State.SLEEPING)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        print("\n  Sleeping -- listening for wake word")
        print('  Say "Hi/Hey/Aye Joe/John" to activate, "Stop Joe/John" to stop, Ctrl+T to toggle, Ctrl+C to exit.\n')

        # Start daemon threads
        threading.Thread(
            target=self._wake_word_listener, daemon=True
        ).start()
        threading.Thread(
            target=self._silence_monitor, daemon=True
        ).start()

        # Register Ctrl+T as manual stop (press only)
        keyboard.add_hotkey("ctrl+t", self._on_ctrl_t, suppress=True)

        try:
            while self.keep_running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nShutting down gracefully...")
            self.keep_running = False
        finally:
            keyboard.unhook_all()
            if hasattr(self, "stream") and self.stream:
                self.stream.stop()
                self.stream.close()


def main():
    print("Starting Joe...")
    try:
        app = JoeMonolith()
        app.run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        input("Press Enter to exit...")
        sys.exit(1)

if __name__ == "__main__":
    main()
