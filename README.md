# Joe Monolith

A zero-latency voice dictation tool for Windows 11. Holds `faster-whisper` (CTranslate2) in RAM, records from your microphone to a memory array, and pastes transcribed text via clipboard. No files on disk. No IPC.

Supports NVIDIA CUDA for GPU-accelerated transcription with automatic CPU fallback.

## Installation

1. **Install dependencies:**
   ```
   install.bat
   ```
   Or manually:
   ```
   uv sync
   ```

2. **CUDA (optional):** For GPU acceleration, install PyTorch with CUDA:
   ```
   pip install torch --index-url https://download.pytorch.org/whl/cu121
   ```

## Usage

```
uv run python joe.py
```

Hold **Ctrl+T** to record, release to transcribe and paste into the active window.

The tool works across all applications including terminals, editors, and browsers.

## Architecture

```text
========================================================================
                        JOE MONOLITH ARCHITECTURE
========================================================================

[ Physical Keyboard ]
       | (Win32 Hook: Ctrl+T)
       v
+----------------------------------------------------------------------+
|                           joe.py (Python)                            |
|                                                                      |
|  1. [keyboard lib]      <-- Global hotkey hook via Win32 API         |
|           |                                                          |
|           v (Trigger)                                                |
|  2. [sounddevice]       <-- Records mic directly to RAM (NumPy)     |
|           |                                                          |
|           v (Float32 Array)                                          |
|  3. [faster-whisper]    <-- CTranslate2 Engine (CUDA or CPU)        |
|           |                                                          |
|           v (Text string)                                            |
|  4. [pyperclip+paste]   <-- Clipboard copy + Ctrl+V paste           |
+----------------------------------------------------------------------+
       | (Ctrl+V simulated keystroke)
       v
[ Active Window ]
```

## Configuration

Environment variables:
- `JOE_MODEL` - Whisper model to use (default: `Systran/faster-whisper-base.en`)
- `JOE_THREADS` - CPU threads for inference (default: `4`, only used in CPU mode)

## Notes

- Run as Administrator if the `keyboard` library cannot capture global hotkeys.
- Models are downloaded to the `models/` directory on first run.
- The repo is self-contained: back up the entire folder to run it air-gapped on another machine.
