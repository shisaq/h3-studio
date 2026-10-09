"""Chinese -> English with Zhipu GLM (cloud, has a free tier). Only text is sent, never images or dialogue.

plugin_config:
  "zhipu_translate": {
    "key_file": "~/.config/h3d/zhipu.key",   # or set the ZHIPU_API_KEY environment variable
    "model": "glm-4-flash",
    "fallback_model": "glm-4.5-flash",
    "proxy": ""                              # e.g. http://127.0.0.1:7890; empty = connect directly
  }
"""
import json
import os
import re
import urllib.request

URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
SYSTEM = (
    "You translate short Chinese notes for a video-generation prompt into natural, concise English.\n"
    "Rules:\n"
    "- Describe actions in the third person, present tense (\"walks\", \"smiles\").\n"
    "- Tokens like [[P1]], [[V2]], [[A1]] stand for referenced people, places, videos or audio. Keep every token exactly as written, "
    "use each one as a noun in the sentence, and never translate, drop, duplicate or renumber it.\n"
    "- When a token is followed by 的 plus a noun that only names what the token already is "
    "(for example [[P2]] 的树林, [[P2]] 的办公室, [[P1]] 的人, [[P1]] 的女孩), use the token alone: "
    "write \"walks into [[P2]]\", never \"[[P2]]'s forest\" or \"the forest of [[P2]]\".\n"
    "- A phrase starting with 一个 is a noun phrase: translate it as a singular noun phrase starting with \"a\" or \"an\".\n"
    "- Do not add details that are not in the source. Do not explain.\n"
    "Input: a JSON array of strings. Output: only a JSON array of the translated strings, same length, same order."
)


def register(api):
    cfg = api.config
    key_file = api.expand(cfg.get("key_file") or "~/.config/h3d/zhipu.key")
    models = list(dict.fromkeys(m for m in (cfg.get("model") or "glm-4-flash", cfg.get("fallback_model") or "glm-4.5-flash") if m))
    proxy = cfg.get("proxy") or ""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))

    def key():
        if os.environ.get("ZHIPU_API_KEY"):
            return os.environ["ZHIPU_API_KEY"].strip()
        try:
            return key_file.read_text().strip() or None
        except Exception:
            return None

    def translate(texts):
        last = None
        for n, model in enumerate(models):
            body = {"model": model, "temperature": 0.1,
                    "messages": [{"role": "system", "content": SYSTEM},
                                 {"role": "user", "content": json.dumps(texts, ensure_ascii=False)}]}
            if re.match(r"glm-(4\.[5-9]|[5-9])", model):
                body["thinking"] = {"type": "disabled"}    # newer models "think" by default; slow and pointless here
            try:
                req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                             headers={"Content-Type": "application/json", "Authorization": f"Bearer {key()}"})
                with opener.open(req, timeout=12 if n == 0 and len(models) > 1 else 30) as r:
                    content = json.loads(r.read().decode(), strict=False)["choices"][0]["message"]["content"].strip()
                out = json.loads(re.sub(r"^```[a-z]*\s*|\s*```$", "", content).strip())
                if isinstance(out, list) and len(out) == len(texts) and all(isinstance(x, str) for x in out):
                    if n:
                        api.log(f"{models[0]} unavailable ({last}), used {model}")
                    return out
                last = "unexpected reply shape"
            except Exception as e:
                detail = ""
                if hasattr(e, "read"):
                    try:
                        detail = json.loads(e.read().decode(), strict=False).get("error", {}).get("code", "")
                    except Exception:
                        pass
                last = f"{type(e).__name__} {detail}".strip()
        raise RuntimeError(last or "no model configured")

    api.register_translator(translate, available=lambda: bool(key()), local=False, label=api.L("智谱 GLM（云端）", "Zhipu GLM (cloud)"))
