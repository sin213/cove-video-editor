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
        # Inputs must be interleaved per segment (v0,a0,v1,a1,...) as required
        # by the ffmpeg concat filter. All-video-then-all-audio causes type-mismatch.
        self.assertEqual(
            concat_line,
            "[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[vc][ac]",
        )
        self.assertNotIn("[0:v][1:v][2]", concat_line)
        self.assertEqual(v_label, "vc")
        self.assertEqual(a_label, "ac")

    def test_concat_label_join_rejects_raw_input_labels(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid concat label"):
            _join_filter_labels(["v0", "2"])
        with self.assertRaisesRegex(RuntimeError, "invalid concat label"):
            _join_filter_labels(["v0", "0:v"])

    def test_image_clip_silence_uses_48k_by_default(self) -> None:
        """anullsrc defaults to 48000 Hz for AAC/Opus targets."""
        clip = Clip(_asset("still.jpg", has_audio=False, kind="image"), timeline_start=0.0)
        clip.src_end = 3.0
        job = ExportJob(clips=[clip], output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)
        graph, _, _ = worker._build_filtergraph(
            [("clip", 0.0, 3.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280, tgt_h=720,
            is_audio_only=False, needs_audio=True,
        )
        self.assertIn("sample_rate=48000", graph)
        self.assertNotIn("aformat", graph)

    def test_image_clip_silence_uses_44100_for_mp3(self) -> None:
        """anullsrc uses 44100 Hz + aformat when target codec is libmp3lame."""
        clip = Clip(_asset("still.jpg", has_audio=False, kind="image"), timeline_start=0.0)
        clip.src_end = 3.0
        job = ExportJob(clips=[clip], output=Path("out.avi"), fmt_key="AVI (MPEG-4 + MP3)")
        worker = ExportWorker(job)
        graph, _, _ = worker._build_filtergraph(
            [("clip", 0.0, 3.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280, tgt_h=720,
            is_audio_only=False, needs_audio=True,
            acodec="libmp3lame",
        )
        self.assertIn("sample_rate=44100", graph)
        self.assertIn("aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo", graph)

    def test_gap_silence_uses_44100_for_mp3(self) -> None:
        """Gap segments also use 44100 Hz silence for libmp3lame."""
        clip = Clip(_asset("v.mp4", has_audio=True), timeline_start=1.0)
        clip.src_end = 1.0
        job = ExportJob(clips=[clip], output=Path("out.avi"), fmt_key="AVI (MPEG-4 + MP3)")
        worker = ExportWorker(job)
        graph, _, _ = worker._build_filtergraph(
            [("gap", 0.0, 1.0, None), ("clip", 1.0, 2.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280, tgt_h=720,
            is_audio_only=False, needs_audio=True,
            acodec="libmp3lame",
        )
        self.assertIn("sample_rate=44100", graph)

    def test_default_clip_volume_does_not_add_filter(self) -> None:
        clip = Clip(_asset("v.mp4", has_audio=True), timeline_start=0.0)
        job = ExportJob(clips=[clip], output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)

        graph, _, _ = worker._build_filtergraph(
            [("clip", 0.0, 1.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280,
            tgt_h=720,
            is_audio_only=False,
            needs_audio=True,
        )

        self.assertNotIn("volume=", graph)
        self.assertIn("[0:a]atrim=start=0.000:end=1.000,asetpts=PTS-STARTPTS[a0]", graph)

    def test_clip_volume_adds_ffmpeg_volume_filter(self) -> None:
        clip = Clip(_asset("v.mp4", has_audio=True), timeline_start=0.0)
        clip.audio_volume = 1.5
        job = ExportJob(clips=[clip], output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)

        graph, _, _ = worker._build_filtergraph(
            [("clip", 0.0, 1.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280,
            tgt_h=720,
            is_audio_only=False,
            needs_audio=True,
        )

        self.assertIn(
            "[0:a]atrim=start=0.000:end=1.000,asetpts=PTS-STARTPTS,volume=1.500[a0]",
            graph,
        )

    def test_zero_clip_volume_exports_silence_filter(self) -> None:
        clip = Clip(_asset("v.mp4", has_audio=True), timeline_start=0.0)
        clip.audio_volume = 0.0
        job = ExportJob(clips=[clip], output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)

        graph, _, _ = worker._build_filtergraph(
            [("clip", 0.0, 1.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280,
            tgt_h=720,
            is_audio_only=False,
            needs_audio=True,
        )

        self.assertIn("volume=0.000[a0]", graph)

    def test_no_audio_clip_uses_generated_silence_for_export_audio(self) -> None:
        clip = Clip(_asset("silent.mp4", has_audio=False), timeline_start=0.0)
        job = ExportJob(clips=[clip], output=Path("out.mp4"), fmt_key="MP4 (H.264 + AAC)")
        worker = ExportWorker(job)

        graph, _, a_label = worker._build_filtergraph(
            [("clip", 0.0, 1.0, clip)],
            {clip.id: 0},
            [],
            tgt_w=1280,
            tgt_h=720,
            is_audio_only=False,
            needs_audio=True,
        )

        self.assertEqual(a_label, "ac")
        self.assertNotIn("[0:a]", graph)
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000", graph)


if __name__ == "__main__":
    unittest.main()
