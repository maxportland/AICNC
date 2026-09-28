# Wake Word Detection Setup for "Hey Milo"

Wake word detection runs fully offline with [Vosk](https://alphacephei.com/vosk/).
No account, API key, or internet connection is needed.

## Installation

```bash
# In the config's venv
venv/bin/pip install vosk sounddevice

# Download the small English model (~40 MB)
cd ~
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip vosk-model-small-en-us-0.15.zip
```

The detector looks for the model in the config directory first, then in
`~/vosk-model-small-en-us-0.15`.

Restart LinuxCNC. The AI Assistant log should show:

```
[WAKE] Vosk initialized with wake phrase 'Hey Milo' (...)
[WAKE] Listening for 'Hey Milo'...
```

## Using it

Say "Hey Milo" or "Hi Milo". The UI switches to the AI Assistant tab and starts
recording. You can keep talking straight after the wake word
("Hey Milo, move X ten millimeters"). Detection happens while you are still
speaking, so the command isn't lost.

## How it works

`wake_word_detector.py` runs Vosk with a small grammar instead of full speech
recognition:

- the wake word `milo`, the greetings `hey`/`hi`, and ~130 everyday "decoy"
  words (`DECOY_WORDS`) that sound-alikes can land on instead
  ("hey Miles", "hey mind the tool", "hey mike")
- `[unk]` for everything else

A detection needs "hey milo" or "hi milo" to hold in the partial result for 3
consecutive 100 ms blocks, or to appear in a final result. After a hit the
recognizer resets, and there is a 2 second cooldown.

Measured on the Raspberry Pi 5 with synthesized speech: all wake phrases were
detected, including accents and faster speech, with no false triggers on
sound-alikes except "hey my low", which sounds the same. CPU use is about 3-6%
of one core. The previous full-vocabulary approach used about 20% and often
heard "Hey Milo" as "hey my low".

## Troubleshooting

### "Vosk is not installed"
`venv/bin/pip install vosk`

### "Vosk model not found"
Download and extract the model as shown above.

### Missed detections
- Check the microphone level meter while recording
- Speak "Hey Milo" as two clear words; very clipped speech can be missed

### False triggers
- If a phrase you use often triggers it, add its words to `DECOY_WORDS`
- A false trigger only starts a recording. Machine actions still need
  confirmation before they run.
