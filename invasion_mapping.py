"""Tabla numérica de GGD Invasion y lectura segura de mapas editables."""

from __future__ import annotations

import json
import re
from pathlib import Path


# Los números MIDI son la autoridad; las etiquetas solo ayudan a identificarlos.
TARGETS = [
    {"piece": "Kick Left", "kontakt": "B-1", "midi": 23, "fl": "B1"},
    {"piece": "Kick Right", "kontakt": "C0", "midi": 24, "fl": "C2"},
    {"piece": "Kick ADK (Auto Double Kick)", "kontakt": "A#-1", "midi": 22, "fl": "A#1"},
    {"piece": "Snare Hit", "kontakt": "D0", "midi": 26, "fl": "D2"},
    {"piece": "Snare Flam", "kontakt": "D#0", "midi": 27, "fl": "D#2"},
    {"piece": "Snare Ruff", "kontakt": "E0", "midi": 28, "fl": "E2"},
    {"piece": "Snare Wires Off", "kontakt": "F0", "midi": 29, "fl": "F2"},
    {"piece": "Snare Sidestick", "kontakt": "F#0", "midi": 30, "fl": "F#2"},
    {"piece": "Rack Tom 1", "kontakt": "A0", "midi": 33, "fl": "A2"},
    {"piece": "Rack Tom 2", "kontakt": "A#0", "midi": 34, "fl": "A#2"},
    {"piece": "Rack Tom 3", "kontakt": "B0", "midi": 35, "fl": "B2"},
    {"piece": "Floor Tom 1", "kontakt": "C1", "midi": 36, "fl": "C3"},
    {"piece": "Floor Tom 2", "kontakt": "C#1", "midi": 37, "fl": "C#3"},
    {"piece": "Floor Tom 3", "kontakt": "D1", "midi": 38, "fl": "D3"},
    {"piece": "Hat Tip Tight", "kontakt": "F1", "midi": 41, "fl": "F3"},
    {"piece": "Hat Edge Tight", "kontakt": "F#1", "midi": 42, "fl": "F#3"},
    {"piece": "Hat Tip Closed", "kontakt": "G1", "midi": 43, "fl": "G3"},
    {"piece": "Hat Edge Closed", "kontakt": "G#1", "midi": 44, "fl": "G#3"},
    {"piece": "Hat Open 1", "kontakt": "A1", "midi": 45, "fl": "A3"},
    {"piece": "Hat Open 2", "kontakt": "A#1", "midi": 46, "fl": "A#3"},
    {"piece": "Hat Open 3", "kontakt": "B1", "midi": 47, "fl": "B3"},
    {"piece": "Hat Pedal", "kontakt": "C2", "midi": 48, "fl": "C4"},
    {"piece": "Hat CC (reservada, NO generar notas)", "kontakt": "F-1", "midi": 17, "fl": "F1"},
    {"piece": "Main Crash L Hit", "kontakt": "E2", "midi": 52, "fl": "E4"},
    {"piece": "Main Crash L Choke", "kontakt": "F2", "midi": 53, "fl": "F4"},
    {"piece": "Main Crash R Hit", "kontakt": "F#2", "midi": 54, "fl": "F#4"},
    {"piece": "Main Crash R Choke", "kontakt": "G2", "midi": 55, "fl": "G4"},
    {"piece": "Wide Crash L Hit", "kontakt": "G#2", "midi": 56, "fl": "G#4"},
    {"piece": "Wide Crash L Choke", "kontakt": "A2", "midi": 57, "fl": "A4"},
    {"piece": "Wide Crash R Hit", "kontakt": "A#2", "midi": 58, "fl": "A#4"},
    {"piece": "Wide Crash R Choke", "kontakt": "B2", "midi": 59, "fl": "B4"},
    {"piece": "Ride Bell", "kontakt": "C#3", "midi": 61, "fl": "C#5"},
    {"piece": "Ride", "kontakt": "D3", "midi": 62, "fl": "D5"},
    {"piece": "Ride Edge", "kontakt": "D#3", "midi": 63, "fl": "D#5"},
    {"piece": "China L Hit", "kontakt": "F3", "midi": 65, "fl": "F5"},
    {"piece": "China L Choke", "kontakt": "F#3", "midi": 66, "fl": "F#5"},
    {"piece": "China R Hit", "kontakt": "G3", "midi": 67, "fl": "G5"},
    {"piece": "China R Choke", "kontakt": "G#3", "midi": 68, "fl": "G#5"},
    {"piece": "X-Hat Closed", "kontakt": "A#3", "midi": 70, "fl": "A#5"},
    {"piece": "X-Hat Open", "kontakt": "B3", "midi": 71, "fl": "B5"},
    {"piece": "Splash L Hit", "kontakt": "C#4", "midi": 73, "fl": "C#6"},
    {"piece": "Splash L Choke", "kontakt": "D4", "midi": 74, "fl": "D6"},
    {"piece": "Splash C Hit", "kontakt": "D#4", "midi": 75, "fl": "D#6"},
    {"piece": "Splash C Choke", "kontakt": "E4", "midi": 76, "fl": "E6"},
    {"piece": "Stack Hit", "kontakt": "F#4", "midi": 78, "fl": "F#6"},
    {"piece": "Bell L Hit", "kontakt": "G4", "midi": 79, "fl": "G6"},
    {"piece": "Bell R Hit", "kontakt": "G#4", "midi": 80, "fl": "G#6"},
]

GM_NAMES = {
    35: "Bombo acústico", 36: "Bombo", 37: "Golpe lateral de caja",
    38: "Caja acústica", 39: "Palmada", 40: "Caja eléctrica",
    41: "Tom de suelo grave", 42: "Hi-hat cerrado", 43: "Tom de suelo agudo",
    44: "Hi-hat de pedal", 45: "Tom grave", 46: "Hi-hat abierto",
    47: "Tom medio-grave", 48: "Tom medio-agudo", 49: "Crash 1",
    50: "Tom agudo", 51: "Ride 1", 52: "China", 53: "Campana de ride",
    54: "Pandereta", 55: "Splash", 56: "Cencerro", 57: "Crash 2",
    58: "Vibraslap", 59: "Ride 2", 60: "Bongo agudo", 61: "Bongo grave",
    62: "Conga aguda apagada", 63: "Conga aguda abierta", 64: "Conga grave",
    65: "Timbal agudo", 66: "Timbal grave", 67: "Agogó agudo", 68: "Agogó grave",
    69: "Cabasa", 70: "Maracas", 71: "Silbato corto", 72: "Silbato largo",
    73: "Güiro corto", 74: "Güiro largo", 75: "Claves", 76: "Bloque agudo",
    77: "Bloque grave", 78: "Cuica apagada", 79: "Cuica abierta",
    80: "Triángulo apagado", 81: "Triángulo abierto",
}

DEFAULT_MAPPING = {
    35: 23, 36: 23, 38: 26, 40: 26, 37: 30,
    42: 43, 44: 48, 46: 46,
    50: 33, 48: 34, 47: 35, 45: 36, 43: 37, 41: 38,
    49: 52, 57: 54, 55: 73, 52: 65, 51: 62, 59: 63, 53: 61,
}
KICK_SOURCES = {35, 36}
VALID_TARGETS = {target["midi"] for target in TARGETS if target["midi"] != 17}

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def _integer(value, label: str) -> int:
    """No aceptar bool: en Python se considera int, pero no es una nota."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} debe ser un número entero.")
    return value


def note_name(number: int, c4_octave: int = 3) -> str:
    """Nombre informativo; c4_octave indica la octava que se muestra para MIDI 60."""
    number = _integer(number, "La nota MIDI")
    c4_octave = _integer(c4_octave, "La octava de MIDI 60")
    if not 0 <= number <= 127:
        raise ValueError("La nota MIDI debe estar entre 0 y 127.")
    return f"{_NOTE_NAMES[number % 12]}{number // 12 - 5 + c4_octave}"


def name_to_number(name: str, c4_octave: int = 3) -> int:
    """Convertir etiquetas con sostenidos o bemoles sin usarlas para remapear."""
    c4_octave = _integer(c4_octave, "La octava de MIDI 60")
    if not isinstance(name, str):
        raise ValueError("El nombre de nota debe ser texto.")
    match = re.fullmatch(r"([A-Ga-g])([#b]?)(-?\d+)", name.strip())
    if not match:
        raise ValueError(f"Nombre de nota inválido: {name!r}.")
    letter, accidental, octave = match.groups()
    semitone = _SEMITONES[letter.upper()] + {"": 0, "#": 1, "b": -1}[accidental]
    number = (int(octave) - c4_octave + 5) * 12 + semitone
    if not 0 <= number <= 127:
        raise ValueError(f"La nota {name!r} queda fuera de MIDI 0–127.")
    return number


def _unique_object(pairs):
    """Evitar que una clave JSON repetida sustituya silenciosamente otra regla."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Clave repetida en el mapa JSON: {key!r}.")
        result[key] = value
    return result


def load_mapping(path: str | Path, allow_adk: bool = False) -> dict[int, int]:
    """Leer {\"mapping\": {\"35\": 23}} o un diccionario directo de números."""
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        document = json.load(handle, object_pairs_hook=_unique_object)
    if not isinstance(document, dict):
        raise ValueError("El mapa JSON debe ser un objeto.")
    rules = document.get("mapping", document)
    if not isinstance(rules, dict):
        raise ValueError("El campo 'mapping' debe ser un objeto de origen a destino.")
    allowed = VALID_TARGETS if allow_adk else VALID_TARGETS - {22}
    mapping = {}
    for source, destination in rules.items():
        if not isinstance(source, str) or not re.fullmatch(r"\d+", source):
            raise ValueError(f"Origen MIDI inválido: {source!r}; use claves como '35'.")
        source_number = int(source)
        if not 0 <= source_number <= 127:
            raise ValueError(f"El origen MIDI {source_number} queda fuera de 0–127.")
        if source_number in mapping:
            raise ValueError(f"El origen MIDI {source_number} aparece más de una vez.")
        destination = _integer(destination, f"El destino del origen {source_number}")
        if destination == 17:
            raise ValueError("MIDI 17 está reservado para Hat CC y no admite notas.")
        if destination == 22 and not allow_adk:
            raise ValueError("MIDI 22 requiere activar explícitamente la opción ADK.")
        if destination not in allowed:
            raise ValueError(f"Destino MIDI {destination} no válido en la tabla de Invasion.")
        mapping[source_number] = destination
    return mapping


def ensure_mapping(path: str | Path) -> Path:
    """Crear el mapa predeterminado si falta; conservar siempre el mapa del usuario."""
    path = Path(path)
    document = {
        "description": "GM de Songsterr a GGD Invasion. Los números MIDI son la autoridad. "
                       "35/36 usan 23; la alternancia puede elegir 24. "
                       "17 está reservado; 22 requiere ADK explícito.",
        "mapping": {str(source): destination for source, destination in sorted(DEFAULT_MAPPING.items())},
    }
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError:
        pass
    return path
