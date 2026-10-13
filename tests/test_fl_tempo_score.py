"""Verificación binaria del score experimental; no acredita importación en FL."""

import csv
import struct
import tempfile
import unittest
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import mido

from fl_tempo_score import export_tempo_score
from tests.test_conversion import track_from_events


def decode_tempo_score(path):
    """Decodificador independiente de chunks y del tamaño variable FLP/FSC."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"FLhd":
        raise AssertionError("Falta FLhd")
    header_length = struct.unpack_from("<I", raw, 4)[0]
    if header_length != 6:
        raise AssertionError("Cabecera FLhd de longitud inesperada")
    file_format, channels, ppq = struct.unpack_from("<HHH", raw, 8)
    offset = 8 + header_length
    if raw[offset:offset + 4] != b"FLdt":
        raise AssertionError("Falta FLdt")
    data_length = struct.unpack_from("<I", raw, offset + 4)[0]
    data = raw[offset + 8:]
    if len(data) != data_length:
        raise AssertionError("FLdt truncado o contiene bytes sin declarar")
    records = []
    cursor = 0
    while cursor < len(data):
        event_id = data[cursor]
        cursor += 1
        if event_id < 64:
            length = 1
        elif event_id < 128:
            length = 2
        elif event_id < 192:
            length = 4
        else:
            length = 0
            shift = 0
            while True:
                if cursor >= len(data) or shift > 28:
                    raise AssertionError("Longitud variable inválida")
                byte = data[cursor]
                cursor += 1
                length |= (byte & 127) << shift
                if byte < 128:
                    break
                shift += 7
        payload = data[cursor:cursor + length]
        if len(payload) != length:
            raise AssertionError("Evento FL truncado")
        cursor += length
        if event_id == 223:
            if len(payload) % 12:
                raise AssertionError("REC no se compone de tuplas de 12 bytes")
            records.extend(struct.iter_unpack("<IIi", payload))
    return (file_format, channels, ppq), records


class FlTempoScoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_midi(self, tempos, *, ppq=480, midi_type=1, extra_tracks=()):
        midi = mido.MidiFile(type=midi_type, ticks_per_beat=ppq)
        midi.tracks.append(track_from_events([
            (tick, mido.MetaMessage("set_tempo", tempo=value)) for tick, value in tempos
        ]))
        midi.tracks.extend(extra_tracks)
        path = self.root / "tempos.mid"
        midi.save(path)
        return path

    def test_three_tempos_have_correct_binary_records_exact_tick_scaling_and_csv(self):
        tempos = [(0, 500003), (240, 431039), (1920, 612347)]
        midi_path = self.write_midi(tempos)
        original = midi_path.read_bytes()
        destination = self.root / "tempo experimental.fsc"
        result = export_tempo_score(midi_path, destination)
        self.assertEqual(destination, Path(result.path))
        self.assertEqual(3, result.point_count)
        header, records = decode_tempo_score(result.path)
        self.assertEqual((0x10, 1, 96), header)
        expected_values = [int((Decimal(60000000) / Decimal(us)).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP) * 1000) for _, us in tempos]
        self.assertEqual([(0, 0x40000005, expected_values[0]),
                          (48, 0x40000005, expected_values[1]),
                          (384, 0x40000005, expected_values[2])], records)
        self.assertEqual(original, midi_path.read_bytes())
        self.assertTrue(result.warnings, "La aproximación de BPM requiere una advertencia.")
        self.assertIn("0.001", " ".join(result.warnings))
        with Path(result.csv_path).open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(3, len(rows))
        for index, ((tick, tempo), row) in enumerate(zip(tempos, rows)):
            with self.subTest(index=index):
                self.assertEqual(str(tick), row["tick_midi"])
                self.assertEqual("480", row["source_ppq"])
                self.assertEqual(str(tick // 5), row["tick_fl"])
                self.assertEqual("96", row["fl_ppq"])
                self.assertEqual(str(tempo), row["tempo_us_original"])
                self.assertEqual(str(expected_values[index]), row["value_fl"])
                self.assertEqual(Decimal(expected_values[index]) / 1000, Decimal(row["bpm_fl"]))

    def test_type_zero_input_and_exact_millibpm_values(self):
        path = self.write_midi([(0, 500000), (480, 375000), (960, 300000)], midi_type=0)
        result = export_tempo_score(path)
        header, records = decode_tempo_score(result.path)
        self.assertEqual((0x10, 1, 96), header)
        self.assertEqual([(0, 0x40000005, 120000), (96, 0x40000005, 160000),
                          (192, 0x40000005, 200000)], records)
        self.assertEqual(3, result.point_count)
        self.assertEqual(".fsc", Path(result.path).suffix)
        self.assertNotEqual(path, Path(result.path))

    def test_many_tempos_decode_with_multibyte_event_length(self):
        path = self.write_midi([(index * 480, 500000) for index in range(14)])
        result = export_tempo_score(path)
        _, records = decode_tempo_score(result.path)
        self.assertEqual(14, result.point_count)
        self.assertEqual([(index * 96, 0x40000005, 120000) for index in range(14)], records)

    def test_identical_copied_tempos_are_allowed_but_conflicting_extra_tempos_are_rejected(self):
        tempos = [(0, 500000), (480, 400000)]
        copied = track_from_events([(tick, mido.MetaMessage("set_tempo", tempo=value))
                                    for tick, value in tempos])
        source = self.write_midi(tempos, extra_tracks=[copied])
        result = export_tempo_score(source)
        _, records = decode_tempo_score(result.path)
        self.assertEqual([(0, 0x40000005, 120000), (96, 0x40000005, 150000)], records)
        for tick, tempo in ((480, 600000), (240, 500000)):
            with self.subTest(tick=tick, tempo=tempo):
                additional = track_from_events([(tick, mido.MetaMessage("set_tempo", tempo=tempo))])
                source = self.write_midi(tempos, extra_tracks=[additional])
                destination = self.root / "tempo_conflictivo.fsc"
                with self.assertRaises(ValueError):
                    export_tempo_score(source, destination)
                self.assertFalse(destination.exists())

    def test_ticks_not_representable_at_fl_ppq_are_rejected_without_quantizing(self):
        for ppq, tempo_tick in ((480, 1), (481, 120)):
            with self.subTest(ppq=ppq, tick=tempo_tick):
                source = self.write_midi([(0, 500000), (tempo_tick, 400000)], ppq=ppq)
                original = source.read_bytes()
                destination = self.root / "sin_cuantizar.fsc"
                with self.assertRaises(ValueError) as caught:
                    export_tempo_score(source, destination)
                self.assertTrue(str(caught.exception))
                self.assertFalse(destination.exists())
                self.assertEqual(original, source.read_bytes())

    def test_midi_tempo_zero_is_rejected_with_clear_error(self):
        path = self.write_midi([(0, 0)])
        with self.assertRaises(ValueError) as caught:
            export_tempo_score(path, self.root / "invalido.fsc")
        self.assertIn("tempo", str(caught.exception).lower())
        self.assertFalse((self.root / "invalido.fsc").exists())


if __name__ == "__main__":
    unittest.main()
