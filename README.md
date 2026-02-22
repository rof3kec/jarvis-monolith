# Jarvis Monolith

A zero-latency, CPU-only voice dictation tool designed specifically for Linux (Wayland/X11). 
It holds `faster-whisper` (CTranslate2) in RAM, records straight from your microphone to a memory array, and uses `ydotool` to inject the text instantly. No files on disk. No IPC.

## Installation

1. **Permissions:** Ensure your user is in the `input` group to read keyboard events:
   ```bash
   sudo usermod -aG input $USER
   ```
2. **Setup Environment:** Use `uv` to pull dependencies safely:
   ```bash
   uv sync
   ```
3. **Daemonize `ydotool`:** (Required for pasting text into Wayland windows):
   ```bash
   sudo pacman -S ydotool
   sudo systemctl enable --now ydotoold
   ```
4. **Auto-Start (Optional):**
   ```bash
   cp systemd/jarvis.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now jarvis.service
   ```

## Usage
Simply hold **`Ctrl + Space`** on any keyboard, speak, and release to type.

## Architecture & Replicability
This repo is entirely self-contained. The AI models are saved directly into the `models/` directory, so you can back up this entire folder to an external drive and run it air-gapped on any Linux machine.

## Architecture

```text
========================================================================
                      JARVIS MONOLITH ARCHITECTURE                      
========================================================================

[ Physical Keyboard(s) ]
       | (Hardware Events: Ctrl+Space)
       v
+----------------------------------------------------------------------+
|                         jarvis.py (Python)                           |
|                                                                      |
|  1. [evdev Listener]  <-- Detects hotkey across ALL connected boards |
|           |                                                          |
|           v (Trigger)                                                |
|  2. [sounddevice]     <-- Records Mic directly to RAM (NumPy array)  |
|           |                                                          |
|           v (Float32 Array)                                          |
|  3. [faster-whisper]  <-- CTranslate2 Engine (base.en in RAM)        |
|           |               (Capped at 4 threads, Zero Disk I/O)       |
|           v (Text string)                                            |
|  4. [ydotool]         <-- Spawns subprocess to type text directly    |
+----------------------------------------------------------------------+
       | (CLI Command: ydotool type -d 1 -H 1 "text")
       v
[ Virtual Keyboard (ydotoold) ]
       |
       v
[ Active Wayland/X11 Window ]
```
