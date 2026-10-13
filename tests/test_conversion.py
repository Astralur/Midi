"""Pruebas de los números MIDI y de la conservación de eventos y ticks."""

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mido

from invasion_core import Options, convert, inspect_midi
from invasion_mapping import DEFAULT_MAPPING, TARGETS, load_mapping, name_to_number, note_name


# Expectativas independientes de los datos que carga la aplicación.
GM_EXPECTED = {
    35: 23, 36: 23, 38: 26, 40: 26, 37: 30,
    42: 43, 44: 48, 46: 46, 50: 33, 48: 34,
    47: 35, 45: 36, 43: 37, 41: 38, 49: 52,
    57: 54, 55: 73, 52: 65, 51: 62, 59: 63, 53: 61,
}
KONTAKT_EXPECTED = {
    23: "B-1", 24: "C0", 22: "A#-1", 26: "D0", 27: "D#0",
    28: "E0", 29: "F0", 30: "F#0", 33: "A0", 34: "A#0",
    35: "B0", 36: "C1", 37: "C#1", 38: "D1", 41: "F1",
    42: "F#1", 43: "G1", 44: "G#1", 45: "A1", 46: "A#1",
    47: "B1", 48: "C2", 17: "F-1", 52: "E2", 53: "F2",
    54: "F#2", 55: "G2", 56: "G#2", 57: "A2", 58: "A#2",
    59: "B2", 61: "C#3", 62: "D3", 63: "D#3", 65: "F3",
    66: "F#3", 67: "G3", 68: "G#3", 70: "A#3", 71: "B3",
    73: "C#4", 74: "D4", 75: "D#4", 76: "E4", 78: "F#4",
    79: "G4", 80: "G#4",
}


def track_from_events(events):
    """Construye deltas enteros desde una lista de ticks absolutos."""
    track = mido.MidiTrack()
    last = 0
    for tick, message in sorted(events, key=lambda item: item[0]):
        track.append(message.copy(time=tick - last))
        last = tick
    track.append(mido.MetaMessage("end_of_track", time=0))
    return track


def absolute_events(track):
    tick = 0
    result = []
    for message in track:
        tick += message.time
        result.append((tick, message))
    return result


def positive_notes(track):
    return [(tick, msg.note, msg.velocity, msg.channel)
            for tick, msg in absolute_events(track)
            if msg.type == "note_on" and msg.velocity > 0]


def note_ends(track):
    return [(tick, msg.note, msg.channel)
            for tick, msg in absolute_events(track)
            if msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)]


def timing(track, kind):
    """Compara todos los campos MIDI, omitiendo únicamente el delta."""
    return [(tick, {key: value for key, value in msg.dict().items() if key != "time"})
            for tick, msg in absolute_events(track) if msg.type == kind]


def hits_events(hits, channel=9):
    events = []
    for tick, number, velocity, duration in hits:
        events.append((tick, mido.Message("note_on", note=number, velocity=velocity, channel=channel)))
        events.append((tick + duration, mido.Message("note_off", note=number, velocity=0, channel=channel)))
    return events


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_midi(self, hits=(), *, ppq=480, conductor=None, drum_extra=(), extra_tracks=(), midi_type=1):
        midi = mido.MidiFile(type=midi_type, ticks_per_beat=ppq)
        if conductor is None:
            conductor = [(0, mido.MetaMessage("track_name", name="Canción sintética")),
                         (0, mido.MetaMessage("set_tempo", tempo=500000)),
                         (0, mido.MetaMessage("time_signature", numerator=4, denominator=4))]
        drums = [(0, mido.MetaMessage("track_name", name="Drums Songsterr"))]
        drums.extend(hits_events(hits))
        drums.extend(drum_extra)
        if midi_type == 0:
            midi.tracks.append(track_from_events(list(conductor) + drums))
        else:
            midi.tracks.append(track_from_events(conductor))
            midi.tracks.append(track_from_events(drums))
            midi.tracks.extend(extra_tracks)
        path = self.root / "original.mid"
        midi.save(path)
        return path

    def run_conversion(self, path, **kwargs):
        result = convert(path, **kwargs)
        self.assertTrue(result.passed, result.report)
        self.assertTrue(Path(result.output_path).is_file())
        self.assertTrue(Path(result.report_path).is_file())
        return result, mido.MidiFile(result.output_path)

    def write_split_drums(self, main_hits, supplementary_hits, *, main_name=None, supplementary_channel=1):
        """Songsterr puede separar golpes auxiliares en una pista '+ Drums'."""
        midi = mido.MidiFile(type=1, ticks_per_beat=480)
        midi.tracks.append(track_from_events([
            (0, mido.MetaMessage("track_name", name="Canción dividida")),
            (0, mido.MetaMessage("set_tempo", tempo=500003)),
            (480, mido.MetaMessage("set_tempo", tempo=433337)),
            (0, mido.MetaMessage("time_signature", numerator=4, denominator=4)),
        ]))
        main_events = [(0, mido.MetaMessage("instrument_name", name="Drums"))]
        if main_name is not None:
            main_events.append((0, mido.MetaMessage("track_name", name=main_name)))
        midi.tracks.append(track_from_events(main_events + hits_events(main_hits, channel=0)))
        midi.tracks.append(track_from_events([
            (0, mido.MetaMessage("track_name", name="+ Drums")),
        ] + hits_events(supplementary_hits, channel=supplementary_channel)))
        # Esta nota coincidiría con un número GM si se confundiera con batería.
        midi.tracks.append(track_from_events([
            (0, mido.MetaMessage("instrument_name", name="Guitar")),
        ] + hits_events([(10, 38, 127, 40)], channel=2)))
        path = self.root / "bateria_dividida.mid"
        midi.save(path)
        return path

    def test_complete_kontakt_table_uses_midi_numbers_and_fl_octaves(self):
        actual = {item["midi"]: item["kontakt"] for item in TARGETS}
        self.assertEqual(KONTAKT_EXPECTED, actual)
        for item in TARGETS:
            with self.subTest(piece=item["piece"]):
                number = item["midi"]
                self.assertEqual(number, name_to_number(item["kontakt"], c4_octave=3))
                self.assertEqual(item["kontakt"], note_name(number, c4_octave=3))
                self.assertEqual(number, name_to_number(item["fl"], c4_octave=5))
                self.assertEqual(item["fl"], note_name(number, c4_octave=5))
        self.assertEqual(60, name_to_number("C3", c4_octave=3))
        self.assertEqual(60, name_to_number("C5", c4_octave=5))

    def test_all_gm_notes_preserve_timing_velocity_duration_and_conductor(self):
        self.assertEqual(GM_EXPECTED, DEFAULT_MAPPING)
        hits = [(index * 160, source, 30 + index * 3, 17 + index)
                for index, source in enumerate(GM_EXPECTED)]
        conductor = [
            (0, mido.MetaMessage("track_name", name="Canción 7/8")),
            (0, mido.MetaMessage("set_tempo", tempo=500003)),
            (240, mido.MetaMessage("set_tempo", tempo=431039)),
            (1440, mido.MetaMessage("set_tempo", tempo=612347)),
            (0, mido.MetaMessage("time_signature", numerator=4, denominator=4)),
            (1920, mido.MetaMessage("time_signature", numerator=7, denominator=8,
                                  clocks_per_click=36, notated_32nd_notes_per_beat=12)),
            (0, mido.MetaMessage("key_signature", key="C")),
            (960, mido.MetaMessage("key_signature", key="Dm")),
        ]
        path = self.write_midi(hits, conductor=conductor)
        source = mido.MidiFile(path)
        result, output = self.run_conversion(path)
        self.assertEqual(1, output.type)
        self.assertEqual(480, output.ticks_per_beat)
        self.assertEqual(2, len(output.tracks))
        self.assertEqual([(tick, GM_EXPECTED[note], velocity, 0)
                          for tick, note, velocity, _ in hits], positive_notes(output.tracks[1]))
        self.assertEqual([(tick + duration, GM_EXPECTED[note], 0)
                          for tick, note, _, duration in hits], note_ends(output.tracks[1]))
        for kind in ("set_tempo", "time_signature", "key_signature"):
            self.assertEqual(timing(source.tracks[0], kind), timing(output.tracks[0], kind))
        self.assertTrue(any(msg.type == "track_name" and msg.name == "Canción 7/8"
                            for msg in output.tracks[0]))
        self.assertIn("500003", result.report)
        self.assertIn("431039", result.report)
        self.assertIn("612347", result.report)

    def test_kick_alternation_is_tick_based_inclusive_and_resets(self):
        hits = [(tick, 36 if index % 2 else 35, 100, 20)
                for index, tick in enumerate((0, 120, 240, 360, 481, 601))]
        path = self.write_midi(hits)
        _, output = self.run_conversion(path)
        self.assertEqual([23, 24, 23, 24, 23, 24], [n[1] for n in positive_notes(output.tracks[1])])

    def test_odd_ppq_threshold_never_rounds_up(self):
        # 481/4 = 120,25: separación 120 alterna; 121 reinicia.
        path = self.write_midi([(0, 36, 95, 10), (120, 36, 96, 10), (241, 36, 97, 10)], ppq=481)
        _, output = self.run_conversion(path)
        self.assertEqual(481, output.ticks_per_beat)
        self.assertEqual([(0, 23, 95, 0), (120, 24, 96, 0), (241, 23, 97, 0)],
                         positive_notes(output.tracks[1]))

    def test_disabled_alternation_and_channel_ten(self):
        path = self.write_midi([(0, 35, 100, 30), (60, 36, 101, 30)])
        _, output = self.run_conversion(path, options=Options(alternate_kicks=False, output_channel=9))
        self.assertEqual([(0, 23, 100, 9), (60, 23, 101, 9)], positive_notes(output.tracks[1]))

    def test_collapse_uses_max_velocity_and_conservative_duration(self):
        path = self.write_midi([(10, 38, 80, 100), (10, 40, 120, 35)])
        result, output = self.run_conversion(path)
        self.assertEqual([(10, 26, 120, 0)], positive_notes(output.tracks[1]))
        self.assertEqual([(110, 26, 0)], note_ends(output.tracks[1]))
        self.assertIn("colaps", result.report.lower())

    def test_overlapping_sources_with_crossed_durations_keep_exact_on_and_off_events(self):
        # MIDI no identifica qué voz se apaga: una cola FIFO no es una
        # referencia válida para distinguir estos dos orígenes tras remapear.
        path = self.write_midi([(0, 38, 90, 200), (50, 40, 110, 20)])
        result, output = self.run_conversion(path)
        self.assertEqual([(0, 26, 90, 0), (50, 26, 110, 0)], positive_notes(output.tracks[1]))
        self.assertEqual([(70, 26, 0), (200, 26, 0)], note_ends(output.tracks[1]))
        self.assertIn("solap", result.report.lower())

    def test_zero_duration_and_retrigger_keep_all_events_and_off_first(self):
        path = self.write_midi([(0, 38, 90, 10), (10, 38, 100, 0), (10, 42, 80, 0)])
        _, output = self.run_conversion(path)
        self.assertEqual([(0, 26, 90, 0), (10, 26, 100, 0), (10, 43, 80, 0)],
                         positive_notes(output.tracks[1]))
        self.assertEqual([(10, 26, 0), (10, 26, 0), (10, 43, 0)], note_ends(output.tracks[1]))
        messages = [msg for tick, msg in absolute_events(output.tracks[1])
                    if tick == 10 and msg.type in ("note_on", "note_off")]
        self.assertEqual(["note_off", "note_off", "note_off", "note_on", "note_on"],
                         [msg.type for msg in messages])

    def test_fixed_duration_is_explicit_option(self):
        path = self.write_midi([(10, 38, 91, 72), (100, 42, 92, 83)])
        _, output = self.run_conversion(path, options=Options(fixed_duration=7))
        self.assertEqual([(17, 26, 0), (107, 43, 0)], note_ends(output.tracks[1]))

    def test_velocity_zero_ends_notes_and_note_off_precedes_retrigger(self):
        drums = [
            (0, mido.Message("note_on", channel=9, note=38, velocity=99)),
            (120, mido.Message("note_on", channel=9, note=38, velocity=110)),
            (120, mido.Message("note_on", channel=9, note=38, velocity=0)),
            (240, mido.Message("note_off", channel=9, note=38)),
        ]
        path = self.write_midi(drum_extra=drums)
        _, output = self.run_conversion(path)
        self.assertEqual([(0, 26, 99, 0), (120, 26, 110, 0)], positive_notes(output.tracks[1]))
        self.assertEqual([(120, 26, 0), (240, 26, 0)], note_ends(output.tracks[1]))
        at_120 = [msg for tick, msg in absolute_events(output.tracks[1])
                  if tick == 120 and msg.type in ("note_on", "note_off")]
        self.assertTrue(at_120[0].type == "note_off" or at_120[0].velocity == 0)
        self.assertEqual("note_on", at_120[1].type)
        self.assertGreater(at_120[1].velocity, 0)

    def test_unmapped_notes_are_omitted_and_reported(self):
        path = self.write_midi([(0, 38, 90, 10), (1920, 39, 100, 40), (2040, 39, 101, 40)])
        result, output = self.run_conversion(path)
        self.assertEqual([(0, 26, 90, 0)], positive_notes(output.tracks[1]))
        self.assertIn("39", result.report)
        self.assertIn("NO_MAPEADAS", result.report.upper())

    def test_unmapped_track_keeps_original_notes_timing_velocity_and_channel(self):
        path = self.write_midi([(0, 38, 90, 10), (1920, 39, 100, 40)])
        _, output = self.run_conversion(path, options=Options(include_unmapped=True))
        unmapped = next(track for track in output.tracks
                        if any(msg.type == "track_name" and msg.name == "NO_MAPEADAS" for msg in track))
        self.assertEqual([(1920, 39, 100, 9)], positive_notes(unmapped))
        self.assertEqual([(1960, 39, 9)], note_ends(unmapped))
        self.assertEqual([(0, 26, 90, 0)], positive_notes(output.tracks[1]))

    def test_format_zero_and_copied_timing_support_fl_import(self):
        path = self.write_midi([(25, 38, 101, 40), (200, 60, 88, 19)])
        _, output = self.run_conversion(path, options=Options(format_zero=True, include_unmapped=True))
        self.assertEqual(0, output.type)
        self.assertEqual(1, len(output.tracks))
        self.assertEqual([(25, 26, 101, 0), (200, 60, 88, 9)], positive_notes(output.tracks[0]))
        self.assertEqual([(0, 500000)], [(tick, msg.tempo) for tick, msg in absolute_events(output.tracks[0])
                                       if msg.type == "set_tempo"])
        _, copied = self.run_conversion(path, options=Options(copy_timing=True))
        for kind in ("set_tempo", "time_signature"):
            self.assertEqual(timing(copied.tracks[0], kind), timing(copied.tracks[1], kind))

    def test_missing_tempo_falls_back_to_120_bpm_with_warning(self):
        path = self.write_midi([(0, 38, 100, 20)], conductor=[])
        result, output = self.run_conversion(path)
        self.assertEqual([(0, 500000)], [(tick, msg.tempo) for tick, msg in absolute_events(output.tracks[0])
                                       if msg.type == "set_tempo"])
        self.assertIn("120", result.report)

    def test_timing_duplicates_and_conflicts_across_tracks_are_normalized(self):
        conductor = [(0, mido.MetaMessage("set_tempo", tempo=500003)),
                     (0, mido.MetaMessage("set_tempo", tempo=500003)),
                     (480, mido.MetaMessage("set_tempo", tempo=400001)),
                     (0, mido.MetaMessage("time_signature", numerator=4, denominator=4))]
        extra = [(480, mido.MetaMessage("set_tempo", tempo=600007)),
                 (0, mido.MetaMessage("time_signature", numerator=4, denominator=4)),
                 (0, mido.MetaMessage("time_signature", numerator=3, denominator=8,
                                      clocks_per_click=36, notated_32nd_notes_per_beat=16)),
                 (960, mido.MetaMessage("time_signature", numerator=7, denominator=8))]
        path = self.write_midi([(0, 38, 100, 20)], conductor=conductor, drum_extra=extra)
        result, output = self.run_conversion(path)
        self.assertEqual([(0, 500003), (480, 600007)],
                         [(tick, msg.tempo) for tick, msg in absolute_events(output.tracks[0])
                          if msg.type == "set_tempo"])
        self.assertEqual([(0, 4, 4), (0, 4, 4), (0, 3, 8), (960, 7, 8)],
                         [(tick, msg.numerator, msg.denominator)
                          for tick, msg in absolute_events(output.tracks[0]) if msg.type == "time_signature"])
        source = mido.MidiFile(path)
        self.assertEqual(timing(source.tracks[0], "time_signature") + timing(source.tracks[1], "time_signature"),
                         timing(output.tracks[0], "time_signature"))
        self.assertIn("duplic", result.report.lower())
        self.assertIn("conflic", result.report.lower())

    def test_program_and_control_changes_are_removed_and_counted(self):
        extra = [(0, mido.Message("program_change", channel=9, program=8)),
                 (10, mido.Message("control_change", channel=9, control=7, value=100)),
                 (20, mido.Message("control_change", channel=9, control=64, value=0))]
        path = self.write_midi([(0, 38, 100, 20)], drum_extra=extra)
        result, output = self.run_conversion(path)
        self.assertFalse(any(msg.type in ("program_change", "control_change") for msg in output.tracks[1]))
        self.assertIn("program_change", result.report)
        self.assertIn("control_change", result.report)

    def test_multiple_candidates_require_selection_and_preserve_other_song_timing(self):
        named_drums = track_from_events([(0, mido.MetaMessage("track_name", name="Percussion extra"))]
                                       + hits_events([(100, 49, 96, 21)], channel=3))
        non_drums = track_from_events([(0, mido.MetaMessage("track_name", name="Piano")),
                                      (20, mido.Message("note_on", channel=0, note=60, velocity=95)),
                                      (90, mido.Message("note_off", channel=0, note=60)),
                                      (240, mido.MetaMessage("set_tempo", tempo=454547))])
        path = self.write_midi([(0, 38, 100, 20)], extra_tracks=[named_drums, non_drums])
        inspection = inspect_midi(path)
        self.assertEqual([1, 2], [candidate.index for candidate in inspection.candidates])
        self.assertEqual({38: 1}, inspection.candidates[0].notes)
        self.assertEqual({49: 1}, inspection.candidates[1].notes)
        with self.assertRaises(ValueError):
            convert(path)
        _, output = self.run_conversion(path, track_index=2)
        self.assertEqual([(100, 52, 96, 0)], positive_notes(output.tracks[1]))
        self.assertEqual([(0, 500000), (240, 454547)],
                         [(tick, msg.tempo) for tick, msg in absolute_events(output.tracks[0])
                          if msg.type == "set_tempo"])

    def test_instrument_name_detects_drums_without_matching_track_name(self):
        for name in (None, "Parte principal"):
            with self.subTest(track_name=name):
                path = self.write_split_drums([(0, 38, 90, 30)], [(80, 42, 80, 20)], main_name=name)
                inspection = inspect_midi(path)
                self.assertEqual([1, 2], [candidate.index for candidate in inspection.candidates])
                self.assertEqual({38: 1}, inspection.candidates[0].notes)
                self.assertEqual({42: 1}, inspection.candidates[1].notes)
                self.assertIn("instrument", inspection.candidates[0].reason.lower())

    def test_merge_drums_keeps_four_supplementary_notes_and_excludes_guitar(self):
        path = self.write_split_drums([(0, 35, 90, 30), (120, 36, 100, 30), (240, 38, 110, 45)],
                                     [(24, 38, 85, 25), (160, 42, 86, 20),
                                      (320, 49, 87, 50), (480, 51, 88, 60)])
        source = mido.MidiFile(path)
        result, output = self.run_conversion(path, options=Options(merge_drums=True))
        self.assertEqual([(0, 23, 90, 0), (24, 26, 85, 0), (120, 24, 100, 0),
                          (160, 43, 86, 0), (240, 26, 110, 0), (320, 52, 87, 0),
                          (480, 62, 88, 0)], positive_notes(output.tracks[1]))
        self.assertEqual([(30, 23, 0), (49, 26, 0), (150, 24, 0), (180, 43, 0),
                          (285, 26, 0), (370, 52, 0), (540, 62, 0)], note_ends(output.tracks[1]))
        self.assertIn("notas origen mapeadas=7", result.report.lower())
        self.assertIn("+ Drums", result.report)
        for kind in ("set_tempo", "time_signature"):
            self.assertEqual(timing(source.tracks[0], kind), timing(output.tracks[0], kind))

    def test_merge_pairs_source_notes_independently_between_tracks(self):
        # Misma nota MIDI en dos canales y dos pistas: el apagado de la pista
        # auxiliar no debe cerrar anticipadamente la nota principal.
        for supplementary_channel in (0, 1):
            with self.subTest(supplementary_channel=supplementary_channel):
                path = self.write_split_drums([(0, 38, 90, 200)], [(50, 38, 110, 20)],
                                             supplementary_channel=supplementary_channel)
                result, output = self.run_conversion(path, options=Options(merge_drums=True))
                self.assertEqual([(0, 26, 90, 0), (50, 26, 110, 0)], positive_notes(output.tracks[1]))
                self.assertEqual([(70, 26, 0), (200, 26, 0)], note_ends(output.tracks[1]))
                self.assertIn("solap", result.report.lower())

    def test_merge_same_tick_collision_collapses_across_tracks(self):
        path = self.write_split_drums([(10, 38, 80, 100)], [(10, 40, 120, 35)])
        result, output = self.run_conversion(path, options=Options(merge_drums=True))
        self.assertEqual([(10, 26, 120, 0)], positive_notes(output.tracks[1]))
        self.assertEqual([(110, 26, 0)], note_ends(output.tracks[1]))
        self.assertIn("colaps", result.report.lower())
        self.assertIn("notas origen mapeadas=2", result.report.lower())

    def test_merge_and_explicit_track_are_mutually_exclusive(self):
        path = self.write_split_drums([(0, 38, 90, 30)], [(80, 42, 80, 20)])
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            convert(path, track_index=1, options=Options(merge_drums=True))
        self.assertEqual(original, path.read_bytes())
        self.assertFalse(path.with_name(path.stem + "_Invasion.mid").exists())

    def test_type_zero_input_and_running_status(self):
        # El segundo note_on y los apagados usan running status sin byte 0x99.
        body = bytes.fromhex("00 99 26 64 78 28 6e 00 26 00 78 28 00 00 ff 2f 00")
        path = self.root / "running.mid"
        path.write_bytes(b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
                         + b"MTrk" + struct.pack(">I", len(body)) + body)
        _, output = self.run_conversion(path)
        self.assertEqual([(0, 26, 100, 0), (120, 26, 110, 0)], positive_notes(output.tracks[1]))
        self.assertEqual([(120, 26, 0), (240, 26, 0)], note_ends(output.tracks[1]))

    def test_type_zero_mixed_channels_processes_only_channel_ten(self):
        melodic = [(0, mido.Message("note_on", channel=0, note=38, velocity=120)),
                   (20, mido.Message("note_off", channel=0, note=38))]
        path = self.write_midi([(10, 38, 90, 50)], midi_type=0, drum_extra=melodic)
        inspection = inspect_midi(path)
        self.assertEqual({38: 1}, inspection.candidates[0].notes)
        result, output = self.run_conversion(path)
        self.assertEqual([(10, 26, 90, 0)], positive_notes(output.tracks[1]))
        self.assertEqual([(60, 26, 0)], note_ends(output.tracks[1]))
        self.assertIn("canal", result.report.lower())

    def test_isolated_control_on_channel_ten_does_not_hide_named_drums(self):
        extra = [(0, mido.Message("control_change", channel=9, control=7, value=80))]
        extra += hits_events([(10, 38, 90, 30)], channel=3)
        path = self.write_midi(drum_extra=extra)
        inspection = inspect_midi(path)
        self.assertEqual({38: 1}, inspection.candidates[0].notes)
        _, output = self.run_conversion(path)
        self.assertEqual([(10, 26, 90, 0)], positive_notes(output.tracks[1]))

    def test_sysex_and_unknown_meta_do_not_break_conversion_and_are_reported(self):
        extra = [(0, mido.Message("sysex", data=(0x7D, 1, 2, 3))),
                 (30, mido.UnknownMetaMessage(0x7E, data=(1, 2, 3)))]
        path = self.write_midi([(0, 38, 100, 20)], drum_extra=extra)
        result, output = self.run_conversion(path)
        self.assertEqual([(0, 26, 100, 0)], positive_notes(output.tracks[1]))
        self.assertIn("sysex", result.report.lower())
        self.assertIn("meta", result.report.lower())

    def test_smpte_and_type_two_are_explicitly_rejected(self):
        for name, midi_type, division in (("smpte", 0, 0xE728), ("type2", 2, 480)):
            with self.subTest(name=name):
                body = bytes.fromhex("00 ff 2f 00")
                path = self.root / f"{name}.mid"
                path.write_bytes(b"MThd" + struct.pack(">IHHH", 6, midi_type, 1, division)
                                 + b"MTrk" + struct.pack(">I", len(body)) + body)
                with self.assertRaises(ValueError) as caught:
                    inspect_midi(path)
                self.assertIn("SMPTE" if name == "smpte" else "2", str(caught.exception))

    def test_conversion_cannot_overwrite_original(self):
        path = self.write_midi([(0, 38, 100, 20)])
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            convert(path, output_path=path)
        self.assertEqual(original, path.read_bytes())

    def test_custom_mapping_uses_numbers_and_requires_explicit_adk(self):
        path = self.write_midi([(10, 38, 99, 20), (50, 36, 98, 20)])
        mapping_path = self.root / "custom.json"
        mapping_path.write_text(json.dumps({"mapping": {"38": 27, "36": 22}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_mapping(mapping_path)
        self.assertEqual({38: 27, 36: 22}, load_mapping(mapping_path, allow_adk=True))
        _, output = self.run_conversion(path, mapping_path=mapping_path,
                                        options=Options(allow_adk=True, alternate_kicks=False))
        self.assertEqual([(10, 27, 99, 0), (50, 22, 98, 0)], positive_notes(output.tracks[1]))

    def test_reserved_cc_and_invalid_mapping_numbers_are_rejected(self):
        mapping_path = self.root / "invalid.json"
        for mapping in ({"38": 17}, {"38": 128}, {"38": -1}, {"38": 25},
                        {"128": 26}, {"38": True}, {"38": 26.0}):
            with self.subTest(mapping=mapping):
                mapping_path.write_text(json.dumps({"mapping": mapping}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_mapping(mapping_path, allow_adk=True)

    def test_duplicate_custom_mapping_keys_are_not_silently_accepted(self):
        mapping_path = self.root / "duplicate.json"
        for content in ('{"mapping":{"38":26,"38":27}}',
                        '{"mapping":{"38":26,"038":27}}'):
            with self.subTest(content=content):
                mapping_path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_mapping(mapping_path)

    def test_reopened_file_validation_detects_corruption_and_reports_failure(self):
        # Simular que la escritura produce un archivo diferente comprueba que
        # la validación lee realmente el disco, además de comparar su modelo.
        path = self.write_midi([(10, 38, 99, 20)])
        original_save = mido.MidiFile.save
        for corruption in ("tick", "ppq", "tempo", "velocity"):
            with self.subTest(corruption=corruption):
                def corrupt_and_save(midi, filename=None, file=None):
                    if corruption == "ppq":
                        midi.ticks_per_beat += 1
                    elif corruption == "tempo":
                        next(msg for msg in midi.tracks[0] if msg.type == "set_tempo").tempo += 1
                    else:
                        note = next(msg for msg in midi.tracks[1]
                                    if msg.type == "note_on" and msg.velocity > 0)
                        if corruption == "tick":
                            note.time += 1
                        else:
                            note.velocity += 1
                    return original_save(midi, filename=filename, file=file)

                with patch.object(mido.MidiFile, "save", corrupt_and_save):
                    result = convert(path)
                self.assertFalse(result.passed)
                self.assertIn("FALLO", result.report)
                self.assertTrue(Path(result.report_path).is_file())

    def test_invalid_output_location_is_an_error(self):
        path = self.write_midi([(0, 38, 100, 20)])
        occupied = self.root / "not_a_directory"
        occupied.write_text("archivo", encoding="utf-8")
        with self.assertRaises((OSError, ValueError)):
            convert(path, output_path=occupied / "output.mid")


if __name__ == "__main__":
    unittest.main()
