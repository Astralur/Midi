"""Pruebas funcionales de la CLI en procesos independientes sin pantalla."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import mido

from tests.test_conversion import hits_events, positive_notes, timing, track_from_events


APP = Path(__file__).resolve().parents[1] / "songsterr_a_invasion.py"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        midi = mido.MidiFile(type=1, ticks_per_beat=480)
        midi.tracks.append(track_from_events([
            (0, mido.MetaMessage("track_name", name="CLI sintética")),
            (0, mido.MetaMessage("set_tempo", tempo=500003)),
            (960, mido.MetaMessage("set_tempo", tempo=421037)),
            (0, mido.MetaMessage("time_signature", numerator=4, denominator=4)),
            (1920, mido.MetaMessage("time_signature", numerator=7, denominator=8)),
        ]))
        midi.tracks.append(track_from_events([
            (0, mido.MetaMessage("track_name", name="Batería principal")),
        ] + hits_events([(0, 35, 90, 30), (120, 36, 100, 30), (240, 38, 110, 45),
                         (500, 39, 80, 25)])))
        self.source = self.root / "prueba con espacios.mid"
        midi.save(self.source)

    def command(self, *args):
        return subprocess.run([sys.executable, str(APP), *(str(arg) for arg in args)],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", timeout=20, cwd=self.root)

    def expected_output(self):
        return self.source.with_name(self.source.stem + "_Invasion.mid")

    def test_default_conversion_prints_inventory_and_writes_validated_files(self):
        result = self.command(self.source)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Notas únicas encontradas (antes de convertir)", result.stdout)
        self.assertIn("APROBADO", result.stdout)
        self.assertLess(result.stdout.index("Notas únicas encontradas"), result.stdout.index("APROBADO"))
        self.assertEqual("", result.stderr)
        output = mido.MidiFile(self.expected_output())
        self.assertEqual([(0, 23, 90, 0), (120, 24, 100, 0), (240, 26, 110, 0)],
                         positive_notes(output.tracks[1]))
        report = self.source.with_name(self.source.stem + "_reporte.txt")
        self.assertTrue(report.exists())
        self.assertIn("APROBADO", report.read_text(encoding="utf-8"))

    def test_redirected_windows_console_uses_utf8_even_with_legacy_encoding(self):
        environment = dict(os.environ, PYTHONIOENCODING="cp1252")
        result = subprocess.run(
            [sys.executable, str(APP), str(self.source)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", timeout=20, cwd=self.root, env=environment,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("SONGSTERR → GGD INVASION", result.stdout)
        self.assertIn("APROBADO", result.stdout)

    def test_list_tracks_is_read_only_and_shows_note_counts(self):
        original = self.source.read_bytes()
        result = self.command(self.source, "--list-tracks")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Pista 1", result.stdout)
        self.assertIn("Batería principal", result.stdout)
        for number in (35, 36, 38, 39):
            self.assertIn(str(number), result.stdout)
        self.assertFalse(self.expected_output().exists())
        self.assertFalse(self.source.with_name(self.source.stem + "_reporte.txt").exists())
        self.assertEqual(original, self.source.read_bytes())

    def test_multiple_candidates_fail_without_selection_and_accept_track_argument(self):
        midi = mido.MidiFile(self.source)
        midi.tracks.append(track_from_events([
            (0, mido.MetaMessage("track_name", name="Percussion alternativa")),
        ] + hits_events([(10, 49, 85, 50)], channel=3)))
        midi.save(self.source)
        result = self.command(self.source)
        self.assertEqual(1, result.returncode)
        self.assertIn("--track", result.stderr)
        self.assertFalse(self.expected_output().exists())
        result = self.command(self.source, "--track", 2)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        output = mido.MidiFile(self.expected_output())
        self.assertEqual([(10, 52, 85, 0)], positive_notes(output.tracks[1]))
        invalid = self.command(self.source, "--track", 0)
        self.assertEqual(1, invalid.returncode)
        self.assertIn("--track", invalid.stderr)

    def test_merge_drums_flag_preserves_all_four_supplementary_hits(self):
        midi = mido.MidiFile(self.source)
        midi.tracks.append(track_from_events([
            (0, mido.MetaMessage("track_name", name="+ Drums")),
        ] + hits_events([(24, 38, 85, 25), (160, 42, 86, 20),
                         (320, 49, 87, 50), (480, 51, 88, 60)], channel=3)))
        midi.save(self.source)
        result = self.command(self.source, "--merge-drums")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("APROBADO", result.stdout)
        self.assertIn("+ Drums", result.stdout)
        output = mido.MidiFile(self.expected_output())
        self.assertEqual([(0, 23, 90, 0), (24, 26, 85, 0), (120, 24, 100, 0),
                          (160, 43, 86, 0), (240, 26, 110, 0), (320, 52, 87, 0),
                          (480, 62, 88, 0)], positive_notes(output.tracks[1]))
        invalid = self.command(self.source, "--merge-drums", "--track", 1)
        self.assertEqual(1, invalid.returncode)
        self.assertTrue(invalid.stderr)

    def test_format_zero_channel_duration_unmapped_and_custom_output(self):
        destination = self.root / "salida elegida.mid"
        result = self.command(self.source, "--format-zero", "--include-unmapped",
                              "--copy-timing", "--channel", 10, "--no-alternate-kicks",
                              "--fixed-duration", 12, "--output", destination)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        output = mido.MidiFile(destination)
        self.assertEqual(0, output.type)
        self.assertEqual(1, len(output.tracks))
        self.assertEqual([(0, 23, 90, 9), (120, 23, 100, 9), (240, 26, 110, 9), (500, 39, 80, 9)],
                         positive_notes(output.tracks[0]))
        source = mido.MidiFile(self.source)
        for kind in ("set_tempo", "time_signature"):
            self.assertEqual(timing(source.tracks[0], kind), timing(output.tracks[0], kind))
        self.assertFalse(self.expected_output().exists())

    def test_copy_timing_in_format_one(self):
        result = self.command(self.source, "--copy-timing")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        output = mido.MidiFile(self.expected_output())
        self.assertEqual(1, output.type)
        for kind in ("set_tempo", "time_signature"):
            self.assertEqual(timing(output.tracks[0], kind), timing(output.tracks[1], kind))

    def test_experimental_fl_tempo_score_option_writes_binary_score_and_csv(self):
        from tests.test_fl_tempo_score import decode_tempo_score

        result = self.command(self.source, "--fl-tempo-score")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("experimental", result.stdout.lower())
        self.assertIn("APROBADO", result.stdout)
        scores = list(self.root.glob("*.fsc"))
        tables = list(self.root.glob("*.csv"))
        self.assertEqual(1, len(scores))
        self.assertEqual(1, len(tables))
        header, records = decode_tempo_score(scores[0])
        self.assertEqual((0x10, 1, 96), header)
        self.assertEqual([(0, 0x40000005, 119999), (192, 0x40000005, 142505)], records)
        midi = mido.MidiFile(self.expected_output())
        self.assertEqual([(0, 23, 90, 0), (120, 24, 100, 0), (240, 26, 110, 0)],
                         positive_notes(midi.tracks[1]))

    def test_help_exits_without_opening_gui(self):
        result = self.command("--help")
        self.assertEqual(0, result.returncode, result.stderr)
        for option in ("--track", "--merge-drums", "--format-zero", "--copy-timing", "--adk",
                       "--octave-offset", "--fl-tempo-score"):
            self.assertIn(option, result.stdout)
        self.assertFalse(self.expected_output().exists())

    def test_invalid_input_and_invalid_options_have_nonzero_exit_codes(self):
        broken = self.root / "roto.mid"
        broken.write_bytes(b"Esto no es MIDI.")
        for arguments, expected in (((broken,), 1),
                                    ((self.root / "inexistente.mid",), 1),
                                    ((self.source, "--fixed-duration", "0"), 1),
                                    ((self.source, "--channel", "2"), 2)):
            with self.subTest(arguments=arguments):
                result = self.command(*arguments)
                self.assertEqual(expected, result.returncode, result.stdout + result.stderr)
                self.assertTrue(result.stderr)
                self.assertFalse(self.expected_output().exists())


if __name__ == "__main__":
    unittest.main()
