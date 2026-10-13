"""Exportación experimental del tempo MIDI al formato de eventos de FL Studio.

El score contiene únicamente eventos de tempo, sin notas. Su estructura se
contrasta con scores públicos de PyFLP y los lectores de DawVert y LMMS; esa
comprobación no sustituye una importación y reproducción en FL Studio.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
import struct

import mido


REC_TEMPO = 0x40000005
VALID_FL_PPQS = (24, 48, 72, 96, 120, 144, 168, 192, 384, 768, 960)
CSV_COLUMNS = (
    "tick_midi", "source_ppq", "tick_fl", "fl_ppq", "tempo_us_original",
    "bpm_exact", "bpm_fl", "value_fl",
)


@dataclass
class TempoScoreResult:
    path: Path
    point_count: int
    warnings: list[str]
    csv_path: Path


def _varint(value: int) -> bytes:
    """Longitud VLQ de los eventos de FLP: grupos bajos de siete bits primero."""
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _decode_tempo_score(data: bytes) -> tuple[int, list[tuple[int, int]]]:
    """Lector separado del escritor, con límites y tamaños comprobados."""
    if len(data) < 22:
        raise ValueError("Score FL truncado: falta el encabezado.")
    magic, size, file_format, channels, ppq = struct.unpack_from("<4sIhHH", data)
    if (magic, size, file_format, channels) != (b"FLhd", 6, 0x10, 1):
        raise ValueError("Encabezado de score FL inválido.")
    if ppq not in VALID_FL_PPQS:
        raise ValueError("PPQ de score FL no admitido.")
    data_magic, data_size = struct.unpack_from("<4sI", data, 14)
    if data_magic != b"FLdt" or data_size != len(data) - 22:
        raise ValueError("Tamaño de datos del score FL inválido.")
    position = 22
    points = []
    controller_blocks = 0
    saw_version = False
    saw_pattern = False
    while position < len(data):
        event_id = data[position]
        position += 1
        if event_id < 192:
            length = (1, 2, 4)[event_id // 64]
        else:
            length = 0
            for shift in range(0, 35, 7):
                if position >= len(data):
                    raise ValueError("Longitud VLQ truncada en el score FL.")
                byte = data[position]
                position += 1
                length |= (byte & 127) << shift
                if not byte & 128:
                    break
            else:
                raise ValueError("Longitud VLQ excesiva en el score FL.")
        end = position + length
        if end > len(data):
            raise ValueError("Evento truncado en el score FL.")
        payload = data[position:end]
        position = end
        if event_id == 199:
            saw_version = bool(payload.endswith(b"\0"))
        elif event_id == 65:
            saw_pattern = payload == b"\0\0"
        elif event_id == 224:
            raise ValueError("El score de tempo no debe contener notas.")
        elif event_id == 223:
            controller_blocks += 1
            if not saw_pattern or length % 12:
                raise ValueError("Bloque de eventos de tempo FL inválido.")
            for tick, target, value in struct.iter_unpack("<IIi", payload):
                if target != REC_TEMPO or not 10_000 <= value <= 522_000:
                    raise ValueError("Evento ajeno al tempo o valor FL no admitido.")
                if points and tick <= points[-1][0]:
                    raise ValueError("Posiciones de tempo FL duplicadas o desordenadas.")
                points.append((tick, value))
    if not saw_version or controller_blocks != 1 or not points:
        raise ValueError("El score FL no contiene un bloque de tempo válido.")
    return ppq, points


def read_tempo_score(path) -> tuple[int, list[tuple[int, int]]]:
    """Devuelve PPQ y pares (tick, BPM multiplicados por 1000)."""
    return _decode_tempo_score(Path(path).read_bytes())


def export_tempo_score(midi_path, score_path=None, *, fl_ppq=96) -> TempoScoreResult:
    """Genera un .fsc experimental y un CSV con los tempos MIDI originales.

    Lee el conductor de la pista 0 del MIDI convertido. Las copias idénticas de
    ese tempo en otras pistas son admisibles; las diferencias se rechazan. No
    redondea posiciones temporales: si el PPQ de destino no puede representarlas,
    la exportación falla antes de escribir archivos.
    """
    if isinstance(fl_ppq, bool) or not isinstance(fl_ppq, int) or fl_ppq not in VALID_FL_PPQS:
        raise ValueError(f"PPQ de FL Studio no admitido: {fl_ppq}; usa {VALID_FL_PPQS}.")
    midi_path = Path(midi_path)
    path = Path(score_path) if score_path is not None else midi_path.with_name(
        midi_path.stem + "_tempo_FL.fsc"
    )
    if path.suffix.lower() != ".fsc":
        raise ValueError("El archivo de tempo FL debe terminar en .fsc.")
    csv_path = path.with_suffix(".csv")
    if midi_path.resolve() in (path.resolve(), csv_path.resolve()):
        raise ValueError("La exportación de tempo no puede sobrescribir el MIDI original.")
    midi = mido.MidiFile(str(midi_path))
    if midi.type not in (0, 1) or midi.ticks_per_beat <= 0 or not midi.tracks:
        raise ValueError("Se requiere MIDI 0 o 1 con PPQ positivo y una pista conductora.")
    warnings = [
        "Exportación .fsc experimental: se verifican la estructura y los valores del archivo; "
        "su importación y reproducción en FL Studio todavía no se han probado. "
        "El score contiene solo tempo y no importa notas ni cambios de compás."
    ]
    conductor = {}
    tick = 0
    duplicate_count = 0
    for message in midi.tracks[0]:
        if not isinstance(message.time, int) or message.time < 0:
            raise ValueError("El MIDI contiene delta times inválidos.")
        tick += message.time
        if message.type == "set_tempo":
            if not 1 <= message.tempo <= 0xFFFFFF:
                raise ValueError("El tempo MIDI debe ser un entero positivo de 24 bits.")
            if tick in conductor:
                duplicate_count += 1
            conductor[tick] = message.tempo
    conductor_pairs = set(conductor.items())
    for track in midi.tracks[1:]:
        tick = 0
        for message in track:
            if not isinstance(message.time, int) or message.time < 0:
                raise ValueError("El MIDI contiene delta times inválidos.")
            tick += message.time
            if message.type == "set_tempo" and (tick, message.tempo) not in conductor_pairs:
                raise ValueError(
                    "Hay tempos fuera de la pista conductora que no coinciden con la pista 0; "
                    "exporta el MIDI normalizado antes de crear el score FL."
                )
    if 0 not in conductor:
        conductor[0] = 500_000
        warnings.append("Sin tempo en tick 0: se usa el valor MIDI inicial de 120 BPM.")
    if duplicate_count:
        warnings.append(
            f"{duplicate_count} tempos duplicados en el mismo tick: prevalece el último, como en MIDI."
        )

    source_ppq = midi.ticks_per_beat
    points = []
    rows = []
    precision_loss = False
    with localcontext() as context:
        context.prec = 50
        for tick_midi, tempo_us in sorted(conductor.items()):
            tick_fl, remainder = divmod(tick_midi * fl_ppq, source_ppq)
            if remainder:
                raise ValueError(
                    f"El tempo en tick MIDI {tick_midi} (PPQ {source_ppq}) no se puede representar "
                    f"exactamente con PPQ FL {fl_ppq}; no se redondean ticks."
                )
            if tick_fl > 0xFFFFFFFF:
                raise ValueError("La posición de tempo excede el campo de 32 bits del score FL.")
            bpm = Decimal(60_000_000) / Decimal(tempo_us)
            if not Decimal(10) <= bpm <= Decimal(522):
                raise ValueError(f"Tempo {bpm} BPM fuera del rango FL del score: 10 a 522 BPM.")
            bpm_fl = bpm.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            value_fl = int(bpm_fl * 1000)
            precision_loss |= bpm != bpm_fl
            points.append((tick_fl, value_fl))
            rows.append((
                tick_midi, source_ppq, tick_fl, fl_ppq, tempo_us,
                format(bpm, "f"), format(bpm_fl, ".3f"), value_fl,
            ))
    if precision_loss:
        warnings.append(
            "FL almacena el tempo en pasos de 0.001 BPM: algunos BPM se redondean a esa precisión. "
            "El MIDI y el CSV conservan los microsegundos por negra originales; "
            "la columna bpm_exact usa hasta 50 cifras significativas."
        )

    # Estructura de metadatos del score vacío público de PyFLP, FL 20.8.4.2576.
    # No se copian notas, música ni un proyecto o plantilla de terceros.
    version = b"20.8.4.2576\0"
    body = b"\xc7" + _varint(len(version)) + version
    body += b"\x9f" + struct.pack("<I", 2576)
    body += b"\x1c\x01\x25\x01\x41\x00\x00"
    payload = b"".join(struct.pack("<IIi", tick_fl, REC_TEMPO, value) for tick_fl, value in points)
    body += b"\xdf" + _varint(len(payload)) + payload
    score = struct.pack("<4sIhHH", b"FLhd", 6, 0x10, 1, fl_ppq)
    score += struct.pack("<4sI", b"FLdt", len(body)) + body
    if _decode_tempo_score(score) != (fl_ppq, points):
        raise ValueError("La verificación interna del score FL ha fallado.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(score)
    if read_tempo_score(path) != (fl_ppq, points):
        raise ValueError("La verificación del score FL guardado ha fallado.")
    with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)
    return TempoScoreResult(path, len(points), warnings, csv_path)
