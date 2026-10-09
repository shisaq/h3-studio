---
name: h3
description: "Generate short videos with sound locally with MiniMax-H3 (h3.c) through H3 Studio: /h3 switches the engine on; describe a video in chat (text, reference images, first/last frame) and the result is sent back to the conversation. Also stops the engine and reports progress."
version: 2.0.0
platforms: [macos]
metadata:
  hermes:
    tags: [Video, Generation, MiniMax-H3, Local-GPU]
---

# h3: local video generation

H3 Studio's server (h3d) queues jobs and runs h3. You drive it with the `h3ctl` command (in the H3 Studio folder; use its full path if it is not on PATH). Each job is its own process and frees its memory when done. "On" only means jobs are accepted; the engine switches itself off after an idle hour and says so. With the `hermes_notify` plugin enabled, h3ctl sends the result back to **the conversation that asked** (a group stays in the group, a direct message stays direct). Do not pass `--notify` yourself.

Install: copy or symlink this folder into your Hermes skills directory, and enable `hermes_notify` in H3 Studio's config.

## 1. What to do

| The user says | You do |
| --- | --- |
| `/h3` alone, "start h3" | `h3ctl start`, relay the output, add one line on usage (section 5) |
| "stop h3" | `h3ctl stop`; if a job is running, ask whether to wait or stop now (`--force`) |
| "progress", "is it done" | `h3ctl status`; `h3ctl jobs` for history |
| "cancel" | `h3ctl jobs` to find the ID, then `h3ctl cancel <ID>` |
| Describes a video (maybe with images) | Sections 2–4: choose parameters, submit, tell them how long it will take |

Submitting while the engine is off is fine: `submit` switches it on.

## 2. Parameters (use defaults, do not ask about each one)

- **Mode**: images attached → reference mode (`--image`, in the order sent: image 1, 2, ...). "Start from this picture" / "end on this picture" → `--first` / `--last`. No image → text to video.
  - Reference video: `--video` (keeps its sound) or `--silent-video`. Reference audio: `--audio`.
  - In `--desc`, refer to references as `@img1`, `@vid1`, `@vid1.audio`, `@aud1`. The server turns them into the model's labels. **Never write `<Audio N>` yourself**: videos with sound share the audio numbering, and it is easy to get wrong.
  - Limits: 9 images, 12 references in total, 3 that carry sound (2–15 s each); audio needs an image or video alongside. A reference video slows generation a lot (a 2 s one took a draft from about 50 s to about 6 min); say so before submitting.
- **Size** `--size`: `9:16` (default, 576×1024), `16:9`, `1:1`, `3:4`, `4:3`, or `WIDTHxHEIGHT` (multiples of 32, at most 768×1344 pixels in area).
- **Length** `--seconds`: default 2, at most 15. Leave room for the dialogue (roughly 2.5 English words or 4 Chinese characters per second).
- **Quality** `--quality`: `recommended` (default), `draft` ("quick look", "rough"), `high`, `max` (only when asked for the best).
- **Seed** `--seed`: default 42; "another take" → `random`.
- **Music**: none by default (the model's own score tends to be loud). Add `--music` only when the user asks for it.

## 3. The prompt

H3 needs a structured prompt, or speech comes out as mumbling. **You normally do not write it**: pass the description and dialogue, and the server builds the official format, including the `<d>[Language] ...</d>` tag that makes speech intelligible.

```bash
h3ctl submit --desc "A red fox walks through a snowy pine forest, medium tracking shot" \
  --dialogue "Hello there" --language English --seconds 3 --title "Fox"
```

- Write `--desc` in English when you can (you may translate the user's words). Keep the dialogue in the user's language and set `--language` (English, Chinese, Japanese, Cantonese ...).
- Describe the action concretely: where the subject enters from, what it does, where it stops, where it faces.

**Several reference images: describe each with `--about`**, in the same order as `--image`, as `role: what it is | features`:

```bash
h3ctl submit \
  --image person.jpg --about "person: young woman | short black bob, light gray shirt, beige apron" \
  --image place.jpg  --about "scene: modern office | open desks, floor-to-ceiling windows" \
  --desc "@img1 walks slowly into @img2 and introduces the place to the camera" \
  --dialogue "This is where I work." --language English --size 16:9 --seconds 5
```

- Roles: `person`, `scene`, `object` (also animals), `outfit`, `style`.
- **"What it is" must be a specific English noun** (young woman, modern office, red fox). With a generic "person" or "place", that image is mostly ignored. Look at the images to write this.
- Tell the user, when relevant: people work best on a plain background; a scene image should not contain an eye-catching person or animal.

For multi-shot or very detailed scenes you can write the full structured prompt yourself and pass `--prompt-file`, or add `--ai` to let the server's rewriter plugin write it (takes 20–60 s; only if that plugin is enabled).

## 4. After submitting

The command returns within seconds. **Do not wait or poll**: the video is sent when it is done. Tell the user in a sentence or two what is being made and roughly how long it takes (576×1024, recommended quality, on an M5 Pro; draft is about half, high about double):

| 2 s | 3 s | 5 s | 6 s | 8–9 s | 15 s |
| --- | --- | --- | --- | --- | --- |
| ~3 min | ~3.5 min | ~7.5 min | ~10 min | ~17 min | ~35 min |

Each extra second costs more than the last. For 6 seconds or more, tell the user the expected time before submitting. Add queue time if jobs are ahead.

## 5. Replies

- After `/h3`: "Describe the video you want; attach images if you like. Say 'draft' for a faster rough cut, 'stop h3' to switch off." Add the web address from `h3ctl status`.
- **Never put full local file paths in a reply** (Hermes attaches files it sees paths for).
- On failure the server sends the reason. Common ones: size not a multiple of 32, references mixed with first/last frame, longer than 15 seconds.

## 6. Limits

- One job at a time; the rest queue. Generation peaks at roughly 18–26 GB of memory with the GPU fully busy.
- Do not run the `h3` binary directly: bypassing the queue makes jobs fight over the GPU and memory.
