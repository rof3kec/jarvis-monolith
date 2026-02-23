# Joe Monolith - Agent Instructions

Welcome to the Joe Monolith codebase. This document outlines the commands, architecture, conventions, and "gotchas" necessary for an AI agent to work effectively in this repository.

## Project Overview
Joe Monolith is a zero-latency, single-file voice dictation tool designed specifically for Windows 11. It uses `faster-whisper` (CTranslate2) for transcription, holding the model in RAM. Audio is recorded directly to memory and transcribed text is injected into the active window via clipboard simulation.

The primary design philosophy is **Monolithic and Air-gapped**: No IPC, no files written to disk for audio, and the entire repository (including models) can be backed up and run offline.

## Essential Commands

- **Install dependencies:** `uv sync` (or run `install.bat`)
- **Run the application:** `uv run python joe.py`
- **Global installation (uv tool):** The project is configured as a CLI tool (`joe`) in `pyproject.toml` and can be installed globally via `uv tool install .`.

## Code Organization & Architecture

- **`joe.py`**: The entire application logic lives here.
  - **Recording**: Uses `sounddevice` to capture microphone input directly into a `numpy` array.
  - **Hotkeys**: Uses the `keyboard` library to hook `Ctrl+T` globally via the Win32 API.
  - **Inference**: Uses `faster-whisper`. Automatically detects CUDA GPU (falls back to CPU).
  - **Output**: Copies transcribed text to the clipboard using `pyperclip` and simulates a `Ctrl+V` keystroke using `pyautogui`.
  - **Feedback**: Uses `win11toast` for visual notifications and `winsound.Beep` for audio cues.
- **`models/`**: Automatically created on first run. Used to store downloaded Whisper models locally to ensure the repository remains portable and air-gapped.
- **`pyproject.toml`**: Dependency management via `uv`. Note the explicit source configuration for `pytorch-cu124` to ensure CUDA support.

## Conventions and Patterns

1. **Keep it Monolithic**: Avoid splitting `joe.py` into multiple files or introducing complex IPC mechanisms unless explicitly requested. The simplicity of a single file is intentional.
2. **RAM-Only Audio**: Never write temporary `.wav` files to disk. All audio processing must happen in memory (`float32` arrays) to ensure zero latency.
3. **Local Models**: Always initialize the `WhisperModel` with `download_root` set to the local `models/` directory.
4. **Windows-Native API usage**: The code relies on Windows-specific libraries (`winsound`, `win11toast`). Do not try to make these cross-platform unless requested.

## Gotchas and Troubleshooting

- **Administrator Privileges**: The `keyboard` library requires low-level Win32 hooks to capture hotkeys when other applications are focused. If hotkeys fail to register, the script likely needs to be run as Administrator.
- **CUDA Installation**: CUDA acceleration is supported and detected automatically. If adding or updating dependencies, preserve the `tool.uv.sources` configuration in `pyproject.toml` that pulls PyTorch from the `cu124` index.
- **Graceful Shutdown**: The script uses a `keep_running` flag and intercepts `KeyboardInterrupt` to unhook keys properly using `keyboard.unhook_all()`. Always ensure hooks are released to avoid system-wide keyboard glitches.
- **Testing**: There is no automated test suite in this repository. Changes must be validated manually by running `joe.py` and interacting with the `Ctrl+T` hotkey.
