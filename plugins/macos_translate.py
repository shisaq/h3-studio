"""Chinese -> English with the translation built into macOS. Runs on this machine; nothing is sent anywhere.

Needs macOS 15 or later, the helper built by scripts/build-translate.sh, and the Chinese and English
language packs (System Settings > General > Language & Region > Translation Languages).
"""
import json
import subprocess
import time

_cache = {"at": 0.0, "ok": False}


def register(api):
    helper = api.root / "bin/h3-translate"

    def available():
        if not helper.exists():
            return False
        if time.time() - _cache["at"] > 60:    # the web page polls status every few seconds; do not spawn each time
            try:
                r = subprocess.run([str(helper), "--status", "zh-Hans", "en"], capture_output=True, text=True, timeout=20)
                _cache["ok"] = r.stdout.strip() == "installed"
            except Exception:
                _cache["ok"] = False
            _cache["at"] = time.time()
        return _cache["ok"]

    def translate(texts):
        r = subprocess.run([str(helper), "zh-Hans", "en"], input=json.dumps(texts), capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-200:] or "translation helper failed")
        return json.loads(r.stdout)

    api.register_translator(translate, available=available, local=True, label=api.L("本机翻译", "On-device (macOS)"))
