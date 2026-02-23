#!/usr/bin/env python3
import sys
import os

# Suppress any console window that native libraries (portaudio, ctranslate2, etc.)
# might allocate. Must run before importing those libraries.
if sys.platform == "win32":
    import ctypes
    # If we somehow have a console (e.g. launched via python.exe), free it.
    ctypes.windll.kernel32.FreeConsole()
    # After freeing the console, redirect both Python-level and C-level
    # stdout/stderr to devnull so libraries (transformers, tqdm, etc.)
    # don't crash on closed handles (errno 9 EBADF).
    _devnull = open(os.devnull, "w")
    sys.stdout = _devnull
    sys.stderr = _devnull
    _devnull_fd = _devnull.fileno()
    os.dup2(_devnull_fd, 1)
    os.dup2(_devnull_fd, 2)
    # Patch subprocess so any child processes we spawn also get no console.
    import subprocess as _subprocess
    _orig_popen_init = _subprocess.Popen.__init__
    def _popen_no_window(self, *args, **kwargs):
        kwargs.setdefault("creationflags", 0)
        kwargs["creationflags"] |= 0x08000000  # CREATE_NO_WINDOW
        _orig_popen_init(self, *args, **kwargs)
    _subprocess.Popen.__init__ = _popen_no_window

import enum
import re
import threading
import time
import sounddevice as sd
import numpy as np
from faster_whisper import WhisperModel
import keyboard
import pyperclip
import pyautogui
import pystray
from PIL import Image, ImageDraw

try:
    from win11toast import notify as win11_notify
    from winrt.windows.ui.notifications import ToastNotificationManager
    HAS_TOAST = True
except ImportError:
    HAS_TOAST = False

# --- Configuration ---
MODEL_SIZE = os.environ.get("JOE_MODEL", "Systran/faster-whisper-base.en")
SR_MODEL_ID = os.environ.get("JOE_SR_MODEL", "Sagicc/whisper-tiny-sr")
CPU_THREADS = int(os.environ.get("JOE_THREADS", 4))
SAMPLE_RATE = 16000
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JOE_IMAGE = os.path.join(_SCRIPT_DIR, "joe.png") if os.path.exists(os.path.join(_SCRIPT_DIR, "joe.png")) else None
PREFS_FILE = os.path.join(_SCRIPT_DIR, "joe_prefs.txt")

# Wake word / silence detection
WAKE_BUFFER_SECONDS = 2
WAKE_CHECK_INTERVAL = 0.3
WAKE_RMS_THRESHOLD = int(os.environ.get("JOE_WAKE_RMS_THRESHOLD", 120))
SILENCE_THRESHOLD = int(os.environ.get("JOE_SILENCE_THRESHOLD", 250))
SILENCE_DURATION = float(os.environ.get("JOE_SILENCE_DURATION", 4.0))
SILENCE_GRACE_PERIOD = float(os.environ.get("JOE_SILENCE_GRACE", 3.0))
SILENCE_MIN_RECORDING = float(os.environ.get("JOE_MIN_RECORDING", 1.5))

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
LOCAL_MODEL_DIR = os.path.join(_SCRIPT_DIR, "models")
os.makedirs(LOCAL_MODEL_DIR, exist_ok=True)


class State(enum.Enum):
    SLEEPING = "SLEEPING"
    RECORDING = "RECORDING"
    TRANSCRIBING = "TRANSCRIBING"


class Language(enum.Enum):
    ENGLISH = "en"
    SERBIAN = "sr"


def load_language_pref():
    try:
        with open(PREFS_FILE, "r") as f:
            return Language[f.read().strip()]
    except Exception:
        return Language.ENGLISH


def save_language_pref(lang):
    try:
        with open(PREFS_FILE, "w") as f:
            f.write(lang.name)
    except Exception:
        pass


def load_sr_model(device):
    """Load Serbian Whisper model directly (no pipeline, avoids tqdm/stderr issues)."""
    try:
        import warnings
        import logging
        warnings.filterwarnings("ignore")
        logging.getLogger("transformers").setLevel(logging.ERROR)
        logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
        os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] = "1"
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        os.environ["TQDM_DISABLE"] = "1"

        from transformers import WhisperForConditionalGeneration, WhisperProcessor
        import torch

        cache_dir = os.path.join(LOCAL_MODEL_DIR, "sr")
        torch_device = "cuda" if device == "cuda" else "cpu"

        # transformers has a built-in disable_progress_bar() that makes its
        # internal tqdm() calls return a no-op EmptyTqdm — use that.
        from transformers.utils import logging as _hf_logging
        _hf_logging.disable_progress_bar()

        processor = WhisperProcessor.from_pretrained(SR_MODEL_ID, cache_dir=cache_dir)
        model = WhisperForConditionalGeneration.from_pretrained(
            SR_MODEL_ID, cache_dir=cache_dir
        )
        model.to(torch_device)
        model.eval()

        return {"model": model, "processor": processor, "device": torch_device}
    except Exception as e:
        notify(f"Serbian model failed: {e}")
        return None


def detect_device():
    """Detect CUDA GPU availability, fall back to CPU."""
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda", "float16"
    except ImportError:
        pass
    return "cpu", "int8"


def notify(message, use_image=False):
    """Show a Windows toast notification."""
    if HAS_TOAST:
        try:
            image_path = JOE_IMAGE if use_image else None
            win11_notify("Joe", message, image=image_path, app_id="Joe")
        except Exception:
            pass


def clear_notifications():
    """Clear all Joe notifications from the action center."""
    if HAS_TOAST:
        try:
            ToastNotificationManager.history.clear_with_id("Joe")
        except Exception:
            pass


SOUND_DIR = os.path.join(_SCRIPT_DIR, "sound")
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
    except Exception:
        notify("Error: Could not paste text.")


class JoeMonolith:
    def __init__(self):
        device, compute_type = detect_device()
        self._device = device

        model_kwargs = {
            "device": device,
            "compute_type": compute_type,
            "download_root": os.path.join(LOCAL_MODEL_DIR, "en"),
        }
        if device == "cpu":
            model_kwargs["cpu_threads"] = CPU_THREADS

        self.model = WhisperModel(MODEL_SIZE, **model_kwargs)
        self.sr_model = None  # loaded on demand when Serbian is first used

        self.state = State.SLEEPING
        self.language = load_language_pref()
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
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                callback=self._audio_callback,
            )
            self.stream.start()
            notify("Joe woke up!", use_image=True)
        except Exception as e:
            raise

    # ------------------------------------------------------------------
    # Audio callback (runs in sounddevice's C thread -- keep it fast)
    # ------------------------------------------------------------------
    def _audio_callback(self, indata, frames, time_info, status):
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
    def _transition_to(self, new_state, language=None):
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
            if language is not None:
                self.language = language

        # Side effects (outside lock)
        if new_state == State.RECORDING:
            lang_label = "Serbian" if self.language == Language.SERBIAN else "English"
            self._dict_buf = []
            self._last_rms = 0.0
            self._silence_start = None
            self._recording_start = time.time()
            play_beep("on")
            notify(f"joe is listening... [{lang_label}]", use_image=True)

        elif new_state == State.TRANSCRIBING:
            play_beep("off")
            notify("joe stopped listening...", use_image=True)
            threading.Thread(
                target=self._transcribe_dictation, daemon=True
            ).start()

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
                except Exception:
                    continue

                if text and WAKE_PATTERN.search(text):
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
                    # Always use English for stop-word detection regardless of dictation language
                    segments, _ = self.model.transcribe(
                        audio_f32, beam_size=1, language="en"
                    )
                    text = " ".join(s.text for s in segments).strip()
                except Exception:
                    continue

                if text and STOP_PATTERN.search(text):
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

            elapsed = time.time() - self._recording_start

            # Skip during grace period (let user start speaking)
            if elapsed < SILENCE_GRACE_PERIOD:
                continue

            # Don't cut off if we haven't captured enough audio yet
            recorded_samples = sum(len(c) for c in self._dict_buf)
            if recorded_samples < SAMPLE_RATE * SILENCE_MIN_RECORDING:
                continue

            if self._last_rms < SILENCE_THRESHOLD:
                if self._silence_start is None:
                    self._silence_start = time.time()
                elif time.time() - self._silence_start >= SILENCE_DURATION:
                    self._transition_to(State.TRANSCRIBING)
            else:
                self._silence_start = None

    # ------------------------------------------------------------------
    # Hotkey handlers
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
            self._transition_to(State.SLEEPING)
            return

        audio_f32 = audio_int16.astype(np.float32) / 32768.0

        # Snapshot language so it can't change mid-transcription
        lang = self.language

        try:
            if lang == Language.SERBIAN:
                if self.sr_model is None:
                    notify("Loading Serbian model...")
                    self.sr_model = load_sr_model(self._device)
                if self.sr_model is None:
                    notify("Serbian model failed to load.")
                    self._transition_to(State.SLEEPING)
                    return
                import torch
                sr = self.sr_model
                inputs = sr["processor"](
                    audio_f32, sampling_rate=SAMPLE_RATE, return_tensors="pt"
                )
                input_features = inputs.input_features.to(sr["device"])
                with torch.no_grad():
                    predicted_ids = sr["model"].generate(
                        input_features,
                        language="serbian",
                        task="transcribe",
                    )
                text = sr["processor"].batch_decode(
                    predicted_ids, skip_special_tokens=True
                )[0].strip()
            else:
                segments, _ = self.model.transcribe(audio_f32, beam_size=5)
                text = " ".join(s.text for s in segments).strip()

                # Strip the wake word from the beginning of the transcription
                text = re.sub(r"^(hi|hey|high|aye)[\s,.\-!?]*(joe|john)[\s,.\-!?]*", "", text, flags=re.IGNORECASE).strip()
                # Clean up leftover leading punctuation or whitespace
                text = re.sub(r"^[\s,.\-!?]+", "", text).strip()

                # Strip the stop word from the transcription (anywhere it appears)
                text = STOP_PATTERN.sub("", text).strip()

            if text:
                type_text(text)
                time.sleep(0.05)
                pyautogui.press("enter")
        except Exception as e:
            notify(f"Transcription failed: {e}")

        self._transition_to(State.SLEEPING)

    # ------------------------------------------------------------------
    # System tray icon
    # ------------------------------------------------------------------
    def _make_tray_image(self):
        """Create a small circular tray icon (green dot on dark background)."""
        if JOE_IMAGE and os.path.exists(JOE_IMAGE):
            img = Image.open(JOE_IMAGE).convert("RGBA")
            img = img.resize((64, 64), Image.LANCZOS)
            return img
        # Fallback: draw a simple green circle
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((4, 4, size - 4, size - 4), fill=(50, 200, 80, 255))
        return img

    def _set_language_english(self, icon, item):
        self.language = Language.ENGLISH
        save_language_pref(Language.ENGLISH)
        notify("Language: English")

    def _set_language_serbian(self, icon, item):
        self.language = Language.SERBIAN
        save_language_pref(Language.SERBIAN)
        if self.sr_model is None:
            notify("Loading Serbian model...")
            self.sr_model = load_sr_model(self._device)
        notify("Language: Serbian")

    def _is_english(self, item):
        return self.language == Language.ENGLISH

    def _is_serbian(self, item):
        return self.language == Language.SERBIAN

    def _quit_joe(self, icon, item):
        self.keep_running = False
        icon.stop()

    def _setup_tray(self):
        image = self._make_tray_image()
        menu = pystray.Menu(
            pystray.MenuItem("Joe (voice dictation)", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Language",
                pystray.Menu(
                    pystray.MenuItem(
                        "English",
                        self._set_language_english,
                        checked=self._is_english,
                        radio=True,
                    ),
                    pystray.MenuItem(
                        "Serbian",
                        self._set_language_serbian,
                        checked=self._is_serbian,
                        radio=True,
                    ),
                ),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit_joe),
        )
        self._tray = pystray.Icon("Joe", image, "Joe – voice dictation", menu)
        self._tray.run()  # blocks until icon.stop() is called

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        # Start daemon threads
        threading.Thread(
            target=self._wake_word_listener, daemon=True
        ).start()
        threading.Thread(
            target=self._silence_monitor, daemon=True
        ).start()

        # Register hotkeys
        keyboard.add_hotkey("ctrl+t", self._on_ctrl_t, suppress=True)

        # Run the main loop in a background thread so the tray can own the
        # main thread (pystray on Windows requires this).
        def _main_loop():
            try:
                while self.keep_running:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                self.keep_running = False
            finally:
                keyboard.unhook_all()
                if hasattr(self, "stream") and self.stream:
                    self.stream.stop()
                    self.stream.close()
                if hasattr(self, "_tray"):
                    self._tray.stop()

        threading.Thread(target=_main_loop, daemon=True).start()

        # Tray blocks the main thread until Quit is chosen
        self._setup_tray()

        # After tray exits, ensure cleanup
        self.keep_running = False
        keyboard.unhook_all()
        if hasattr(self, "stream") and self.stream:
            self.stream.stop()
            self.stream.close()


def main():
    if sys.platform == "win32":
        import ctypes
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\JoeVoiceDictation")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            sys.exit(0)

    try:
        app = JoeMonolith()
        app.run()
    except Exception:
        import traceback
        traceback.print_exc()
        input("Press Enter to exit...")
        sys.exit(1)

if __name__ == "__main__":
    main()
