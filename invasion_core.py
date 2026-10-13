"""Conversión MIDI en ticks enteros, independiente de la interfaz y la CLI."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
import unicodedata

import mido

from invasion_mapping import GM_NAMES, KICK_SOURCES, TARGETS, ensure_mapping, load_mapping, note_name


@dataclass
class Options:
    output_channel: int = 0
    alternate_kicks: bool = True
    allow_adk: bool = False
    format_zero: bool = False
    copy_timing: bool = False
    include_unmapped: bool = False
    fixed_duration: int | None = None
    octave_offset: int = 2
    merge_drums: bool = False


@dataclass
class Candidate:
    index: int
    name: str
    reason: str
    notes: dict[int, int]


@dataclass
class Inspection:
    midi: mido.MidiFile
    candidates: list[Candidate]
    warnings: list[str]


@dataclass
class ConversionResult:
    output_path: Path
    report_path: Path
    report: str
    passed: bool


@dataclass
class _Note:
    tick: int
    end: int
    note: int
    velocity: int
    channel: int
    order: int
    off_velocity: int = 0
    source_track: int = 0


@dataclass
class _Hit:
    tick: int
    end: int
    note: int
    velocity: int
    channel: int
    off_velocity: int = 0
    sources: list[_Note] = field(default_factory=list)


def _absolute(track):
    tick = 0
    for order, message in enumerate(track):
        if not isinstance(message.time, int) or message.time < 0:
            raise ValueError("El archivo contiene delta times inválidos; se requieren ticks enteros no negativos.")
        tick += message.time
        yield tick, order, message


def _channel_nine(track) -> bool:
    return any(message.type in ("note_on", "note_off")
               and message.channel == 9 for message in track)


def _track_name(track, index: int) -> str:
    for kind in ("track_name", "instrument_name"):
        name = next((message.name for message in track
                     if message.type == kind and message.name), None)
        if name:
            return name
    return f"Pista {index}"


def _drum_name(name: str) -> bool:
    normalized = "".join(c for c in unicodedata.normalize("NFKD", name.lower())
                         if not unicodedata.combining(c))
    return any(part in normalized for part in ("drum", "bateria", "percussion"))


def inspect_midi(path) -> Inspection:
    """Inventaría la batería sin alterar el archivo y rechaza tiempos incompatibles."""
    midi = mido.MidiFile(str(path))
    # La división SMPTE es un entero con signo en el encabezado MIDI de mido.
    if midi.ticks_per_beat <= 0:
        raise ValueError("División SMPTE no compatible: se requiere MIDI con PPQ positivo.")
    if midi.type == 2:
        raise ValueError("MIDI formato 2 no compatible: sus pistas tienen líneas temporales independientes.")
    if midi.type not in (0, 1):
        raise ValueError(f"Formato MIDI {midi.type} no compatible; se admiten 0 y 1.")
    candidates = []
    for index, track in enumerate(midi.tracks):
        name = _track_name(track, index)
        named_track = any(message.type == "track_name" and _drum_name(message.name)
                          for message in track)
        named_instrument = any(message.type == "instrument_name" and _drum_name(message.name)
                               for message in track)
        named = named_track or named_instrument
        channel_nine = _channel_nine(track)
        if channel_nine or named:
            notes = Counter(message.note for message in track
                            if message.type == "note_on" and message.velocity > 0
                            and (not channel_nine or message.channel == 9))
            reason = " / ".join(part for condition, part in (
                (channel_nine, "canal MIDI 10"),
                (named_track, "nombre de batería"),
                (named_instrument, "nombre de instrumento de batería")) if condition)
            candidates.append(Candidate(index, name, reason, dict(sorted(notes.items()))))
    warnings = []
    if not any(message.type == "set_tempo" for track in midi.tracks for message in track):
        warnings.append("No hay eventos de tempo: se añade 120 BPM (500000 µs/negra) en tick 0.")
    return Inspection(midi, candidates, warnings)


def _read_source_notes(track, only_nine: bool, warnings: list[str]):
    """Empareja por canal/nota FIFO; velocity 0 siempre cierra una nota."""
    active = defaultdict(deque)
    early_offs = defaultdict(deque)
    notes = []
    consumed = set()
    last_tick = 0
    for tick, order, message in _absolute(track):
        last_tick = tick
        if message.type not in ("note_on", "note_off"):
            continue
        if only_nine and message.channel != 9:
            continue
        key = message.channel, message.note
        is_on = message.type == "note_on" and message.velocity > 0
        if is_on:
            note = _Note(tick, tick, message.note, message.velocity, message.channel, order)
            consumed.add(order)
            # Una nota de duración cero escrita con off antes de on es legal y no se mueve.
            while early_offs[key] and early_offs[key][0][0] < tick:
                early_offs[key].popleft()
            if early_offs[key] and early_offs[key][0][0] == tick:
                _, off_order, off_velocity = early_offs[key].popleft()
                note.off_velocity = off_velocity
                consumed.add(off_order)
                notes.append(note)
            else:
                active[key].append(note)
        elif active[key]:
            note = active[key].popleft()
            note.end = tick
            note.off_velocity = message.velocity
            notes.append(note)
            consumed.add(order)
        else:
            early_offs[key].append((tick, order, message.velocity))
    unfinished = 0
    for queue in active.values():
        for note in queue:
            note.end = max(note.tick, last_tick)
            notes.append(note)
            unfinished += 1
    if unfinished:
        warnings.append(f"{unfinished} note_on sin cierre: se añade note_off al final de su pista (tick {last_tick}); la duración original no estaba definida.")
    # También cuentan los offs antiguos retirados al encontrar un on posterior.
    orphan_count = sum(1 for _, order, message in _absolute(track)
                       if message.type in ("note_off", "note_on")
                       and (message.type == "note_off" or message.velocity == 0)
                       and (not only_nine or message.channel == 9) and order not in consumed)
    if orphan_count:
        warnings.append(f"{orphan_count} note_off sin note_on asociado se descartan y figuran en el inventario de descartes.")
    zero_lengths = sum(note.end == note.tick for note in notes)
    if zero_lengths:
        warnings.append(f"{zero_lengths} notas de duración cero se conservan exactamente; se ordena note_off antes de note_on en ese tick.")
    return sorted(notes, key=lambda n: (n.tick, n.order)), consumed, last_tick


def _normalize(messages, kind: str, warnings: list[str]):
    by_tick = defaultdict(list)
    for tick, sequence, message in messages:
        if message.type == kind:
            by_tick[tick].append((sequence, message))
    result = []
    duplicate_count = 0
    conflict_count = 0
    for tick in sorted(by_tick):
        values = by_tick[tick]
        signatures = [tuple(sorted(message.copy(time=0).dict().items()))
                      for _, message in values]
        duplicate_count += len(signatures) - len(set(signatures))
        if len(set(signatures)) > 1:
            conflict_count += len(set(signatures)) - 1
            warnings.append(f"Conflicto {kind} en tick {tick}: se conserva el último evento según orden de pistas y archivo.")
        result.append((tick, values[-1][1].copy(time=0)))
    if duplicate_count:
        warnings.append(f"{kind}: se eliminan {duplicate_count} duplicados exactos (mismo tick y todos los valores).")
    return result, duplicate_count, conflict_count


def _fraction_text(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _position(tick: int, ppq: int, signatures) -> str:
    """Compás:tiempo racional exacto; un cambio parcial inicia un compás nuevo."""
    start, bar, numerator, denominator = 0, 1, 4, 4
    for change_tick, message in signatures:
        if change_tick > tick:
            break
        if numerator == 0 or message.numerator == 0:
            return "indefinido (numerador de compás 0)"
        ticks_per_bar = Fraction(ppq * 4 * numerator, denominator)
        bars = Fraction(change_tick - start, 1) / ticks_per_bar
        bar += -(-bars.numerator // bars.denominator)
        start, numerator, denominator = change_tick, message.numerator, message.denominator
    beat_ticks = Fraction(ppq * 4, denominator)
    ticks_per_bar = beat_ticks * numerator
    elapsed = Fraction(tick - start, 1)
    full_bars = elapsed // ticks_per_bar
    beat = 1 + (elapsed - full_bars * ticks_per_bar) / beat_ticks
    return f"{bar + full_bars}:{_fraction_text(beat)}"


def _track_from_events(events, end_tick: int) -> mido.MidiTrack:
    """Orden estable: metadatos, off, on. Nunca usa segundos ni cuantización."""
    def priority(message):
        if message.type == "note_off" or (message.type == "note_on" and message.velocity == 0):
            return 1
        if message.type == "note_on":
            return 2
        return 0
    ordered = sorted(enumerate(events), key=lambda item: (
        item[1][0], priority(item[1][1]), item[0]))
    track = mido.MidiTrack()
    previous = 0
    for _, (tick, message) in ordered:
        track.append(message.copy(time=tick - previous))
        previous = tick
    track.append(mido.MetaMessage("end_of_track", time=max(previous, end_tick) - previous))
    return track


def _note_events(hits):
    events = []
    for hit in hits:
        events.append((hit.tick, mido.Message("note_on", channel=hit.channel,
                                             note=hit.note, velocity=hit.velocity)))
        events.append((hit.end, mido.Message("note_off", channel=hit.channel,
                                            note=hit.note, velocity=hit.off_velocity)))
    return events


def _written_notes(track):
    """Decodificador de validación: lee solo el MIDI escrito, sin aplicar el mapa."""
    started = defaultdict(deque)
    before_on = defaultdict(deque)
    found, errors = [], []
    absolute = 0
    for message in track:
        absolute += message.time
        if message.type not in ("note_on", "note_off"):
            continue
        key = message.channel, message.note
        if message.type == "note_on" and message.velocity:
            while before_on[key] and before_on[key][0] < absolute:
                errors.append(f"note_off huérfano en salida: canal {key[0] + 1}, nota {key[1]}.")
                before_on[key].popleft()
            if before_on[key] and before_on[key][0] == absolute:
                before_on[key].popleft()
                found.append((absolute, key[1], message.velocity, 0, key[0]))
            else:
                started[key].append((absolute, message.velocity))
        elif started[key]:
            onset, velocity = started[key].popleft()
            found.append((onset, key[1], velocity, absolute - onset, key[0]))
        else:
            before_on[key].append(absolute)
    for key, values in started.items():
        if values:
            errors.append(f"{len(values)} note_on sin cierre en salida, nota {key[1]}.")
    for key, values in before_on.items():
        if values:
            errors.append(f"{len(values)} note_off huérfanos en salida, nota {key[1]}.")
    return sorted(found), errors


def _signature_tuple(tick, message):
    return (tick, message.numerator, message.denominator,
            message.clocks_per_click, message.notated_32nd_notes_per_beat)


def _validate_note_events(track, expected_hits, label, failures, warnings):
    """Compara ataques y cierres por separado: MIDI no identifica notas solapadas."""
    actual_ons, actual_offs = Counter(), Counter()
    for tick, _, message in _absolute(track):
        if message.type == "note_on" and message.velocity:
            actual_ons[tick, message.note, message.velocity, message.channel] += 1
        elif message.type == "note_off" or (message.type == "note_on" and not message.velocity):
            actual_offs[tick, message.note, message.velocity, message.channel] += 1
    expected_ons = Counter((hit.tick, hit.note, hit.velocity, hit.channel) for hit in expected_hits)
    expected_offs = Counter((hit.end, hit.note, hit.off_velocity, hit.channel) for hit in expected_hits)
    if actual_ons != expected_ons:
        failures.append(f"{label}: ataques (tick, nota, velocidad, canal) distintos del origen con las transformaciones declaradas.")
    if actual_offs != expected_offs:
        failures.append(f"{label}: cierres (tick, nota, velocidad off, canal) distintos; duraciones no conservadas.")
    actual_pairs, errors = _written_notes(track)
    failures.extend(errors)
    expected_pairs = Counter((hit.tick, hit.note, hit.velocity,
                              hit.end - hit.tick, hit.channel) for hit in expected_hits)
    if Counter(actual_pairs) != expected_pairs and actual_ons == expected_ons and actual_offs == expected_offs:
        warnings.append(f"{label}: hay solapamientos con cierres cruzados de la misma nota/canal. MIDI no identifica qué ataque cierra cada off; el emparejamiento FIFO interpreta duraciones ambiguas. Se conservaron exactamente todos los ticks de ataque/cierre y velocidades; la validación aprueba esos eventos, no una interpretación exclusiva de cada duración por el reproductor.")


def _validate(path, original, tempos, signatures, hits, unmapped_hits,
              options: Options, mapped_source_count, collapsed_count, key_events, warnings):
    failures = []
    try:
        written = mido.MidiFile(str(path))
    except (OSError, ValueError, EOFError) as exc:
        return False, [f"No se pudo reabrir el MIDI escrito: {exc}"]
    expected_type = 0 if options.format_zero else 1
    expected_track_count = 1 if options.format_zero else 2 + int(options.include_unmapped)
    if written.type != expected_type:
        failures.append(f"Formato de salida incorrecto: {written.type}; se esperaba {expected_type}.")
    if len(written.tracks) != expected_track_count:
        failures.append(f"Número de pistas de salida incorrecto: {len(written.tracks)}; se esperaba {expected_track_count}.")
        return False, failures
    if written.ticks_per_beat != original.ticks_per_beat:
        failures.append("PPQ distinto del original.")
    # El conductor sigue siendo autoridad incluso si se duplican sus eventos en batería.
    conductor_events = list(_absolute(written.tracks[0]))
    actual_tempos = [(tick, message.tempo) for tick, _, message in conductor_events
                     if message.type == "set_tempo"]
    actual_signatures = [_signature_tuple(tick, message) for tick, _, message in conductor_events
                         if message.type == "time_signature"]
    if actual_tempos != [(tick, message.tempo) for tick, message in tempos]:
        failures.append("Los eventos de tempo escritos difieren del original normalizado.")
    if actual_signatures != [_signature_tuple(tick, message) for tick, message in signatures]:
        failures.append("Los eventos de compás escritos difieren del original.")
    actual_keys = [(tick, message.key) for tick, _, message in conductor_events
                   if message.type == "key_signature"]
    if actual_keys != [(tick, message.key) for tick, message in key_events]:
        failures.append("Los eventos de tonalidad escritos difieren del original.")
    note_track = written.tracks[0 if options.format_zero else 1]
    expected = list(hits)
    # En formato 0 las no mapeadas deben identificarse por el multiconjunto completo.
    if options.format_zero and options.include_unmapped:
        expected += unmapped_hits
    _validate_note_events(note_track, expected, "Batería", failures, warnings)
    if mapped_source_count != len(hits) + collapsed_count:
        failures.append("El recuento de notas mapeadas no coincide con notas destino + colapsos.")
    allowed = {int(item["midi"]) for item in TARGETS} - {17}
    if not options.allow_adk:
        allowed.discard(22)
    if any(hit.note not in allowed for hit in hits):
        failures.append("La batería contiene notas fuera del conjunto Invasion permitido.")
    if options.include_unmapped and not options.format_zero:
        _validate_note_events(written.tracks[2], unmapped_hits, "NO_MAPEADAS", failures, warnings)
    if options.copy_timing and not options.format_zero:
        copied = list(_absolute(written.tracks[1]))
        if [(tick, message.tempo) for tick, _, message in copied if message.type == "set_tempo"] != actual_tempos:
            failures.append("La copia de tempos en batería no coincide con conductor.")
        if [_signature_tuple(tick, message) for tick, _, message in copied if message.type == "time_signature"] != actual_signatures:
            failures.append("La copia de compases en batería no coincide con conductor.")
    return not failures, failures


def convert(input_path, output_path=None, *, track_index=None, mapping_path=None,
            options=None) -> ConversionResult:
    """Convierte, reabre, valida y entrega un reporte español junto al MIDI."""
    options = options or Options()
    if isinstance(options.output_channel, bool) or not isinstance(options.output_channel, int) or options.output_channel not in range(16):
        raise ValueError("El canal de salida debe estar entre 0 y 15 (canales MIDI 1 a 16).")
    if options.fixed_duration is not None and (
            isinstance(options.fixed_duration, bool) or not isinstance(options.fixed_duration, int)
            or options.fixed_duration < 1):
        raise ValueError("La duración fija debe ser un entero positivo de ticks.")
    if isinstance(options.octave_offset, bool) or not isinstance(options.octave_offset, int):
        raise ValueError("OCTAVE_OFFSET debe ser un entero.")
    input_path = Path(input_path).expanduser().resolve()
    inspection = inspect_midi(input_path)
    midi = inspection.midi
    warnings = list(inspection.warnings)
    if options.merge_drums and track_index is not None:
        raise ValueError("Fusionar todas las baterías (--merge-drums) no se puede combinar con una pista explícita (--track).")
    if options.merge_drums:
        selected_indices = [candidate.index for candidate in inspection.candidates if candidate.notes]
        if not selected_indices:
            raise ValueError("No se detectó ninguna pista de batería con notas para fusionar; se buscan canal MIDI 10 y nombres de pista/instrumento de batería.")
    elif track_index is None:
        if len(inspection.candidates) != 1:
            choices = "; ".join(f"{candidate.index}: {candidate.name} ({candidate.reason})"
                                for candidate in inspection.candidates)
            if choices:
                raise ValueError("Hay varias pistas candidatas; elige una con --track ÍNDICE: " + choices)
            raise ValueError("No se detectó batería por canal MIDI 10 o nombre. Selecciona explícitamente una pista con --track ÍNDICE.")
        track_index = inspection.candidates[0].index
    if not options.merge_drums:
        if isinstance(track_index, bool) or not isinstance(track_index, int) or not 0 <= track_index < len(midi.tracks):
            raise ValueError(f"Índice de pista inválido: {track_index}; el archivo tiene {len(midi.tracks)} pistas.")
        selected_indices = [track_index]
    mapping_path = Path(mapping_path) if mapping_path else Path(__file__).with_name("mapping_invasion.json")
    ensure_mapping(mapping_path)
    mapping = load_mapping(mapping_path, allow_adk=options.allow_adk)
    output_path = Path(output_path).expanduser().resolve() if output_path else input_path.with_name(input_path.stem + "_Invasion.mid")
    if output_path == input_path or (output_path.exists() and output_path.samefile(input_path)):
        raise ValueError("La salida debe ser distinta del MIDI original para conservarlo.")
    report_path = output_path.with_name(input_path.stem + "_reporte.txt")
    if report_path == input_path or (report_path.exists() and report_path.samefile(input_path)):
        raise ValueError("La ruta del reporte no puede sobrescribir el MIDI original.")
    if any(path.resolve() == mapping_path.resolve()
           or (path.exists() and path.samefile(mapping_path))
           for path in (output_path, report_path)):
        raise ValueError("La salida y el reporte no pueden sobrescribir el mapping JSON editable.")
    notes = []
    notes_by_track = {}
    consumed_note_events = {}
    selection_filters = {}
    selected_end = 0
    for index in selected_indices:
        track = midi.tracks[index]
        only_nine = _channel_nine(track)
        selection_filters[index] = only_nine
        track_warnings = []
        track_notes, consumed, track_end = _read_source_notes(track, only_nine, track_warnings)
        warnings.extend(f"Pista {index} ({_track_name(track, index)}): {warning}"
                        for warning in track_warnings)
        for note in track_notes:
            note.source_track = index
        notes.extend(track_notes)
        notes_by_track[index] = track_notes
        consumed_note_events[index] = consumed
        selected_end = max(selected_end, track_end)
        if only_nine:
            other_channels = sorted({message.channel + 1 for message in track
                                     if hasattr(message, "channel") and message.channel != 9})
            if other_channels:
                warnings.append(f"La pista {index} contiene canal 10: solo se procesa su batería; eventos de los canales {other_channels} se descartan y se cuentan por tipo.")
    # Cada pista se empareja antes de mezclar; así un off nunca cierra otra pista.
    notes.sort(key=lambda note: (note.tick, note.source_track, note.order))
    if options.merge_drums:
        warnings.append(f"Se fusionan {len(selected_indices)} pistas de batería con notas: {', '.join(str(index) for index in selected_indices)}. Emparejamiento note_on/off FIFO independiente por pista, canal y nota; orden para eventos simultáneos: índice de pista y orden original. Se colapsan coincidencias destino/tick también entre pistas.")
    inventory = Counter(note.note for note in notes)
    all_events = []
    song_end = 0
    sequence = 0
    first_song_name = None
    key_events = []
    for index, source_track in enumerate(midi.tracks):
        for tick, order, message in _absolute(source_track):
            song_end = max(song_end, tick)
            all_events.append((tick, sequence, message))
            if message.type == "track_name" and first_song_name is None and message.name:
                first_song_name = (index, order, message.name)
            if message.type == "key_signature":
                key_events.append((tick, message.copy(time=0)))
            sequence += 1
    tempos, tempo_duplicates, tempo_conflicts = _normalize(all_events, "set_tempo", warnings)
    signatures = [(tick, message.copy(time=0))
                  for tick, _, message in sorted(all_events, key=lambda item: item[0])
                  if message.type == "time_signature"]
    if len(set(_signature_tuple(tick, message) for tick, message in signatures)) < len(signatures):
        warnings.append("Compases duplicados exactos: se conservan todos los time_signature del original, incluyendo repetidos.")
    signature_values = defaultdict(set)
    for tick, message in signatures:
        signature_values[tick].add(_signature_tuple(tick, message)[1:])
    if any(len(values) > 1 for values in signature_values.values()):
        warnings.append("Hay compases distintos en el mismo tick: se conservan todos en orden de pistas/archivo; el último es efectivo para las posiciones informativas.")
    if not tempos:
        tempos = [(0, mido.MetaMessage("set_tempo", tempo=500000))]
    if any(message.tempo == 0 for _, message in tempos):
        warnings.append("Hay tempo 0 µs/negra: se conserva exactamente, pero BPM no está definido y el archivo de entrada no tiene un tempo físicamente válido.")
    if any(message.numerator == 0 for _, message in signatures):
        warnings.append("Hay compás con numerador 0: se conserva exactamente; las posiciones compás:tiempo afectadas no se pueden definir.")
    key_events.sort(key=lambda item: item[0])
    conductor_events = [(0, mido.MetaMessage("track_name", name=(first_song_name[2] if first_song_name else input_path.stem)))]
    conductor_events += tempos + signatures + key_events
    warnings.append("Compás:tiempo usa fracciones exactas del pulso del denominador; si un cambio de compás cae a mitad de compás, inicia un nuevo compás para el reporte (los ticks no cambian).")
    mapped = []
    unmapped = []
    previous_kick_tick = None
    previous_kick_destination = 24
    for note in notes:
        destination = mapping.get(note.note)
        if destination is None:
            unmapped.append(note)
            continue
        if options.alternate_kicks and note.note in KICK_SOURCES and destination in (23, 24):
            if previous_kick_tick is None or (note.tick - previous_kick_tick) * 4 > midi.ticks_per_beat:
                destination = 23
            else:
                destination = 24 if previous_kick_destination == 23 else 23
            previous_kick_tick = note.tick
            previous_kick_destination = destination
        end = note.tick + options.fixed_duration if options.fixed_duration is not None else note.end
        mapped.append(_Hit(note.tick, end, destination, max(1, min(127, note.velocity)),
                           options.output_channel, note.off_velocity, [note]))
    grouped = defaultdict(list)
    for hit in mapped:
        grouped[hit.tick, hit.note].append(hit)
    hits, collisions = [], []
    for key in sorted(grouped):
        group = grouped[key]
        longest = max(group, key=lambda hit: hit.end)
        hit = _Hit(key[0], max(item.end for item in group), key[1],
                   max(item.velocity for item in group), options.output_channel,
                   longest.off_velocity, [source for item in group for source in item.sources])
        hits.append(hit)
        if len(group) > 1:
            collisions.append(hit)
    collapsed_count = sum(len(hit.sources) - 1 for hit in collisions)
    if collisions:
        warnings.append(f"{collapsed_count} notas se colapsan en {len(collisions)} grupos destino/tick; se conserva velocidad máxima y duración más larga (criterio conservador explícito).")
    if options.fixed_duration is not None:
        warnings.append(f"Duración fija activada: cada nota mapeada dura {options.fixed_duration} ticks; posiciones y velocidades se conservan. NO_MAPEADAS conserva su duración original.")
    if options.allow_adk:
        warnings.append("ADK (22) está permitido explícitamente; solo se genera si el JSON lo asigna.")
    if options.format_zero and options.include_unmapped and unmapped:
        warnings.append("Formato 0 solo admite una pista: NO_MAPEADAS se mezcla con las notas de batería conservando canal y nota origen; puede disparar sonidos ajenos en Invasion. Usa formato 1 para mantenerla separada.")
    unmapped_hits = [_Hit(note.tick, note.end, note.note, note.velocity, note.channel,
                          note.off_velocity, [note]) for note in unmapped]
    drum_events = [(0, mido.MetaMessage("track_name", name="Batería Invasion"))] + _note_events(hits)
    if options.copy_timing and not options.format_zero:
        drum_events += tempos + signatures
    elif options.copy_timing and options.format_zero:
        warnings.append("Formato 0: tempo/compás ya están en la única pista; no se añaden copias duplicadas.")
    output = mido.MidiFile(type=0 if options.format_zero else 1, ticks_per_beat=midi.ticks_per_beat)
    if options.format_zero:
        combined = conductor_events + _note_events(hits)
        if options.include_unmapped:
            combined += _note_events(unmapped_hits)
        output.tracks.append(_track_from_events(combined, max(song_end, max((hit.end for hit in hits), default=0))))
    else:
        output.tracks.append(_track_from_events(conductor_events, song_end))
        output.tracks.append(_track_from_events(drum_events, max(selected_end, max((hit.end for hit in hits), default=0))))
        if options.include_unmapped:
            output.tracks.append(_track_from_events(
                [(0, mido.MetaMessage("track_name", name="NO_MAPEADAS"))] + _note_events(unmapped_hits), selected_end))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output.save(str(output_path))
    passed, failures = _validate(output_path, midi, tempos, signatures, hits, unmapped_hits,
                                 options, len(mapped), collapsed_count, key_events, warnings)
    targets = {int(item["midi"]): item for item in TARGETS}
    selected_text = "; ".join(f"{index} — {_track_name(midi.tracks[index], index)}"
                              for index in selected_indices)
    selection_text = ("fusionar todas las candidatas de batería con notas"
                      if options.merge_drums else "una pista de batería")
    lines = ["SONGSTERR → GGD INVASION — REPORTE", "=" * 72,
             f"Entrada: {input_path}", f"Salida: {output_path}",
             f"Mapa editable: {mapping_path.resolve()}",
             f"{'Pistas elegidas (índices desde 0)' if options.merge_drums else 'Pista elegida (índice desde 0)'}: {selected_text}",
             f"Selección: {selection_text}",
             f"Formato entrada/salida: {midi.type}/{output.type}; PPQ original/salida: {midi.ticks_per_beat}/{output.ticks_per_beat}",
             f"Canal de batería: {options.output_channel + 1}; alternancia: {'sí' if options.alternate_kicks else 'no'}; ADK: {'permitido por JSON' if options.allow_adk else 'prohibido'}",
             f"Nombres: Kontakt C3 = MIDI 60; FL C{3 + options.octave_offset} = MIDI 60; OCTAVE_OFFSET={options.octave_offset}. Los números MIDI mandan.",
             "", "PISTAS PROCESADAS ANTES DE CONVERTIR", "Índice | Nombre | Filtro | Notas origen | Mapeadas | No mapeadas"]
    for index in selected_indices:
        track_notes = notes_by_track[index]
        mapped_count = sum(note.note in mapping for note in track_notes)
        lines.append(f"{index} | {_track_name(midi.tracks[index], index)} | {'solo canal MIDI 10' if selection_filters[index] else 'todos los canales de esta pista'} | {len(track_notes)} | {mapped_count} | {len(track_notes) - mapped_count}")
    lines += ["", "INVENTARIO GM ANTES DE CONVERTIR", "MIDI | Nombre GM | Cantidad"]
    lines += [f"{number} | {GM_NAMES.get(number, 'Fuera del mapa GM estándar')} | {inventory[number]}"
              for number in sorted(inventory)] or ["(sin notas)"]
    lines += ["", "MAPEO APLICADO", "Origen GM | Cantidad | Pieza Invasion | MIDI | Kontakt | FL"]
    for source in sorted(inventory):
        destinations = sorted({hit.note for hit in hits if any(note.note == source for note in hit.sources)})
        if not destinations:
            lines.append(f"{source} {GM_NAMES.get(source, 'GM desconocido')} | {inventory[source]} | NO MAPEADA | — | — | —")
        for destination in destinations:
            target = targets[destination]
            lines.append(f"{source} {GM_NAMES.get(source, 'GM desconocido')} | {inventory[source]} | {target['piece']} | {destination} | {note_name(destination, c4_octave=3)} | {note_name(destination, c4_octave=3 + options.octave_offset)}")
    lines += ["", "NOTAS NO_MAPEADAS", "MIDI | Nombre GM | Cantidad | Primer tick | Primera aparición compás:tiempo"]
    unmapped_by_note = defaultdict(list)
    for note in unmapped:
        unmapped_by_note[note.note].append(note)
    if unmapped:
        lines.append("Se conservan en NO_MAPEADAS (canales originales)." if options.include_unmapped
                     else "Se omiten del MIDI de salida; todas están inventariadas aquí.")
    for number in sorted(unmapped_by_note):
        group = unmapped_by_note[number]
        first_tick = min(note.tick for note in group)
        lines.append(f"{number} | {GM_NAMES.get(number, 'Fuera del mapa GM estándar')} | {len(group)} | {first_tick} | {_position(first_tick, midi.ticks_per_beat, signatures)}")
    if not unmapped:
        lines.append("Ninguna.")
    lines += ["", "TEMPOS (VALORES EXACTOS; BPM SOLO INFORMATIVO)",
              "Nº | Tick | Compás:tiempo | µs/negra | BPM"]
    for index, (tick, message) in enumerate(tempos, 1):
        bpm_text = f"{Decimal(60000000) / Decimal(message.tempo):.3f}" if message.tempo else "no definido"
        lines.append(f"{index} | {tick} | {_position(tick, midi.ticks_per_beat, signatures)} | {message.tempo} | {bpm_text}")
    lines += ["", "COMPASES", "Nº | Tick | Compás:tiempo | Numerador/denominador | clocks_per_click | notated_32nd_notes_per_beat"]
    for index, (tick, message) in enumerate(signatures, 1):
        lines.append(f"{index} | {tick} | {_position(tick, midi.ticks_per_beat, signatures)} | {message.numerator}/{message.denominator} | {message.clocks_per_click} | {message.notated_32nd_notes_per_beat}")
    if not signatures:
        lines.append("Sin eventos de compás; 4/4 implícito usado solo para posiciones del reporte.")
    lines += ["", "COLAPSOS DECLARADOS", "Tick | MIDI destino | Kontakt | FL | Orígenes (pista, MIDI, velocidad, duración ticks) | Velocidad final | Duración final"]
    for hit in collisions:
        source_text = "; ".join(f"(pista {note.source_track}, {note.note}, {note.velocity}, {note.end - note.tick})" for note in hit.sources)
        lines.append(f"{hit.tick} | {hit.note} | {note_name(hit.note, c4_octave=3)} | {note_name(hit.note, c4_octave=3 + options.octave_offset)} | {source_text} | {hit.velocity} | {hit.end - hit.tick}")
    if not collisions:
        lines.append("Ninguno.")
    lines += ["", "EVENTOS CONSERVADOS, TRANSFORMADOS Y DESCARTADOS",
              "Se conserva el nombre de canción del primer track_name no vacío; la pista de batería se renombra.",
              f"Conductor: {len(tempos)} tempos, {len(signatures)} compases, {len(key_events)} key_signature (ticks/valores exactos).",
              f"Normalización de tempos: duplicados eliminados={tempo_duplicates}, conflictos resueltos={tempo_conflicts}; todos los eventos de compás se conservan.",
              "Todos los end_of_track se regeneran; se conserva el final global y el final de las pistas procesadas."]
    for index, source_track in enumerate(midi.tracks):
        discarded = Counter()
        transformed = Counter()
        channels = Counter()
        for tick, order, message in _absolute(source_track):
            if message.type in ("set_tempo", "time_signature", "key_signature", "end_of_track"):
                continue
            if first_song_name and (index, order) == first_song_name[:2]:
                continue
            if order in consumed_note_events.get(index, set()):
                number = message.note
                if number in mapping:
                    transformed[message.type] += 1
                    continue
                if options.include_unmapped:
                    transformed["NO_MAPEADAS:" + message.type] += 1
                    continue
            discarded[message.type] += 1
            if hasattr(message, "channel"):
                channels[message.channel + 1] += 1
        lines.append(f"Pista {index} ({_track_name(source_track, index)}):")
        lines.append("  Transformados/conservados: " + (", ".join(f"{kind}={count}" for kind, count in sorted(transformed.items())) or "ninguno"))
        lines.append("  Descartados por tipo: " + (", ".join(f"{kind}={count}" for kind, count in sorted(discarded.items())) or "ninguno"))
        if channels:
            lines.append("  Descartados por canal MIDI: " + ", ".join(f"{channel}={count}" for channel, count in sorted(channels.items())))
    lines += ["", "ADVERTENCIAS Y DECISIONES CONSERVADORAS"]
    lines += ["- " + warning for warning in warnings] or ["Ninguna."]
    lines += ["", "VALIDACIÓN AUTOMÁTICA TRAS REABRIR EL ARCHIVO",
              "APROBADO" if passed else "FALLO — EL ARCHIVO NO ES FIABLE",
              f"Notas origen mapeadas={len(mapped)}; destino={len(hits)}; colapsos declarados={collapsed_count}; igualdad={len(mapped) == len(hits) + collapsed_count}.",
              "Se compararon PPQ, tempos exactos, los cuatro campos de compás, tonalidades y cada ataque/cierre (tick, nota, velocidad y canal) del MIDI reabierto contra el origen con solo las transformaciones declaradas. Las duraciones se verifican por sus ticks de cierre; los solapamientos ambiguos se advierten explícitamente.",
              "Se verificaron notas destino válidas (17 siempre excluida; 22 solo con permiso explícito), NO_MAPEADAS y copias de tempo/compás cuando corresponden."]
    lines += ["- " + failure for failure in failures]
    report = "\n".join(lines) + "\n"
    report_path.write_text(report, encoding="utf-8")
    return ConversionResult(output_path, report_path, report, passed)
