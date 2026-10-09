#!/usr/bin/env python3
"""h3d — a small web UI and job queue for h3.c (MiniMax-H3 inference on Apple Silicon).

It stays resident and light: it never loads the model itself. Every job runs as its own
`h3` process, so memory is returned as soon as a job finishes. The "engine" switch only
decides whether queued jobs are executed; it turns itself off after an idle period.

Only the Python standard library is used. Optional features (translation engines, an
LLM prompt rewriter, chat notifications) live in plugins/ and are enabled in the config.
"""
import importlib.util
import ipaddress
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

VERSION = "0.2.0"
ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
CONFIG_FILE = Path(os.environ.get("H3D_CONFIG") or Path.home() / ".config/h3d/config.json")

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8190,
    # Networks allowed to reach the server. There is no login: keep this tight.
    "allow": ["127.0.0.0/8", "::1/128"],
    "lang": "en",                      # language of notifications and of h3ctl output: "en" or "zh"
    "public_url": "",                  # shown by `h3ctl status`, e.g. http://my-mac:8190
    "h3_dir": "~/h3.c",                # checkout of antirez/h3.c, with the built `h3` binary inside
    "model_dir": "~/h3.c/MiniMax-H3",  # must contain FL2VA/ (and Ref2VA/ for reference mode)
    "output_dir": "~/h3-studio/jobs",
    "state_dir": "~/.local/state/h3d",
    "idle_minutes": 60,                # engine turns itself off after this long without work
    "notify_idle": True,
    # Time estimates in the UI were fitted on an M5 Pro (20-core GPU). Scale them for other machines.
    "speed_factor": 1.0,
    "plugins": ["macos_translate"],
    "plugin_config": {},
    "translate_order": [],             # engine names in order of preference; empty = registration order
}

MAX_PIXELS = 768 * 1344
MAX_SECONDS = 15.0
FPS = 24
PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

# Quality presets. Measured on one reference clip (576x1024, 56 frames):
# high 433 s, recommended 188 s with no visible difference, draft 59 s but visibly softer.
QUALITY_PRESETS = {
    "draft": {"layers": 45, "core_reuse": 4, "render_scale": 0.5},
    "recommended": {"layers": 45, "core_reuse": 4},
    "high": {"layers": 50, "steps": 20},
    "max": {"layers": 50, "steps": 50},
}

lock = threading.RLock()
wake = threading.Event()
config = dict(DEFAULT_CONFIG)
P = SimpleNamespace()                  # resolved paths, filled by load_config()
engine = {"enabled": False, "since": None, "by": None, "last_activity": time.time(), "stopping": False, "notify": None}
jobs = {}                              # id -> job dict
order = []                             # submission order
current = {"id": None, "proc": None}

TRANSLATORS = []                       # [{name, fn, available, local, label}]
REWRITERS = []                         # [{name, fn, label}]
NOTIFIERS = {}                         # name -> {send, targets, resolve, client_env}


# ---------------------------------------------------------------- small helpers

def L(zh, en):
    """A message in both languages. Picked per request (web, CLI) or by config (notifications)."""
    return {"zh": zh, "en": en}


def pick(msg, lang=None):
    if isinstance(msg, dict) and ("zh" in msg or "en" in msg):
        return msg.get(lang or config.get("lang") or "en") or msg.get("en") or msg.get("zh")
    return msg


class BadRequest(Exception):
    def __init__(self, msg, code=None):
        super().__init__(pick(msg, "en"))
        self.msg, self.code = msg, code


def now():
    return time.time()


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


def expand(p):
    return Path(os.path.expanduser(str(p)))


def load_config():
    if CONFIG_FILE.exists():
        try:
            config.update(json.loads(CONFIG_FILE.read_text()))
        except Exception as e:           # a broken config must not stop the server from starting
            log(f"config error ({CONFIG_FILE}): {e}")
    else:
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2))
        except Exception:
            pass
    P.h3_dir = expand(config["h3_dir"])
    P.h3_bin = P.h3_dir / "h3"
    P.model = expand(config["model_dir"])
    P.out = expand(config["output_dir"])
    P.state = expand(config["state_dir"])
    P.uploads = P.state / "uploads"
    P.jobs_file = P.state / "jobs.json"


def save_jobs():
    with lock:
        data = [jobs[i] for i in order if i in jobs]
    tmp = P.jobs_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(P.jobs_file)


def load_jobs():
    if not P.jobs_file.exists():
        return
    try:
        data = json.loads(P.jobs_file.read_text())
    except Exception:
        return
    for j in data:
        if j.get("status") == "running":   # the server was restarted while this job was running
            j["status"] = "failed"
            j["error"] = L("服务重启，任务中断", "Interrupted by a server restart")
            j["finished"] = j.get("finished") or now()
        if not isinstance(j.get("notify"), dict):
            j["notify"] = None             # older versions stored a plain string here
        jobs[j["id"]] = j
        order.append(j["id"])


def touch():
    engine["last_activity"] = now()


def clean_env():
    """Environment for h3 and ffmpeg: no proxies (they need no network), Homebrew on PATH."""
    env = {k: v for k, v in os.environ.items() if k not in PROXY_VARS}
    env["NO_PROXY"] = env["no_proxy"] = "*"
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + env.get("PATH", "")
    return env


def fmt_dur(sec, lang=None):
    sec = int(round(sec or 0))
    if (lang or config.get("lang")) == "zh":
        return f"{sec // 60} 分 {sec % 60} 秒" if sec >= 60 else f"{sec} 秒"
    return f"{sec // 60} min {sec % 60} s" if sec >= 60 else f"{sec} s"


def memory_status():
    out = {}
    try:
        out["total_gb"] = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) / 2**30
        out["free_pct"] = int(subprocess.check_output(["sysctl", "-n", "kern.memorystatus_level"]))
    except Exception:
        pass
    return out


def has_fl2va():
    return (P.model / "FL2VA/transformer/config.json").exists()


def has_ref2va():
    return (P.model / "Ref2VA/transformer/model.safetensors.index.json").exists()


def extract_frames(video, fractions=(0.05, 0.5, 0.92), width=640):
    """A few still frames from a video (start, middle, end). For rewriter plugins, which cannot watch video."""
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True, env=clean_env()).stdout.strip()
    try:
        dur = float(dur)
    except ValueError:
        dur = 0.0
    frames = []
    for frac in fractions:
        f = P.state / f"frame-{uuid.uuid4().hex}.jpg"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * frac:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", f"scale={width}:-2", "-pix_fmt", "yuvj420p", "-q:v", "4", str(f)],
                           capture_output=True, env=clean_env())
        if r.returncode == 0 and f.exists():
            frames.append(f)
    return frames, dur


# ---------------------------------------------------------------- plugins

class PluginAPI:
    """What a plugin gets in register(api). Keep plugins to this surface; everything else may change."""

    def __init__(self, name):
        self.name = name
        self.config = (config.get("plugin_config") or {}).get(name, {})
        self.root, self.L, self.BadRequest = ROOT, L, BadRequest
        self.log = lambda *a: log(f"[{name}]", *a)
        self.clean_env, self.expand, self.extract_frames = clean_env, expand, extract_frames
        self.paths, self.core_config, self.FPS = P, config, FPS
        # helpers for rewriters
        self.upload_path, self.frames_for_seconds = upload_path, frames_for_seconds
        self.video_kinds, self.check_mentions, self.mention_labels = video_kinds, check_mentions, mention_labels

    def register_translator(self, translate, available=lambda: True, local=False, label=None, name=None):
        """translate(list[str]) -> list[str], Chinese to English. `local` = text never leaves this machine."""
        name = name or self.name
        TRANSLATORS.append({"name": name, "fn": translate, "available": available, "local": local, "label": label or L(name, name)})

    def register_rewriter(self, rewrite, label=None, name=None):
        """rewrite(req) -> str: turn a plain description (plus reference media) into a full structured prompt."""
        name = name or self.name
        REWRITERS.append({"name": name, "fn": rewrite, "label": label or L(name, name)})

    def register_notifier(self, send, targets=lambda: [], resolve=lambda req: None, client_env=(), name=None):
        """send(target, text, video=None, cover=None); targets() -> [{"target", "label"}];
        resolve(req) -> target for a request from the CLI (req["client_env"] holds the variables named in client_env)."""
        NOTIFIERS[name or self.name] = {"send": send, "targets": targets, "resolve": resolve, "client_env": tuple(client_env)}


def load_plugins():
    for name in config.get("plugins") or []:
        path = ROOT / "plugins" / f"{name}.py"
        try:
            spec = importlib.util.spec_from_file_location(f"h3d_plugin_{name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.register(PluginAPI(name))
            log(f"plugin loaded: {name}")
        except Exception as e:
            log(f"plugin {name} failed to load: {type(e).__name__}: {e}")


def notify_targets():
    out = []
    for name, n in NOTIFIERS.items():
        try:
            out += [dict(t, plugin=name) for t in n["targets"]()]
        except Exception as e:
            log(f"notifier {name} targets failed: {e}")
    return out


def resolve_notify(req):
    """Where to send the result. `notify` may be false, true (ask the plugins), or {"plugin", "target"}."""
    want = req.get("notify")
    if not want:
        return None
    if isinstance(want, dict):
        if want.get("plugin") in NOTIFIERS and want.get("target"):
            return {"plugin": want["plugin"], "target": str(want["target"])}
        return None
    for name, n in NOTIFIERS.items():
        try:
            target = n["resolve"](req)
        except Exception as e:
            log(f"notifier {name} resolve failed: {e}")
            target = None
        if target:
            return {"plugin": name, "target": target}
    ts = notify_targets()                  # nothing claimed the request: fall back to the first default target
    return {"plugin": ts[0]["plugin"], "target": ts[0]["target"]} if ts else None


def deliver(notify, text, video=None, cover=None):
    n = NOTIFIERS.get((notify or {}).get("plugin"))
    if not n:
        return
    try:
        n["send"](notify["target"], text, video=video, cover=cover)
    except Exception as e:
        log(f"notify via {notify['plugin']} failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- translation (Chinese -> English)

CJK_RE = re.compile(r"[㐀-鿿]")


def translators(want=None):
    """Engines to try, in order. want: None/"auto", "local" (text must stay on this machine), "off", or a name."""
    if want == "off":
        return []
    names = config.get("translate_order") or []
    ts = sorted(TRANSLATORS, key=lambda t: names.index(t["name"]) if t["name"] in names else len(names))
    if want in ("local", "system"):
        ts = [t for t in ts if t["local"]]
    elif want not in (None, "", "auto"):
        ts = [t for t in ts if t["name"] == want]
    out = []
    for t in ts:
        try:
            if t["available"]():
                out.append(t)
        except Exception:
            pass
    return out


def to_english(texts, want=None):
    """Translate the strings that contain Chinese; leave the rest alone.
    Returns (texts, why): why is None on success, else "off", "none" (no engine) or "failed"."""
    idx = [i for i, t in enumerate(texts) if t and CJK_RE.search(t)]
    if not idx:
        return list(texts), None
    if want == "off":
        return list(texts), "off"
    engines = translators(want)
    if not engines:
        return list(texts), "none"
    src = [texts[i] for i in idx]
    for t in engines:                      # first engine that works wins; the rest are fallbacks
        try:
            out = t["fn"](src)
            if isinstance(out, list) and len(out) == len(src) and all(isinstance(x, str) for x in out):
                res = list(texts)
                for i, s in zip(idx, out):
                    res[i] = s
                return res, None
            log(f"translator {t['name']} returned a malformed result")
        except Exception as e:
            log(f"translator {t['name']} failed: {str(e)[:160]}")
    return list(texts), "failed"


# ---------------------------------------------------------------- @ mentions of reference media

# In the description, references are written @img1 / @vid1 / @vid1.audio / @aud1
# (or @图1 / @视频1 / @视频1原声 / @音频1 in the Chinese UI). They become official labels here.
MENTION_RE = re.compile(r"@(图|视频|音频|img|vid|aud)\s*(\d+)(原声|\.audio)?", re.I)
KIND = {"图": "img", "img": "img", "视频": "vid", "vid": "vid", "音频": "aud", "aud": "aud"}
PLACEHOLDER_RE = re.compile(r"\[\[\s*([PVApva])\s*(\d+)\s*\]\]")

# What a reference image contributes -> (generic noun, what stays the same).
REF_ROLES = {
    "person": ("person", "the {noun}'s face, hair, and clothing stay consistent with"),
    "scene": ("place", "the {noun} stays recognizable from"),
    "object": ("object", "the {noun}'s shape, color, and details stay consistent with"),
    "outfit": ("outfit", "the {noun} keeps the design shown in"),
    "style": ("visual style", "the {noun} follows"),
}


def video_kinds(req):
    """Kinds of the reference videos in a request, in order ("video" keeps its soundtrack, "silent" drops it)."""
    v = req.get("videos")
    if v is None:
        v = (req.get("inputs") or {}).get("ref_videos") or []
    return [(x.get("kind") or "video") if isinstance(x, dict) else "video" for x in v]


def _audio_index(kinds, kind, n, snd):
    """Audio labels are shared: h3 numbers every reference that carries sound in command-line order, and videos
    come before standalone audio clips. So videos with sound take <Audio 1..k>, standalone clips start at k+1."""
    sound = [i for i, k in enumerate(kinds, 1) if k != "silent"]
    if kind == "vid":
        return sound.index(n) + 1 if snd and n in sound else None
    return len(sound) + n


def mention_labels(text, kinds, picture_as_subject=False):
    """@img1 -> <Picture 1> (or <Subject 1>), @vid1 -> <Video 1>, @vid1.audio / @aud1 -> <Audio n>."""
    def rep(m):
        kind, n, snd = KIND[m.group(1).lower()], int(m.group(2)), m.group(3)
        if kind == "img":
            return f"<Subject {n}>" if picture_as_subject else f"<Picture {n}>"
        if kind == "vid" and not snd:
            return f"<Video {n}>"
        a = _audio_index(kinds, kind, n, snd)
        return f"<Audio {a}>" if a else m.group(0)
    return MENTION_RE.sub(rep, text or "")


def mentions_to_placeholders(text, kinds):
    """Translation mangles @ marks, so swap them for [[P1]] / [[V1]] / [[A1]] first."""
    def rep(m):
        kind, n, snd = KIND[m.group(1).lower()], int(m.group(2)), m.group(3)
        if kind == "img":
            return f" [[P{n}]] "
        if kind == "vid" and not snd:
            return f" [[V{n}]] "
        a = _audio_index(kinds, kind, n, snd)
        return f" [[A{a}]] " if a else m.group(0)
    return re.sub(r"\s{2,}", " ", MENTION_RE.sub(rep, text or "")).strip()


def placeholders_to_labels(text):
    names = {"P": "Subject", "V": "Video", "A": "Audio"}
    text = PLACEHOLDER_RE.sub(lambda m: f"<{names[m.group(1).upper()]} {int(m.group(2))}>", text)
    return re.sub(r"\s+([,.;:!?，。；：！？])", r"\1", text)


def check_mentions(text, n_img, kinds, n_audio):
    """Reject a mention of something that was not uploaded, so the prompt never carries a label the model cannot resolve."""
    limits = {"img": n_img, "vid": len(kinds), "aud": n_audio}
    names = {"img": L("图片", "image(s)"), "vid": L("视频", "video(s)"), "aud": L("音频", "audio clip(s)")}
    for m in MENTION_RE.finditer(text or ""):
        kind, n, snd = KIND[m.group(1).lower()], int(m.group(2)), m.group(3)
        if not 1 <= n <= limits[kind]:
            raise BadRequest(L(f"描述里写了 {m.group(0)}，但没有这个素材（{names[kind]['zh']}一共 {limits[kind]} 个）",
                               f"The description mentions {m.group(0)}, but there are only {limits[kind]} {names[kind]['en']}"))
        if kind == "vid" and snd and kinds[n - 1] == "silent":
            raise BadRequest(L(f"{m.group(0)}：视频 {n} 设成了静音，没有原声可用",
                               f"{m.group(0)}: video {n} is set to silent, so it has no soundtrack"))


# ---------------------------------------------------------------- composing the prompt without an LLM

def _zh_cleanup(body, roles, names):
    """Two Chinese habits that translate badly; fix them before translating.

    "@图1 的人" repeats what the mark already is and comes out as "<Subject 1> people".
    "走进 @图2 的树林": picture 2 *is* the forest, yet it becomes "<Subject 2>'s forest", which turns the
    scene into a property of itself. Measured: 0 of 4 runs fully correct that way, 2 of 2 with "walks into
    <Subject 2>". So when the noun after 的 names the same thing as the image, keep only the mark
    ("@图2 的窗边", a different thing, is left alone).
    """
    body = re.sub(r"(\[\[P\d+\]\])\s*的\s*(人物|人|女孩子?|女生|女人|女性|男孩子?|男生|男人|男性|角色|主角)(?![㐀-鿿]{0,1}们)", r"\1", body)
    generic = {"scene": "场景环境地方背景空间", "outfit": "衣服服装造型穿搭", "object": "东西物体", "style": "风格画风样子"}
    tail = r"(?=[里中内外上下旁边，。、,.;；！!？?\s]|慢|缓|快|走|站|坐|跑|跳|向|对|说|看|$)"
    for i, role in enumerate(roles, 1):
        if role == "person":
            continue
        same = set(CJK_RE.findall(names[i - 1])) | set(generic.get(role, ""))

        def drop(m, same=same):
            return m.group(1) if set(m.group(2)) & same else m.group(0)
        body = re.sub(r"(\[\[P%d\]\])\s*(?:里|中)?的\s*([㐀-鿿]{1,6}?)" % i + tail, drop, body)
    return body


def _tidy(t):
    """Translated nouns and features get embedded mid-sentence: drop the article, final punctuation, leading capital."""
    t = re.sub(r"^(the|an|a)\s+", "", t.strip(), flags=re.I).rstrip(".。,，")
    return t[:1].lower() + t[1:] if t[:1].isupper() and not t[:2].isupper() else t


NOTES = {
    "none": L("没有可用的翻译，中文原样交给了模型（多张参考图时请用英文写）",
              "No translator is available, so the Chinese text was passed through as is (write English when using several references)"),
    "failed": L("翻译失败，中文原样交给了模型", "Translation failed; the Chinese text was passed through as is"),
    "placeholder_lost": L("翻译时素材标记丢了，描述保留了原文", "The translation lost a reference mark, so the original description was kept"),
}


def compose_prompt(req):
    """Wrap the description, the line of dialogue and its language in the official prompt format.

    No model is involved. What the format really requires is small: fixed field names, [Shot 1], and
    dialogue inside <d>[Language] ...</d> with a speaker id. In a same-seed comparison this was as good as
    an LLM-written prompt; speech is intelligible because of the <d> tag, not because of the LLM.
    """
    mode = req.get("mode") or "t2va"
    desc = " ".join((req.get("description") or "").split())
    if not desc:
        raise BadRequest(L("先写一句想要的画面", "Describe the shot first"))
    if desc[-1] not in ".。!！?？":
        desc += "."
    dialogue = (req.get("dialogue") or "").strip()
    language = req.get("language") or "English"
    seconds = frames_for_seconds(float(req.get("seconds") or 2)) / FPS
    want = req.get("translate")

    if mode == "ref2va":
        kinds = video_kinds(req)
        n_aud = len(req.get("audios") or [])
        n_img = len(req.get("images") or [])
        check_mentions(desc + dialogue, n_img, kinds, n_aud)
        meta = (list(req.get("refs") or []) + [{}] * n_img)[:n_img]
        roles = [(m.get("role") if m.get("role") in REF_ROLES else ("person" if i == 0 else "scene")) for i, m in enumerate(meta)]
        raw_feats = [" ".join((m.get("desc") or "").split()).rstrip("。.，,") for m in meta]
        # "What it is" must be a specific noun (young woman, snowy pine forest). With a generic
        # "person"/"place" the scene image was mostly ignored in testing.
        names = [" ".join((m.get("name") or "").split()).rstrip("。.，,") for m in meta]
        body_src = _zh_cleanup(mentions_to_placeholders(desc, kinds), roles, names)
        # Prefix nouns with 一个 so they translate as singular ("年轻女性" alone becomes "Young women").
        names_src = [("一个" + n if CJK_RE.search(n) and not n.startswith("一") else n) for n in names]
        translated, why = to_english([body_src] + raw_feats + names_src, want)
        body = placeholders_to_labels(translated[0])
        if len(PLACEHOLDER_RE.findall(translated[0])) != len(PLACEHOLDER_RE.findall(body_src)):
            body, why = placeholders_to_labels(body_src), "placeholder_lost"
        feats = [(_tidy(f) if CJK_RE.search(src) else f) for f, src in zip(translated[1:1 + n_img], raw_feats)]
        # If nothing was translated, keep the nouns as typed (without the 一个 added for the translator).
        names = [_tidy(n) for n in translated[1 + n_img:]] if why is None else names

        n_audio_labels = sum(1 for k in kinds if k != "silent") + n_aud
        defs, retention, only = [], [], []
        scene = next((i for i in range(1, n_img + 1) if roles[i - 1] == "scene"), 0)
        for i in range(1, n_img + 1):
            generic, keep = REF_ROLES[roles[i - 1]]
            noun = names[i - 1] or generic
            defs.append(f"<Subject {i}> is the {noun} in <Picture {i}>" + (f", with {feats[i - 1]}." if feats[i - 1] else "."))
            retention.append(f"<Subject {i}> (appears in [Shot 1]): fully_preserved - {keep.format(noun=noun)} <Picture {i}>.")
            # With a scene image present, the other images must contribute only their subject. Without this
            # sentence the person image's own background took over the frame in 2 of 4 runs; with it, 0 of 4.
            if scene and roles[i - 1] in ("person", "object", "outfit"):
                only.append(f"Only the {noun} is taken from <Picture {i}>; the background of <Picture {i}> never appears.")
        defs += [f"<Video {i}> is a reference video; its role is stated in the description below." for i in range(1, len(kinds) + 1)]
        defs += [f"<Audio {i}> is a reference audio track; its role is stated in the description below." for i in range(1, n_audio_labels + 1)]
        retention += [f"<Audio {i}>: reference - used as stated in the description." for i in range(1, n_audio_labels + 1)]
        task = "reference generation" + (" + audio reference" if n_audio_labels else "")
        speaker = next((f"<Subject {i}>" for i in range(1, n_img + 1) if roles[i - 1] == "person"), "<Subject 1>" if n_img else "The speaker")
        speech = f" {speaker} (S1) says in a clear voice, <d>[{language}] {dialogue}</d>" if dialogue else ""
        setting = f"The setting is <Subject {scene}> throughout the shot. " if scene else ""
        return {"prompt": (
            "subject_definitions:\n" + "\n".join(defs) + "\n\n"
            f"summary:\n[{task}] {body}\n\n"
            "retention_analysis:\n" + "\n".join(retention) + "\n\n"
            "detailed_description:\nNo visible text or subtitles." + (" " + " ".join(only) if only else "") + "\n"
            f"[Shot 1] {setting}{body}{speech}\n\n"
            "overall_soundscape: Clean, quiet ambience. No hum, hiss, or background noise.\n\n"
            "non_diegetic_music: N/A"), "seconds": seconds, "note": NOTES.get(why)}

    head = {
        "i2va": "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n\n",
        "fl2va": ("How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark "
                  f"of the target video; Picture 2 (from Shot 1) aligns with the {seconds:.2f}-second mark of the target video.\n\n"),
        "l2va": ("How the reference pictures align with the target video — <Picture 1> (from [Shot 1]) aligns with the "
                 f"{seconds:.2f}-second mark of the target video.\n\n"),
    }.get(mode, "")
    speech = f" The speaker (S1) says: <d>[{language}] {dialogue}</d>" if dialogue else ""
    (desc,), why = to_english([desc], want)
    return {"prompt": (
        f"{head}integrated_multimodal_description: [Shot 1] {desc}{speech}\n\n"
        "overall_soundscape: Natural ambient sound that matches the scene.\n\n"
        "non_diegetic_music: N/A"), "seconds": seconds, "note": NOTES.get(why)}


def rewrite_prompt(req):
    if not REWRITERS:
        raise BadRequest(L("没有启用 AI 扩写插件", "No prompt-rewriter plugin is enabled"))
    text = REWRITERS[0]["fn"](req)
    touch()
    return {"prompt": re.sub(r"^```[a-z]*\n|\n```$", "", (text or "").strip()).strip(),
            "seconds": frames_for_seconds(float(req.get("seconds") or 2)) / FPS}


# ---------------------------------------------------------------- validating a job and building the command line

def frames_for_seconds(sec):
    """h3 rounds the frame count up to 5 + 17n."""
    want = max(1, math.ceil(sec * FPS))
    return 5 + 17 * max(0, math.ceil((want - 5) / 17))


def render_size(w, h, scale):
    """Internal canvas for draft quality: same aspect ratio, multiples of 32, closest to `scale` of the output."""
    gw, gh = w // 32, h // 32
    g = math.gcd(gw, gh)
    k = min(g, max(1, round(g * scale)))
    return gw // g * k * 32, gh // g * k * 32


def as_int(v, name, lo, hi):
    try:
        v = int(v)
    except Exception:
        raise BadRequest(L(f"{name['zh']}必须是整数", f"{name['en']} must be an integer"))
    if not lo <= v <= hi:
        raise BadRequest(L(f"{name['zh']}需在 {lo}–{hi} 之间", f"{name['en']} must be between {lo} and {hi}"))
    return v


UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}(\.[a-z0-9]{1,5})?")


def upload_path(uid):
    if not isinstance(uid, str) or not UPLOAD_ID_RE.fullmatch(uid):
        raise BadRequest(L(f"无效的上传文件 {uid}", f"Invalid upload id {uid}"))
    p = P.uploads / uid
    if not p.exists():
        raise BadRequest(L("上传的文件已不存在，请重新上传", "The uploaded file is gone; please upload it again"))
    return p


def normalize(req):
    """Turn a request into a fixed parameter set. Returns (params, inputs)."""
    p = req.get("params") or {}
    out = {}
    w = as_int(p.get("width", 576), L("宽", "Width"), 32, 1344)
    h = as_int(p.get("height", 1024), L("高", "Height"), 32, 1344)
    if w % 32 or h % 32:
        raise BadRequest(L("宽和高都必须是 32 的倍数", "Width and height must be multiples of 32"))
    if w * h > MAX_PIXELS:
        raise BadRequest(L("分辨率超过 768×1344 的面积上限", "The resolution exceeds the 768×1344 pixel budget"))
    out["width"], out["height"] = w, h

    if p.get("frames"):
        out["frames"] = as_int(p["frames"], L("帧数", "Frame count"), 1, int(MAX_SECONDS * FPS))
    else:
        try:
            sec = float(p.get("seconds", 2))
        except Exception:
            raise BadRequest(L("时长必须是数字", "Duration must be a number"))
        if not 0 < sec <= MAX_SECONDS:
            raise BadRequest(L("时长需在 0–15 秒之间", "Duration must be between 0 and 15 seconds"))
        out["seconds"] = sec

    preset = p.get("quality") or "recommended"
    base = dict(QUALITY_PRESETS.get(preset, {})) if preset != "custom" else {}
    out["quality"] = preset
    out["steps"] = as_int(p.get("steps", base.get("steps", 20)), L("步数", "Steps"), 1, 60)
    out["layers"] = as_int(p.get("layers", base.get("layers", 50)), L("层数", "Layers"), 30, 50)
    reuse = as_int(p.get("reuse", base.get("reuse", 1)) or 1, L("整体复用", "Reuse"), 1, 3)
    core = as_int(p.get("core_reuse", base.get("core_reuse", 1)) or 1, L("核心复用", "Core reuse"), 1, 6)
    if reuse > 1 and core > 1:
        raise BadRequest(L("整体复用与核心复用只能二选一", "Whole-step reuse and core reuse cannot be combined"))
    out["reuse"], out["core_reuse"] = reuse, core
    if out["steps"] <= 7 and reuse > 1:
        raise BadRequest(L("步数 ≤7 时请保持整体复用为 1", "Keep reuse at 1 when using 7 steps or fewer"))
    out["token_reduction"] = bool(p.get("token_reduction"))

    rw, rh = p.get("render_width"), p.get("render_height")
    if rw or rh:
        rw, rh = as_int(rw, L("内部宽", "Render width"), 32, w), as_int(rh, L("内部高", "Render height"), 32, h)
        if rw % 32 or rh % 32 or rw * h != rh * w:
            raise BadRequest(L("内部画布须为 32 的倍数且与输出同比例",
                               "The render canvas must use multiples of 32 and the output's aspect ratio"))
        if (rw, rh) != (w, h):
            out["render_width"], out["render_height"] = rw, rh
    elif base.get("render_scale"):
        rw, rh = render_size(w, h, base["render_scale"])
        if (rw, rh) != (w, h):
            out["render_width"], out["render_height"] = rw, rh

    seed = p.get("seed", 42)
    seed = random.randint(1, 2**31 - 1) if seed in (None, "", -1, "-1", "random") else seed
    out["seed"] = as_int(seed, L("种子", "Seed"), 0, 2**31 - 1)
    out["ref_image_size"] = "max" if p.get("ref_image_size") == "max" else "match"
    out["crop"] = p.get("crop") if p.get("crop") in ("center", "top", "none") else "center"
    out["int8_row_fc2"] = bool(p.get("int8_row_fc2"))
    out["ssd_streaming"] = bool(p.get("ssd_streaming"))
    if out["int8_row_fc2"] and out["ssd_streaming"]:
        raise BadRequest(L("SSD 流式加载不能与 int8 行量化同时开启", "SSD streaming cannot be combined with int8 row FC2"))
    out["reference_rope"] = bool(p.get("reference_rope"))
    env = {}
    if p.get("qwen_prefetch") not in (None, ""):
        env["H3_QWEN_PREFETCH"] = str(as_int(p["qwen_prefetch"], L("Qwen 预取线程", "Qwen prefetch workers"), 0, 8))
    if p.get("qwen_prefetch_depth") not in (None, ""):
        env["H3_QWEN_PREFETCH_DEPTH"] = str(as_int(p["qwen_prefetch_depth"], L("Qwen 预取深度", "Qwen prefetch depth"), 1, 6))
    if p.get("zero_copy") is False:
        env["H3_ZERO_COPY_WEIGHTS"] = "0"
    out["env"] = env

    i = req.get("inputs") or {}
    inputs = {"ref_images": [], "first_frame": None, "last_frame": None, "ref_videos": [], "ref_audios": []}
    for uid in i.get("ref_images") or []:
        inputs["ref_images"].append(str(upload_path(uid)))
    for key in ("first_frame", "last_frame"):
        if i.get(key):
            inputs[key] = str(upload_path(i[key]))
    for v in i.get("ref_videos") or []:
        kind = v.get("kind", "video")
        if kind not in ("video", "silent", "video_audio"):
            raise BadRequest(L("参考视频类型无效", "Invalid reference video kind"))
        item = {"path": str(upload_path(v.get("id"))), "kind": kind}
        if kind == "video_audio":
            item["audio"] = str(upload_path(v.get("audio")))
        inputs["ref_videos"].append(item)
    for uid in i.get("ref_audios") or []:
        inputs["ref_audios"].append(str(upload_path(uid)))
    # h3's hard limits, checked here so the user is not told 20 seconds into loading the model.
    n_sound = sum(1 for v in inputs["ref_videos"] if v["kind"] != "silent") + len(inputs["ref_audios"])
    if len(inputs["ref_images"]) > 9:
        raise BadRequest(L("参考图最多 9 张", "At most 9 reference images"))
    if len(inputs["ref_images"]) + len(inputs["ref_videos"]) + len(inputs["ref_audios"]) > 12:
        raise BadRequest(L("参考素材合计最多 12 个", "At most 12 references in total"))
    if n_sound > 3:
        raise BadRequest(L("带声音的参考素材最多 3 个（带原声的视频也算；可以把视频设成静音）",
                           "At most 3 references may carry sound (videos with a soundtrack count; set a video to silent to free a slot)"))
    if inputs["ref_audios"] and not (inputs["ref_images"] or inputs["ref_videos"]):
        raise BadRequest(L("参考音频要和图片或视频一起用", "A reference audio clip needs an image or video reference alongside it"))
    has_ref = inputs["ref_images"] or inputs["ref_videos"] or inputs["ref_audios"]
    if has_ref and (inputs["first_frame"] or inputs["last_frame"]):
        raise BadRequest(L("参考素材不能与首帧/尾帧同时使用", "References cannot be combined with a first or last frame"))
    if has_ref and not has_ref2va():
        raise BadRequest(L("Ref2VA 模型未安装，无法使用参考素材", "The Ref2VA checkpoint is not installed, so references are unavailable"))
    return out, inputs


def crop_image(src, dst, w, h, mode):
    """Crop a reference image to the output aspect ratio. mode: center or top."""
    vf = (f"crop='if(gt(iw/ih,{w}/{h}),ih*{w}/{h},iw)':'if(gt(iw/ih,{w}/{h}),ih,iw*{h}/{w})'"
          f":'(iw-out_w)/2':'{'0' if mode == 'top' else '(ih-out_h)/2'}'")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vf", vf, "-pix_fmt", "yuvj420p", "-q:v", "2", dst],
                       capture_output=True, env=clean_env())
    return r.returncode == 0


def build_argv(job):
    p, i, d = job["params"], job["inputs_local"], Path(job["dir"])
    a = ["/usr/bin/time", "-l", str(P.h3_bin), "--profile", "-d", str(P.model),
         "--width", str(p["width"]), "--height", str(p["height"])]
    a += ["--frames", str(p["frames"])] if p.get("frames") else ["--seconds", f"{p['seconds']:g}"]
    a += ["--steps", str(p["steps"]), "--layers", str(p["layers"]), "--seed", str(p["seed"])]
    if p["reuse"] > 1:
        a += ["--reuse", str(p["reuse"])]
    if p["core_reuse"] > 1:
        a += ["--core-reuse", str(p["core_reuse"])]
    if p.get("render_width"):
        a += ["--render-width", str(p["render_width"]), "--render-height", str(p["render_height"])]
    for flag, key in (("--token-reduction", "token_reduction"), ("--use-int8-row-fc2", "int8_row_fc2"),
                      ("--ssd-streaming", "ssd_streaming"), ("--use-reference-rope", "reference_rope")):
        if p.get(key):
            a.append(flag)
    if i["first_frame"]:
        a += ["--first-frame", i["first_frame"]]
    if i["last_frame"]:
        a += ["--last-frame", i["last_frame"]]
    for img in i["ref_images"]:
        a += ["--ref-image", img]
    if i["ref_images"] and p["ref_image_size"] == "max":
        a += ["--ref-image-size", "max"]
    # Order matters for audio numbering: videos first, then standalone audio (see _audio_index).
    for v in i["ref_videos"]:
        if v["kind"] == "video":
            a += ["--ref-video", v["path"]]
        elif v["kind"] == "silent":
            a += ["--ref-silent-video", v["path"]]
        else:
            a += ["--ref-video-audio", v["path"], v["audio"]]
    for au in i["ref_audios"]:
        a += ["--ref-audio", au]
    return a + ["-p", (d / "prompt.txt").read_text(), "-o", str(d / "output.mp4")]


def job_mode(inputs):
    if inputs["ref_images"] or inputs["ref_videos"] or inputs["ref_audios"]:
        return "ref2va"
    if inputs["first_frame"] and inputs["last_frame"]:
        return "fl2va"
    if inputs["first_frame"]:
        return "i2va"
    if inputs["last_frame"]:
        return "l2va"
    return "t2va"


MODE_NAMES = {"t2va": L("文生视频", "Text to video"), "ref2va": L("参考生成", "Reference"), "i2va": L("首帧生成", "First frame"),
              "fl2va": L("首尾帧", "First + last frame"), "l2va": L("尾帧生成", "Last frame")}


def job_upload_ids(req):
    i = req.get("inputs") or {}
    ids = list(i.get("ref_images") or []) + list(i.get("ref_audios") or []) + [i.get("first_frame"), i.get("last_frame")]
    for v in i.get("ref_videos") or []:
        ids += [v.get("id"), v.get("audio")]
    return sorted({x for x in ids if isinstance(x, str) and x})


def uploads_of(job):
    """Upload ids a job used (older jobs only have them inside the saved web form)."""
    ids = set(job.get("upload_ids") or [])
    u = (job.get("ui") or {}).get("uploads") or {}
    for x in (u.get("refs") or []) + (u.get("audios") or []) + (u.get("videos") or []) + [u.get("first"), u.get("last")]:
        if isinstance(x, dict) and x.get("id"):
            ids.add(x["id"])
    return ids


def strip_music(prompt):
    """Force the trailing non_diegetic_music section to N/A. The score h3 adds on its own tends to be loud and muddy."""
    return re.sub(r"(?is)non_diegetic_music\s*:.*\Z", "non_diegetic_music: N/A", prompt)


def create_job(req):
    prompt = (req.get("prompt") or "").strip()
    if not prompt:
        raise BadRequest(L("提示词为空", "The prompt is empty"))
    if not req.get("music"):               # no background music unless asked for
        prompt = strip_music(prompt)
    _in = req.get("inputs") or {}
    kinds = video_kinds(req)
    check_mentions(prompt, len(_in.get("ref_images") or []), kinds, len(_in.get("ref_audios") or []))
    prompt = mention_labels(prompt, kinds)   # @ marks also work in hand-written prompts
    params, inputs = normalize(req)
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    d = P.out / jid
    (d / "inputs").mkdir(parents=True)
    (d / "prompt.txt").write_text(prompt)
    # Copy inputs into the job folder: the upload cache expires, the job folder can be re-run as is.
    local = {"ref_images": [], "first_frame": None, "last_frame": None, "ref_videos": [], "ref_audios": []}
    w, h = params["width"], params["height"]

    def bring(src, name, crop=False):
        dst = d / "inputs" / name
        if crop and params["crop"] != "none" and Path(src).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            cropped = dst.with_suffix(".jpg")
            if crop_image(src, str(cropped), w, h, params["crop"]):
                return str(cropped)
        shutil.copy2(src, dst)
        return str(dst)

    for n, src in enumerate(inputs["ref_images"], 1):
        local["ref_images"].append(bring(src, f"picture{n}{Path(src).suffix}", crop=True))
    for key in ("first_frame", "last_frame"):
        if inputs[key]:
            local[key] = bring(inputs[key], f"{key}{Path(inputs[key]).suffix}", crop=True)
    for n, v in enumerate(inputs["ref_videos"], 1):
        item = {"kind": v["kind"], "path": bring(v["path"], f"video{n}{Path(v['path']).suffix}")}
        if v.get("audio"):
            item["audio"] = bring(v["audio"], f"video{n}-audio{Path(v['audio']).suffix}")
        local["ref_videos"].append(item)
    for n, src in enumerate(inputs["ref_audios"], 1):
        local["ref_audios"].append(bring(src, f"audio{n}{Path(src).suffix}"))

    job = {
        "id": jid, "created": now(), "status": "queued", "source": req.get("source") or "web",
        "title": (req.get("title") or "").strip()[:80],
        "description": (req.get("description") or "").strip()[:2000],
        "mode": job_mode(inputs), "params": params, "inputs_local": local, "dir": str(d),
        "notify": resolve_notify(req), "progress": None, "error": None,
        "started": None, "finished": None, "metrics": {},
        "ui": req.get("ui"),                  # snapshot of the web form, for "reuse settings"
        "upload_ids": job_upload_ids(req),
    }
    (d / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=1))
    with lock:
        jobs[jid] = job
        order.append(jid)
        touch()
    save_jobs()
    wake.set()
    return job


# ---------------------------------------------------------------- running jobs

PROG_RE = re.compile(r"([A-Za-z][A-Za-z0-9 ()-]*?)\s+(\d+)/(\d+)")

# Rough share of total time per phase (measured: loading and encoding ~20%, denoising ~55%, decoding ~25%).
PHASE_SPAN = [
    ("tokenizer", 0, 1), ("vision encoder", 1, 3), ("video VAE encoder", 3, 6), ("audio VAE encoder", 6, 7),
    ("text encoder", 7, 12), ("refine text", 12, 13), ("precompute AdaLN", 13, 15),
    ("load transformer core", 15, 24), ("load transformer", 15, 24), ("denoise enqueue", 24, 26), ("denoise", 26, 74),
    ("audio VAE", 74, 76), ("video VAE load", 76, 80), ("video VAE", 80, 98), ("FFmpeg", 98, 100),
]


def parse_progress(text):
    """h3 prints "phase n/total" and redraws it in place; take the last one in the tail of the log."""
    m = None
    for m in PROG_RE.finditer(text):
        pass
    if not m:
        return None
    phase, done, total = m.group(1).strip(), int(m.group(2)), int(m.group(3))
    span = next(((lo, hi) for name, lo, hi in PHASE_SPAN if name == phase), None)
    overall = round(span[0] + (span[1] - span[0]) * (done / total if total else 0)) if span else None
    return {"phase": phase, "done": done, "total": total, "overall": overall}


def job_title(job, lang=None):
    return job.get("title") or pick(MODE_NAMES.get(job["mode"], job["mode"]), lang)


def run_job(job):
    d = Path(job["dir"])
    log_path = d / "h3.log"
    env = clean_env()
    env.update(job["params"].get("env") or {})
    argv = build_argv(job)
    job["status"], job["started"] = "running", now()
    save_jobs()
    t0 = now()
    with open(log_path, "wb") as logf:
        proc = subprocess.Popen(argv, cwd=P.h3_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
        current["proc"] = proc
        tail = b""
        while True:
            chunk = proc.stdout.read1(8192)
            if not chunk:
                break
            logf.write(chunk)
            tail = (tail + chunk)[-4096:]
            prog = parse_progress(tail.decode("utf-8", "ignore"))
            if prog:
                last = (job.get("progress") or {}).get("overall") or 0
                if prog["overall"] is None or prog["overall"] < last:
                    prog["overall"] = last      # overall progress never goes backwards
                job["progress"] = prog
        rc = proc.wait()
    current["proc"] = None
    text = log_path.read_text(errors="ignore").replace("\r", "\n")
    metrics = {"wall_s": round(now() - t0, 1)}
    m = re.search(r"(\d+)\s+peak memory footprint", text)
    if m:
        metrics["peak_gb"] = round(int(m.group(1)) / 1e9, 1)
    for key, pat in (("denoise_s", r"GPU Euler denoise wall=\s*([0-9.]+)s"),
                     ("vae_s", r"video VAE decoder\s+total\s+wall=\s*([0-9.]+)s")):
        mm = re.findall(pat, text)
        if mm:
            metrics[key] = float(mm[-1])
    job["metrics"], job["finished"] = metrics, now()
    out = d / "output.mp4"
    if job["status"] == "canceled":
        pass
    elif rc == 0 and out.exists():
        job["status"], job["progress"] = "done", None
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(out), "-vf", "select=eq(n\\,0)", "-frames:v", "1",
                        "-pix_fmt", "yuvj420p", "-q:v", "3", str(d / "poster.jpg")], capture_output=True, env=env)
    else:
        job["status"] = "failed"
        errs = [ln for ln in text.splitlines() if re.search(r"error|failed|invalid|cannot|must", ln, re.I)]
        job["error"] = errs[-1][-300:] if errs else L(f"h3 退出码 {rc}", f"h3 exited with code {rc}")
    (d / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=1))
    save_jobs()
    if job.get("notify") and job["status"] in ("done", "failed"):
        p, lang = job["params"], config.get("lang")
        dur = p.get("seconds") or round(p.get("frames", 0) / FPS, 1)
        title = job_title(job, lang)
        if job["status"] == "done":
            msg = pick(L(f"✅ h3 完成：{title}\n{p['width']}×{p['height']}，约 {dur:g} 秒，用时 {fmt_dur(metrics['wall_s'], 'zh')}",
                         f"✅ h3 done: {title}\n{p['width']}×{p['height']}, about {dur:g} s, took {fmt_dur(metrics['wall_s'], 'en')}"), lang)
            args = (job["notify"], msg, str(out), str(d / "poster.jpg"))
        else:
            args = (job["notify"], pick(L(f"❌ h3 失败：{title}\n", f"❌ h3 failed: {title}\n"), lang) + str(pick(job["error"], lang)))
        threading.Thread(target=deliver, args=args, daemon=True).start()


def worker():
    while True:
        wake.wait(5)
        wake.clear()
        while True:
            with lock:
                nxt = None
                if engine["enabled"] and not engine["stopping"]:
                    nxt = next((jobs[i] for i in order if jobs.get(i, {}).get("status") == "queued"), None)
                if nxt:
                    current["id"] = nxt["id"]
            if not nxt:
                break
            try:
                run_job(nxt)
            except Exception as e:
                nxt["status"], nxt["finished"] = "failed", now()
                nxt["error"] = L(f"服务内部错误：{e}", f"Internal error: {e}")
                save_jobs()
            with lock:
                current["id"] = None
                touch()
        with lock:
            if engine["stopping"] and not current["id"]:
                engine.update(enabled=False, stopping=False, since=now(), by="manual")


def idle_watcher():
    while True:
        time.sleep(30)
        with lock:
            if not engine["enabled"] or current["id"]:
                continue
            if any(jobs[i]["status"] == "queued" for i in order if i in jobs):
                continue
            if now() - engine["last_activity"] < config["idle_minutes"] * 60:
                continue
            target = engine.get("notify")
            engine.update(enabled=False, stopping=False, since=now(), by="idle")
        log("engine auto-off (idle)")
        if config.get("notify_idle") and target:
            deliver(target, pick(L(f"💤 h3 服务已空闲 {config['idle_minutes']} 分钟，自动关闭。",
                                   f"💤 h3 has been idle for {config['idle_minutes']} minutes and switched itself off.")))


def engine_start(by="manual", req=None):
    if not P.h3_bin.exists():
        raise BadRequest(L(f"找不到 h3 程序（{P.h3_bin}）。请检查配置里的 h3_dir",
                           f"h3 binary not found at {P.h3_bin}. Check h3_dir in the config"), "setup")
    if not has_fl2va():
        raise BadRequest(L(f"模型目录不完整（{P.model}）。请检查配置里的 model_dir",
                           f"Model folder is incomplete ({P.model}). Check model_dir in the config"), "setup")
    with lock:
        if not engine["enabled"]:
            engine.update(enabled=True, since=now(), by=by,
                          notify=resolve_notify(dict(req, notify=req.get("notify") or True)) if req else None)
        engine["stopping"] = False
        touch()
    wake.set()


def engine_stop(force=False, by="manual"):
    with lock:
        if current["id"] and not force:
            engine["stopping"] = True          # finish the running job, keep the queue
            return "stopping"
        engine.update(enabled=False, stopping=False, since=now(), by=by)
        if current["id"] and force:
            cancel_job(current["id"])
    return "off"


def cancel_job(jid):
    with lock:
        job = jobs.get(jid)
        if not job:
            raise BadRequest(L("任务不存在", "No such job"))
        if job["status"] == "queued":
            job["status"], job["finished"] = "canceled", now()
        elif job["status"] == "running":
            job["status"] = "canceled"
            proc = current.get("proc")
            if proc and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

                def reap(pr=proc):
                    time.sleep(10)
                    if pr.poll() is None:
                        try:
                            os.killpg(pr.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                threading.Thread(target=reap, daemon=True).start()
        touch()
    save_jobs()


def delete_job(jid):
    with lock:
        job = jobs.get(jid)
        if not job:
            raise BadRequest(L("任务不存在", "No such job"))
        if job["status"] in ("queued", "running"):
            raise BadRequest(L("请先取消任务", "Cancel the job first"))
        jobs.pop(jid)
        order.remove(jid)
        still_used = set().union(*[uploads_of(j) for j in jobs.values()]) if jobs else set()
    shutil.rmtree(job["dir"], ignore_errors=True)
    for uid in uploads_of(job) - still_used:   # uploads no other job refers to go with it
        if isinstance(uid, str) and UPLOAD_ID_RE.fullmatch(uid):
            try:
                (P.uploads / uid).unlink()
            except FileNotFoundError:
                pass
    save_jobs()


# ---------------------------------------------------------------- HTTP

def public_job(j, lang=None, log_tail=False):
    d = {k: v for k, v in j.items() if k not in ("inputs_local", "dir")}
    d["error"] = pick(j.get("error"), lang)
    d["mode_name"] = pick(MODE_NAMES.get(j["mode"], j["mode"]), lang)
    base =f"/files/jobs/{j['id']}/"
    d["video"] = base + "output.mp4" if j["status"] == "done" else None
    d["poster"] = base + "poster.jpg" if (Path(j["dir"]) / "poster.jpg").exists() else None
    d["inputs"] = {k: ([base + "inputs/" + Path(x if isinstance(x, str) else x["path"]).name for x in v]
                       if isinstance(v, list) else (base + "inputs/" + Path(v).name if v else None))
                   for k, v in j["inputs_local"].items()}
    try:
        d["prompt"] = (Path(j["dir"]) / "prompt.txt").read_text()
    except Exception:
        d["prompt"] = ""
    if log_tail:
        try:
            d["log_tail"] = (Path(j["dir"]) / "h3.log").read_text(errors="ignore").replace("\r", "\n")[-3000:]
        except Exception:
            d["log_tail"] = ""
    return d


def status_payload(lang=None):
    with lock:
        cur = jobs.get(current["id"]) if current["id"] else None
        queued = [i for i in order if jobs[i]["status"] == "queued"]
        idle_left = None
        if engine["enabled"] and not cur and not queued:
            idle_left = max(0, config["idle_minutes"] * 60 - (now() - engine["last_activity"]))
        usable = {t["name"] for t in translators()}
        return {
            "version": VERSION, "lang": config.get("lang"), "speed_factor": config.get("speed_factor") or 1.0,
            "public_url": config.get("public_url") or "",
            "engine": {"enabled": engine["enabled"], "stopping": engine["stopping"], "since": engine["since"],
                       "by": engine["by"], "idle_left_s": idle_left, "idle_minutes": config["idle_minutes"]},
            "current": public_job(cur, lang) if cur else None,
            "queue": [public_job(jobs[i], lang) for i in queued],
            "memory": memory_status(),
            "setup": {"h3": P.h3_bin.exists(), "fl2va": has_fl2va(), "ref2va": has_ref2va()},
            "translators": [{"name": t["name"], "label": pick(t["label"], lang), "local": t["local"],
                             "available": t["name"] in usable} for t in TRANSLATORS],
            "rewriter": ({"name": REWRITERS[0]["name"], "label": pick(REWRITERS[0]["label"], lang)} if REWRITERS else None),
            "notify_targets": [{"plugin": t["plugin"], "target": t["target"], "label": pick(t.get("label") or t["target"], lang)}
                               for t in notify_targets()],
        }


MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac",
        ".txt": "text/plain; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml"}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg")


class Handler(BaseHTTPRequestHandler):
    server_version = "h3d/" + VERSION

    def log_message(self, fmt, *args):
        pass

    @property
    def lang(self):
        v = (self.headers.get("X-H3-Lang") or "").lower()
        return v if v in ("zh", "en") else config.get("lang") or "en"

    def allowed(self):
        try:
            ip = ipaddress.ip_address(self.client_address[0])
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            return any(ip.version == ipaddress.ip_network(n).version and ip in ipaddress.ip_network(n) for n in config["allow"])
        except Exception:
            return False

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, e):
        self.send_json({"error": pick(e.msg, self.lang), "code": e.code}, 400)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise BadRequest(L("请求体过大", "Request body too large"))
        return json.loads(self.rfile.read(n) or b"{}")

    def send_file(self, path):
        path = Path(path)
        if not path.is_file():
            return self.send_json({"error": "not found"}, 404)
        size = path.stat().st_size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):   # range requests let the browser seek inside a video
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        if path.suffix in (".html", ".js", ".css"):
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                buf = f.read(min(1 << 20, left))
                if not buf:
                    break
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(buf)

    def safe_under(self, root, rel):
        p = (root / unquote(rel)).resolve()
        if root.resolve() not in p.parents and p != root.resolve():
            raise BadRequest(L("非法路径", "Invalid path"))
        return p

    def do_GET(self):
        if not self.allowed():
            return self.send_json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        path = u.path
        try:
            if path in ("/", "/index.html"):
                return self.send_file(WEB_DIR / "index.html")
            if path.startswith("/static/"):
                return self.send_file(self.safe_under(WEB_DIR, path[len("/static/"):]))
            if path.startswith("/files/jobs/"):
                return self.send_file(self.safe_under(P.out, path[len("/files/jobs/"):]))
            if path.startswith("/files/uploads/"):
                return self.send_file(upload_path(path[len("/files/uploads/"):]))
            if path == "/api/status":
                return self.send_json(status_payload(self.lang))
            if path == "/api/client-env":      # env var names the CLI should forward (asked for by notifier plugins)
                return self.send_json(sorted({k for n in NOTIFIERS.values() for k in n["client_env"]}))
            if path == "/api/jobs":
                limit = int((parse_qs(u.query).get("limit") or ["50"])[0])
                with lock:
                    return self.send_json([public_job(jobs[i], self.lang) for i in list(reversed(order))[:limit]])
            m = re.fullmatch(r"/api/jobs/([\w-]+)", path)
            if m:
                job = jobs.get(m.group(1))
                if not job:
                    return self.send_json({"error": pick(L("任务不存在", "No such job"), self.lang)}, 404)
                return self.send_json(public_job(job, self.lang, log_tail=True))
            return self.send_json({"error": "not found"}, 404)
        except BadRequest as e:
            return self.fail(e)

    def do_POST(self):
        if not self.allowed():
            return self.send_json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        path = u.path
        try:
            if path == "/api/upload":          # the request body is the file itself
                name = (parse_qs(u.query).get("name") or ["file"])[0]
                ext = Path(name).suffix.lower()
                if ext not in IMAGE_EXTS + VIDEO_EXTS + AUDIO_EXTS:
                    raise BadRequest(L(f"不支持的文件类型 {ext or '（无扩展名）'}", f"Unsupported file type {ext or '(no extension)'}"))
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= 2 * 2**30:
                    raise BadRequest(L("文件为空或超过 2GB", "The file is empty or larger than 2 GB"))
                P.uploads.mkdir(parents=True, exist_ok=True)
                uid = uuid.uuid4().hex + ext
                with open(P.uploads / uid, "wb") as f:
                    left = n
                    while left > 0:
                        buf = self.rfile.read(min(1 << 20, left))
                        if not buf:
                            break
                        f.write(buf)
                        left -= len(buf)
                kind = "image" if ext in IMAGE_EXTS else ("video" if ext in VIDEO_EXTS else "audio")
                return self.send_json({"id": uid, "name": name, "kind": kind, "size": n, "url": f"/files/uploads/{uid}"})
            body = self.body_json()
            if path == "/api/engine":
                act = body.get("action")
                if act == "start":
                    engine_start(by=body.get("by") or "manual", req=body)
                    return self.send_json(status_payload(self.lang))
                if act == "stop":
                    r = engine_stop(force=bool(body.get("force")))
                    return self.send_json(dict(status_payload(self.lang), result=r))
                raise BadRequest(L("action 只能是 start / stop", "action must be start or stop"))
            if path == "/api/jobs":
                if not engine["enabled"] or engine["stopping"]:
                    if not body.get("autostart"):
                        return self.send_json({"error": pick(L("服务未开启", "The engine is off"), self.lang), "code": "engine_off"}, 409)
                    engine_start(by=body.get("source") or "web", req=body if body.get("notify") else None)
                job = create_job(body)
                with lock:
                    ahead = sum(1 for i in order if jobs[i]["status"] in ("queued", "running")) - 1
                return self.send_json({"job": public_job(job, self.lang), "ahead": ahead})
            m = re.fullmatch(r"/api/jobs/([\w-]+)/cancel", path)
            if m:
                cancel_job(m.group(1))
                return self.send_json({"ok": True})
            if path in ("/api/compose", "/api/rewrite"):
                r = compose_prompt(body) if path == "/api/compose" else rewrite_prompt(body)
                return self.send_json(dict(r, note=pick(r.get("note"), self.lang)))
            return self.send_json({"error": "not found"}, 404)
        except BadRequest as e:
            return self.fail(e)
        except json.JSONDecodeError:
            return self.send_json({"error": pick(L("请求不是合法 JSON", "The request is not valid JSON"), self.lang)}, 400)

    def do_DELETE(self):
        if not self.allowed():
            return self.send_json({"error": "forbidden"}, 403)
        try:
            m = re.fullmatch(r"/api/jobs/([\w-]+)", urlparse(self.path).path)
            if m:
                delete_job(m.group(1))
                return self.send_json({"ok": True})
            return self.send_json({"error": "not found"}, 404)
        except BadRequest as e:
            return self.fail(e)


def cleanup_uploads():
    """Uploads are kept for 7 days; each job has its own copy of what it used."""
    while True:
        cutoff = now() - 7 * 86400
        for p in list(P.uploads.glob("*")) + list(P.state.glob("frame-*.jpg")):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except Exception:
                pass
        time.sleep(6 * 3600)


def main():
    load_config()
    for d in (P.state, P.uploads, P.out):
        d.mkdir(parents=True, exist_ok=True)
    load_jobs()
    load_plugins()
    for fn in (worker, idle_watcher, cleanup_uploads):
        threading.Thread(target=fn, daemon=True).start()
    srv = ThreadingHTTPServer((config["host"], int(config["port"])), Handler)
    srv.daemon_threads = True
    log(f"h3d {VERSION} listening on {config['host']}:{config['port']} (config: {CONFIG_FILE})")
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    srv.serve_forever()


if __name__ == "__main__":
    main()
