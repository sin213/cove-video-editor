from pathlib import Path
import unittest

from cove_video_editor.clip import Clip, MediaAsset
from cove_video_editor.exporter import ExportJob, ExportWorker, _join_filter_labels


def _asset(name: str, *, has_audio: bool, kind: str = "video") -> MediaAsset:
    return MediaAsset(
        path=Path(name),
        duration=1.0,
        width=1280,
        height=720,
        fps=30.0,
        has_audio=has_audio,
        kind=kind,
    )


class ExporterFiltergraphTests(unittest.TestCase):
    def test_video_concat_uses_generated_segment_labels(self) -> None:
        clips = [
            Clip(_asset("with-audio.mp4", has_audio=True), timeline_start=0.0),
            Clip(_asset("without-audio.mp4", has_audio=False), timeline_start=1.0),
            Clip(_asset("still.png", has_audio=False, kind="image"), timeline_start=2.0),
        ]
        job = ExportJob(clips=clips, output=Path("out.mp4"), fmt_key="mp4")
        worker = ExportWorker(job)

        graph, v_label, a_label = worker._build_filtergraph(
            [("clip", c.timeline_start, c.timeline_end, c) for c in clips],
            {c.id: i for i, c in enumerate(clips)},
            [],
            tgt_w=1280,
            tgt_h=720,
            is_audio_only=False,
            needs_audio=True,
        )

        concat_line = next(part for part in graph.split(";") if "concat=n=3" in part)
        self.assertEqual(
            concat_line,
            "[v0][v1][v2][a0][a1][a2]concat=n=3:v=1:a=1[vc][ac]",
        )
        self.assertNotIn("[0:v][1:v][2]", concat_line)
        self.assertEqual(v_label, "vc")
        self.assertEqual(a_label, "ac")

    def test_concat_label_join_rejects_raw_input_labels(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid concat label"):
            _join_filter_labels(["v0", "2"])
        with self.assertRaisesRegex(RuntimeError, "invalid concat label"):
            _join_filter_labels(["v0", "0:v"])


if __name__ == "__main__":
    unittest.main()
