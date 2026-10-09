"""Send finished videos to a chat through Hermes Agent (`hermes send`), and let Hermes drive h3 from chat.

Targets are written "platform" or "profile/platform", optionally followed by ":chat_id[:thread_id]".
When h3ctl is run by Hermes (see plugins/hermes/skill), the result goes back to the conversation that
asked for it: h3ctl forwards the HERMES_* variables Hermes sets for the commands it runs.

Videos for Feishu / Lark are sent through the open API directly, because the Hermes Feishu adapter uploads
video without duration or cover, and the preview then shows a black frame and 0:00.

plugin_config:
  "hermes_notify": {
    "hermes_bin": "",                       # default: ~/.hermes/hermes-agent/venv/bin/hermes
    "hermes_home": "~/.hermes",
    "targets": {"default": "weixin"},       # Hermes profile -> platform that profile delivers to
    "proxy": "", "no_proxy": ""             # network settings for `hermes send`, if it needs a proxy
  }
"""
import json
import re
import subprocess
import urllib.request
import uuid
from pathlib import Path

ENV_KEYS = ("HERMES_HOME", "HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_THREAD_ID")


def split_target(target):
    """"profile/platform:chat:thread" -> (profile, platform, chat, thread). No profile means "default"."""
    profile, _, rest = target.rpartition("/")
    if not re.fullmatch(r"[\w-]+", profile or "default"):
        profile, rest = "default", target
    bits = rest.split(":")
    return profile or "default", bits[0], (bits[1] if len(bits) > 1 else ""), (bits[2] if len(bits) > 2 else "")


def register(api):
    cfg = api.config
    home = api.expand(cfg.get("hermes_home") or "~/.hermes")
    hermes = api.expand(cfg.get("hermes_bin") or home / "hermes-agent/venv/bin/hermes")
    profiles = cfg.get("targets") or {"default": "weixin"}

    def profile_home(profile):
        return home if profile in ("", "default") else home / "profiles" / profile

    def default_target(profile):
        plat = profiles.get(profile)
        return (plat if profile == "default" else f"{profile}/{plat}") if plat else None

    def targets():
        return [{"target": default_target(p), "label": f"{plat} ({p})" if p != "default" else plat} for p, plat in profiles.items()]

    def resolve(req):
        env = req.get("client_env") or {}
        if not env.get("HERMES_HOME"):
            return None                         # not called by Hermes
        hh = Path(env["HERMES_HOME"])
        profile = hh.name if hh.parent.name == "profiles" else "default"
        plat, chat, thread = (env.get("HERMES_SESSION_" + k, "") for k in ("PLATFORM", "CHAT_ID", "THREAD_ID"))
        if plat and chat:                       # back to the conversation that asked
            return ("" if profile == "default" else profile + "/") + f"{plat}:{chat}" + (f":{thread}" if thread else "")
        return default_target(profile if profile in profiles else "default")

    def send_env(profile):
        env = api.clean_env()
        if cfg.get("proxy"):
            for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
                env[k] = cfg["proxy"]
            env["NO_PROXY"] = env["no_proxy"] = cfg.get("no_proxy") or "localhost,127.0.0.1,::1"
        else:
            env.pop("NO_PROXY", None)
            env.pop("no_proxy", None)
        env["HERMES_HOME"] = str(profile_home(profile))
        env["PATH"] = f"{hermes.parent}:" + env["PATH"]
        return env

    def hermes_send(target, text):
        profile, plat, chat, thread = split_target(target)
        to = ":".join(x for x in (plat, chat, thread) if x)
        r = subprocess.run([str(hermes), "send", "--to", to, "-q", text], timeout=180, capture_output=True, env=send_env(profile))
        if r.returncode:
            api.log(f"hermes send rc={r.returncode}: {r.stderr.decode(errors='ignore')[-300:]}")

    # ---- Feishu / Lark open API

    def feishu_creds(profile):
        env = {}
        try:
            for line in (profile_home(profile) / ".env").read_text().splitlines():
                if line.startswith("FEISHU_") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
        return env

    def feishu_call(host, path, token=None, body=None, fields=None, file_field=None, file_path=None, file_name=None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if fields is not None:                  # multipart upload
            boundary = "----h3d" + uuid.uuid4().hex
            parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()]
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
                          f'Content-Type: application/octet-stream\r\n\r\n').encode() + Path(file_path).read_bytes() + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            data = b"".join(parts)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        else:
            data = json.dumps(body or {}).encode()
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(host + path, data=data, headers=headers, method="POST")
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=120) as r:
            out = json.loads(r.read() or b"{}")
        if out.get("code") not in (0, None):
            raise RuntimeError(f"Feishu API {path} returned {out.get('code')}: {out.get('msg')}")
        return out

    def feishu_send_video(profile, chat_id, text, video, cover):
        env = feishu_creds(profile)
        chat_id = chat_id or env.get("FEISHU_HOME_CHANNEL")
        if not (env.get("FEISHU_APP_ID") and env.get("FEISHU_APP_SECRET") and chat_id):
            raise RuntimeError("missing Feishu app credentials or chat id")
        host = "https://open.larksuite.com" if env.get("FEISHU_DOMAIN", "feishu").lower() == "lark" else "https://open.feishu.cn"
        token = feishu_call(host, "/open-apis/auth/v3/tenant_access_token/internal",
                            body={"app_id": env["FEISHU_APP_ID"], "app_secret": env["FEISHU_APP_SECRET"]})["tenant_access_token"]
        dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                             capture_output=True, text=True, env=api.clean_env()).stdout.strip()
        content = {"file_key": feishu_call(host, "/open-apis/im/v1/files", token=token,
                                           fields={"file_type": "mp4", "file_name": Path(video).name,
                                                   "duration": int(float(dur) * 1000) if dur else 0},
                                           file_field="file", file_path=video, file_name=Path(video).name)["data"]["file_key"]}
        if cover and Path(cover).exists():
            content["image_key"] = feishu_call(host, "/open-apis/im/v1/images", token=token, fields={"image_type": "message"},
                                               file_field="image", file_path=cover, file_name="cover.jpg")["data"]["image_key"]
        send = "/open-apis/im/v1/messages?receive_id_type=chat_id"
        if text:
            feishu_call(host, send, token=token,
                        body={"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)})
        feishu_call(host, send, token=token, body={"receive_id": chat_id, "msg_type": "media", "content": json.dumps(content)})

    def send(target, text, video=None, cover=None):
        profile, plat, chat, thread = split_target(target)
        if video and plat in ("feishu", "lark") and not thread:
            try:
                return feishu_send_video(profile, chat, text, video, cover)
            except Exception as e:
                api.log(f"Feishu direct send failed, falling back to hermes send: {e}")
        hermes_send(target, text + (f"\nMEDIA:{video}" if video else ""))

    if not hermes.exists():
        api.log(f"hermes not found at {hermes}; notifications will fail until hermes_bin is set")
    api.register_notifier(send, targets=targets, resolve=resolve, client_env=ENV_KEYS)
