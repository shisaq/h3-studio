"""Let an LLM write the full structured prompt, using the Codex CLI (`codex exec`).

Useful for multi-shot or detailed scenes; simple shots do not need it (the built-in composer is as good).
The LLM follows MiniMax's official prompt guides. Their license does not allow redistribution, so copy
Ref2VA.md and T2VA_I2VA_FL2VA_L2VA.md from the official MiniMax-H3 release into guides/ yourself.

plugin_config:
  "codex_rewrite": {
    "codex_bin": "",          # default: `codex` found on PATH
    "model": "",              # default: whatever Codex is configured to use
    "timeout": 240,
    "guide_dir": "",          # default: guides/ next to h3d.py
    "proxy": ""               # e.g. http://127.0.0.1:7890 if Codex needs one
  }
"""
import re
import shutil
import subprocess
import uuid

INSTRUCTIONS = """You rewrite prompts for the MiniMax-H3 video model. Turn the user's request into the structured English prompt H3 was trained on.
Follow the official guide attached below ({guide_name}) strictly. Output only the final prompt: no explanation, no Markdown code fence, nothing before or after.

This task:
- Mode: {mode_desc}
- Target length: about {seconds:.2f} seconds, {width}x{height} ({orient}). Actions and dialogue must fit in this time; do not plan more than fits.
- Reference pictures: {pictures}
{refs_note}- User's description: {description}
- Dialogue: {dialogue}
Requirements:
- Everything in English except dialogue and text that must be visible in the scene. Dialogue goes in <d>[{language}] ...</d>, speakers numbered from (S1).
- Unless the user asks for on-screen text, state that no text or subtitles appear.
- Background music: {music_rule}
- Study the reference pictures first and describe appearance, clothing, style and background accurately.
{extra}
==== OFFICIAL GUIDE START ====
{guide}
==== OFFICIAL GUIDE END ====
"""


def register(api):
    cfg, L = api.config, api.L
    guide_dir = api.expand(cfg["guide_dir"]) if cfg.get("guide_dir") else api.root / "guides"
    codex = cfg.get("codex_bin") or shutil.which("codex", path=api.clean_env()["PATH"]) or "codex"
    timeout = int(cfg.get("timeout") or 240)

    def env():
        e = api.clean_env()
        if cfg.get("proxy"):
            for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
                e[k] = cfg["proxy"]
            e["NO_PROXY"] = e["no_proxy"] = "localhost,127.0.0.1,::1"
        else:
            e.pop("NO_PROXY", None)
            e.pop("no_proxy", None)
        return e

    def guide(name):
        try:
            return (guide_dir / name).read_text()
        except Exception:
            raise api.BadRequest(L(f"缺少官方提示词指南 {name}：请从 MiniMax-H3 官方发布里拷到 guides/ 目录",
                                   f"Missing the official prompt guide {name}: copy it from the MiniMax-H3 release into guides/"))

    def rewrite(req):
        mode = req.get("mode") or "t2va"
        images = [str(api.upload_path(u)) for u in (req.get("images") or [])]
        w, h = int(req.get("width") or 576), int(req.get("height") or 1024)
        seconds = api.frames_for_seconds(float(req.get("seconds") or 2)) / api.FPS
        if mode == "ref2va":
            guide_name = "Ref2VA.md"
            mode_desc = "reference generation (full-reference / Ref2VA); the pictures are <Picture 1>, <Picture 2>, ... in order"
            extra = ("- Output all six sections: subject_definitions / summary / retention_analysis / detailed_description / "
                     "overall_soundscape / non_diegetic_music.")
        else:
            guide_name = "T2VA_I2VA_FL2VA_L2VA.md"
            mode_desc = {"i2va": "first-frame generation (I2VA; <Picture 1> is the first frame)",
                         "fl2va": "first and last frame (FL2VA; <Picture 1> first, <Picture 2> last)",
                         "l2va": "last-frame generation (L2VA; <Picture 1> is the last frame)"}.get(mode, "text to video (T2VA)")
            extra = f"- Use the structure from section 2 of the guide; where frame alignment is stated, use {seconds:.2f} seconds."
        guide_text = guide(guide_name)
        dialogue = (req.get("dialogue") or "").strip()
        description = (req.get("description") or "").strip()
        refs_note, frames = "", []
        if mode == "ref2va":
            kinds = api.video_kinds(req)
            vids = [v for v in (req.get("videos") or []) if isinstance(v, dict) and v.get("id")]
            n_aud = len(req.get("audios") or [])
            api.check_mentions(description + dialogue, len(images), kinds, n_aud)
            description = api.mention_labels(description, kinds)    # the LLM sees the official labels directly
            sound = [i for i, k in enumerate(kinds, 1) if k != "silent"]
            notes = []
            # The LLM cannot watch video: hand it three frames of each reference video as pictures.
            for i, v in enumerate(vids, 1):
                first = len(images) + len(frames) + 1
                got, dur = api.extract_frames(api.upload_path(v["id"]))
                frames += got
                last = len(images) + len(frames)
                if last >= first:
                    notes.append(f"Attached pictures {first}-{last} are frames of <Video {i}> (start, middle, end), about {dur:.1f} s long"
                                 + (f", with sound; its soundtrack is <Audio {sound.index(i) + 1}>" if i in sound else ", muted"))
            for j in range(1, n_aud + 1):
                notes.append(f"<Audio {len(sound) + j}> is a separately uploaded audio reference (you cannot hear it; use it as the "
                             "user describes, or as a voice reference if they do not say)")
            if images:
                notes.insert(0, f"Attached pictures 1-{len(images)} are <Picture 1>...<Picture {len(images)}> in order")
            if notes:
                refs_note = "- Reference numbering (use exactly this, do not invent your own):\n  " + "\n  ".join(notes) + "\n"
        instr = INSTRUCTIONS.format(
            guide_name=guide_name, guide=guide_text, mode_desc=mode_desc, seconds=seconds, width=w, height=h,
            orient="portrait" if h > w else ("landscape" if w > h else "square"),
            pictures=f"{len(images)} (attached)" if images else "none",
            description=description or "(not given)", refs_note=refs_note,
            dialogue=dialogue or "none (nobody speaks; describe ambient sound as usual)",
            language=req.get("language") or "English", extra=extra,
            music_rule=("the user wants music: describe instruments, tempo and dynamics in non_diegetic_music; keep it quiet, under the voice."
                        if req.get("music") else "none: write N/A in non_diegetic_music; ambient sound still goes in overall_soundscape."))
        out = api.paths.state / f"rewrite-{uuid.uuid4().hex}.txt"
        base = [codex, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-o", str(out)]
        for img in images + [str(f) for f in frames]:
            base += ["-i", img]
        err = ""
        try:
            # Try the configured model first, then whatever Codex defaults to.
            for model in dict.fromkeys([cfg.get("model") or None, None]):
                try:
                    r = subprocess.run(base + (["-m", model] if model else []) + ["-"], input=instr.encode(), capture_output=True,
                                       cwd=str(api.paths.state), timeout=timeout, env=env())
                except subprocess.TimeoutExpired:
                    raise api.BadRequest(L("提示词改写超时，请重试，或直接手写提示词", "The rewrite timed out. Try again, or write the prompt yourself"))
                except FileNotFoundError:
                    raise api.BadRequest(L("找不到 codex 命令", "The codex command was not found"))
                if r.returncode == 0 and out.exists() and out.read_text().strip():
                    return out.read_text().strip()
                both = r.stderr.decode(errors="ignore") + r.stdout.decode(errors="ignore")
                msgs = re.findall(r'"message":"([^"]+)"', both)
                err = msgs[-1] if msgs else r.stderr.decode(errors="ignore").strip()[-200:]
                api.log(f"rewrite failed (model={model or 'codex default'}): {err}")
            raise api.BadRequest(L(f"AI 没能写出提示词（{err or '未知错误'}）。可以稍后重试，或直接手写提示词",
                                   f"The LLM did not produce a prompt ({err or 'unknown error'}). Try again later, or write the prompt yourself"))
        finally:
            for f in frames + [out]:
                try:
                    f.unlink()
                except OSError:
                    pass

    api.register_rewriter(rewrite, label=L("Codex", "Codex"))
