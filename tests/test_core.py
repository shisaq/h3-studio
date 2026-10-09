"""Tests for the parts of h3d that need neither the model nor a GPU.  Run: python3 -m unittest discover tests"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("h3d", ROOT / "h3d.py")
h3d = importlib.util.module_from_spec(spec)
sys.modules["h3d"] = h3d
spec.loader.exec_module(h3d)

TMP = Path(tempfile.mkdtemp(prefix="h3d-test-"))
h3d.config.update(h3_dir=str(TMP / "h3"), model_dir=str(TMP / "model"), output_dir=str(TMP / "jobs"), state_dir=str(TMP / "state"))
h3d.P.h3_dir, h3d.P.model, h3d.P.out, h3d.P.state = TMP / "h3", TMP / "model", TMP / "jobs", TMP / "state"
h3d.P.h3_bin, h3d.P.uploads, h3d.P.jobs_file = TMP / "h3/h3", TMP / "state/uploads", TMP / "state/jobs.json"
h3d.P.uploads.mkdir(parents=True)


class Frames(unittest.TestCase):
    def test_frame_counts_align_to_5_plus_17n(self):
        self.assertEqual(h3d.frames_for_seconds(0.2), 5)
        self.assertEqual(h3d.frames_for_seconds(2), 56)
        self.assertEqual(h3d.frames_for_seconds(5), 124)
        for sec in (0.5, 1, 3.3, 9, 15):
            f = h3d.frames_for_seconds(sec)
            self.assertEqual((f - 5) % 17, 0)
            self.assertGreaterEqual(f, sec * h3d.FPS)

    def test_draft_canvas_keeps_aspect_ratio(self):
        self.assertEqual(h3d.render_size(576, 1024, 0.5), (288, 512))
        w, h = h3d.render_size(768, 1344, 0.5)
        self.assertEqual(w * 1344, h * 768)
        self.assertEqual((w % 32, h % 32), (0, 0))


class Mentions(unittest.TestCase):
    def test_both_spellings(self):
        self.assertEqual(h3d.mention_labels("@img1 meets @图2", []), "<Picture 1> meets <Picture 2>")
        self.assertEqual(h3d.mention_labels("@IMG1", [], picture_as_subject=True), "<Subject 1>")

    def test_audio_numbers_are_shared_between_videos_and_audio_clips(self):
        kinds = ["silent", "video"]            # video 1 has no sound, video 2 does
        self.assertEqual(h3d.mention_labels("@vid2.audio then @aud1", kinds), "<Audio 1> then <Audio 2>")
        self.assertEqual(h3d.mention_labels("@视频2原声 @音频1 @视频1", kinds), "<Audio 1> <Audio 2> <Video 1>")

    def test_unknown_reference_is_rejected(self):
        with self.assertRaises(h3d.BadRequest):
            h3d.check_mentions("@img3 waves", 2, [], 0)
        with self.assertRaises(h3d.BadRequest):
            h3d.check_mentions("@vid1.audio", 0, ["silent"], 0)
        h3d.check_mentions("@img2 and @aud1", 2, [], 1)

    def test_placeholders_survive_a_round_trip(self):
        src = h3d.mentions_to_placeholders("@图1 走进 @图2，听着 @音频1", [])
        self.assertEqual(src, "[[P1]] 走进 [[P2]] ，听着 [[A1]]")
        self.assertEqual(h3d.placeholders_to_labels("[[P1]] walks into [[ p2 ]] , hearing [[A1]]"),
                         "<Subject 1> walks into <Subject 2>, hearing <Audio 1>")


class Compose(unittest.TestCase):
    def test_text_to_video(self):
        r = h3d.compose_prompt({"mode": "t2va", "description": "A fox walks in snow", "dialogue": "Hello", "language": "English", "seconds": 2})
        self.assertIn("integrated_multimodal_description: [Shot 1] A fox walks in snow.", r["prompt"])
        self.assertIn("<d>[English] Hello</d>", r["prompt"])
        self.assertTrue(r["prompt"].endswith("non_diegetic_music: N/A"))
        self.assertAlmostEqual(r["seconds"], 56 / 24)
        self.assertIsNone(r["note"])

    def test_first_and_last_frame_alignment_uses_real_length(self):
        r = h3d.compose_prompt({"mode": "fl2va", "description": "The camera pushes in", "seconds": 5})
        self.assertIn("Picture 2 (from Shot 1) aligns with the 5.17-second mark", r["prompt"])

    def test_reference_mode_defines_every_image(self):
        r = h3d.compose_prompt({
            "mode": "ref2va", "seconds": 3, "images": ["a", "b"], "language": "Chinese", "dialogue": "欢迎光临！",
            "description": "@img1 walks slowly into @img2 and stops",
            "refs": [{"role": "person", "name": "young woman", "desc": "short black bob"}, {"role": "scene", "name": "snowy pine forest", "desc": ""}]})
        p = r["prompt"]
        self.assertIn("<Subject 1> is the young woman in <Picture 1>, with short black bob.", p)
        self.assertIn("<Subject 2> is the snowy pine forest in <Picture 2>.", p)
        self.assertIn("Only the young woman is taken from <Picture 1>; the background of <Picture 1> never appears.", p)
        self.assertIn("[Shot 1] The setting is <Subject 2> throughout the shot. <Subject 1> walks slowly into <Subject 2> and stops.", p)
        self.assertIn("<Subject 1> (S1) says in a clear voice, <d>[Chinese] 欢迎光临！</d>", p)
        for section in ("subject_definitions:", "summary:", "retention_analysis:", "detailed_description:", "overall_soundscape:", "non_diegetic_music: N/A"):
            self.assertIn(section, p)

    def test_chinese_without_a_translator_is_passed_through_with_a_note(self):
        saved, h3d.TRANSLATORS[:] = list(h3d.TRANSLATORS), []
        try:
            r = h3d.compose_prompt({"mode": "t2va", "description": "一只狐狸在雪地里走", "seconds": 2})
            self.assertIn("一只狐狸在雪地里走", r["prompt"])
            self.assertIn("No translator", h3d.pick(r["note"], "en"))
            r = h3d.compose_prompt({"mode": "ref2va", "seconds": 2, "images": ["a"], "description": "@图1 挥手", "refs": [{"name": "年轻女性"}]})
            self.assertIn("<Subject 1> is the 年轻女性 in <Picture 1>.", r["prompt"])
            self.assertIsNone(h3d.compose_prompt({"mode": "t2va", "description": "一只狐狸", "translate": "off"})["note"])
        finally:
            h3d.TRANSLATORS[:] = saved

    def test_translation_keeps_reference_marks_and_falls_back(self):
        def broken(texts):
            raise RuntimeError("down")

        def fake(texts):
            return [{"[[P1]]慢慢走进 [[P2]].": "[[P1]] walks slowly into [[P2]].", "一个年轻女性": "A young woman",
                     "一个雪地松林": "A snowy pine forest", "黑色短发": "Short black hair."}[x] for x in texts]
        saved = list(h3d.TRANSLATORS)
        h3d.TRANSLATORS[:] = [{"name": "broken", "fn": broken, "available": lambda: True, "local": False, "label": "x"},
                              {"name": "fake", "fn": fake, "available": lambda: True, "local": True, "label": "y"}]
        try:
            r = h3d.compose_prompt({"mode": "ref2va", "seconds": 2, "images": ["a", "b"], "description": "@图1 的女孩慢慢走进 @图2 的树林",
                                    "refs": [{"role": "person", "name": "年轻女性", "desc": "黑色短发"}, {"role": "scene", "name": "雪地松林"}]})
            self.assertIn("<Subject 1> is the young woman in <Picture 1>, with short black hair.", r["prompt"])
            self.assertIn("<Subject 1> walks slowly into <Subject 2>.", r["prompt"])
            self.assertIsNone(r["note"])
            self.assertEqual([t["name"] for t in h3d.translators("local")], ["fake"])
        finally:
            h3d.TRANSLATORS[:] = saved


class Jobs(unittest.TestCase):
    def test_limits_and_presets(self):
        params, inputs = h3d.normalize({"params": {"quality": "draft"}})
        self.assertEqual((params["layers"], params["core_reuse"], params["render_width"], params["render_height"]), (45, 4, 288, 512))
        self.assertEqual(h3d.job_mode(inputs), "t2va")
        for bad in ({"width": 500}, {"width": 1344, "height": 1344}, {"seconds": 16}, {"reuse": 2, "core_reuse": 4}, {"steps": 0}):
            with self.assertRaises(h3d.BadRequest, msg=str(bad)):
                h3d.normalize({"params": bad})

    def test_upload_ids_cannot_escape_the_upload_folder(self):
        for bad in ("../../etc/passwd", "abc", "", None, "0" * 32 + "/x"):
            with self.assertRaises(h3d.BadRequest, msg=str(bad)):
                h3d.upload_path(bad)

    def test_command_line(self):
        job = {"params": h3d.normalize({"params": {"width": 512, "height": 512, "seconds": 1, "seed": 7}})[0], "dir": str(TMP),
               "inputs_local": {"ref_images": ["/a.jpg"], "first_frame": None, "last_frame": None, "ref_audios": ["/v.wav"],
                                "ref_videos": [{"kind": "silent", "path": "/m.mp4"}]}}
        (TMP / "prompt.txt").write_text("hello")
        argv = h3d.build_argv(job)
        self.assertEqual(argv[argv.index("--seed") + 1], "7")
        self.assertIn("--core-reuse", argv)
        # videos must come before standalone audio: h3 numbers audio labels in this order
        self.assertLess(argv.index("--ref-silent-video"), argv.index("--ref-audio"))
        self.assertEqual(argv[-4:], ["-p", "hello", "-o", str(TMP / "output.mp4")])

    def test_music_is_removed_unless_asked_for(self):
        self.assertEqual(h3d.strip_music("a\n\nnon_diegetic_music: loud drums\nmore"), "a\n\nnon_diegetic_music: N/A")

    def test_progress_from_the_tail_of_the_log(self):
        p = h3d.parse_progress("text encoder 3/3\rdenoise 10/20")
        self.assertEqual((p["phase"], p["done"], p["total"], p["overall"]), ("denoise", 10, 20, 50))


class Messages(unittest.TestCase):
    def test_errors_exist_in_both_languages(self):
        try:
            h3d.normalize({"params": {"width": 500}})
        except h3d.BadRequest as e:
            self.assertIn("32", h3d.pick(e.msg, "en"))
            self.assertIn("倍数", h3d.pick(e.msg, "zh"))


if __name__ == "__main__":
    unittest.main()
