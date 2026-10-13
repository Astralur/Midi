# Songsterr → GGD Invasion

Convierte la batería de un MIDI de Songsterr al preset básico **GGD Invasion** para Kontakt 8. Conserva los ticks de inicio, las velocidades y el PPQ; recoge el tempo y los compases de todas las pistas. Incluye interfaz Tkinter, CLI, mapa editable y validación al reabrir la salida.

## Instalar y abrir en Windows

Instala **Python 3.10 o posterior** con Tcl/Tk (incluido en el instalador habitual de python.org). Abre `iniciar.bat`: crea o reutiliza `.venv`, instala la versión fijada de `mido` si falta y abre la ventana. La primera instalación necesita conexión a PyPI.

Instalación manual desde la carpeta del proyecto:

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python songsterr_a_invasion.py
```

Sin argumentos se abre la interfaz; también puedes usar `--gui`. El selector de archivos funciona sin dependencias de arrastrar y soltar. Selecciona el MIDI y la pista de batería, ajusta las opciones y pulsa **Convertir**. El panel muestra el reporte completo y **Abrir carpeta** permite localizar los resultados.

## Línea de comandos

```bat
.venv\Scripts\python songsterr_a_invasion.py cancion.mid
.venv\Scripts\python songsterr_a_invasion.py cancion.mid --list-tracks
.venv\Scripts\python songsterr_a_invasion.py cancion.mid --track 2 --channel 10
.venv\Scripts\python songsterr_a_invasion.py cancion.mid --format-zero --copy-timing --include-unmapped
.venv\Scripts\python songsterr_a_invasion.py cancion.mid --mapping mi_mapa.json --no-alternate-kicks --output salida.mid
```

| Opción | Efecto |
| --- | --- |
| `--track INDICE` | Elegir la pista, con índices desde cero. |
| `--output PATH` | Elegir el archivo de salida. |
| `--mapping PATH` | Usar un mapa JSON editable; se crea el predeterminado si falta. |
| `--no-alternate-kicks` | Desactivar la alternancia de bombos. |
| `--channel {1,10}` | Canal de batería de salida, 1 por defecto. |
| `--format-zero` | Todo en una pista MIDI de formato 0. |
| `--copy-timing` | Copiar tempo y compás también en la pista de batería del formato 1. |
| `--include-unmapped` | Guardar las notas sin mapa en `NO_MAPEADAS`; en formato 0 se integran en la única pista. |
| `--fixed-duration TICKS` | Duración fija positiva en ticks; desactivada por defecto. |
| `--adk` | Permitir destino MIDI 22 en un mapa personalizado. |
| `--octave-offset 2` | Diferencia de octavas mostradas en FL respecto a Kontakt; no transpone. |
| `--list-tracks` | Mostrar las pistas y candidatas sin convertir. |
| `--gui` | Abrir la interfaz. |

Por defecto produce `cancion_Invasion.mid` y `cancion_reporte.txt` junto al original, y emplea `mapping_invasion.json`. En formato 1, pista 0 contiene el conductor y pista 1 la batería. Si hay varias candidatas, la GUI permite elegir; la CLI muestra los índices y solicita una elección, o exige `--track` cuando se ejecuta sin entrada interactiva. Una pista mixta con notas en canal 10 se filtra por ese canal para evitar convertir instrumentos melódicos. Una pista de batería identificada por su nombre y sin canal 10 usa sus notas de todos los canales. El reporte explica la selección y las notas excluidas.

## Mapa y decisiones conservadoras

Los **números MIDI mandan**. Kontakt muestra MIDI 60 como C3 y FL Studio como C5; es la misma nota. La tabla completa está en `invasion_mapping.py`, y el reporte muestra las columnas Kontakt/FL. El desplazamiento de octavas solo cambia etiquetas.

`mapping_invasion.json` contiene un objeto `mapping` con origen GM como clave de texto y destino Invasion como entero:

```json
{"description": "Mi mapa", "mapping": {"35": 23, "36": 23, "38": 26}}
```

También se acepta el diccionario de reglas directamente. Las claves deben estar entre 0 y 127 y los destinos deben pertenecer a la tabla Invasion. No se sobrescribe un mapa existente. MIDI 17 siempre está reservado y no puede generar notas. MIDI 22 solo se acepta con `--adk`; activar ADK no cambia por sí solo el mapa, debes asignar 35/36 a 22 si quieres usarlo. Las notas que falten en tu mapa se omiten de la batería y se detallan en el reporte; `--include-unmapped` las conserva aparte cuando el formato permite varias pistas. En formato 0 se mezclan conservando sus notas y canales originales, y pueden disparar otras articulaciones: usa formato 1 para dejarlas separadas.

La alternancia L/R predeterminada se calcula en ticks para los bombos mapeados a 23/24: una separación mayor que PPQ/4 vuelve al izquierdo y una secuencia rápida alterna entre 23 y 24. No modifica los destinos personalizados de otras articulaciones. Las notas que coinciden en destino y tick se colapsan con la velocidad máxima y la duración más larga, y quedan registradas. La duración fija es optativa; por defecto se conserva la duración original salvo esa resolución de colapsos.

Todos los eventos `set_tempo`, `time_signature` y `key_signature` se recolectan usando sus ticks absolutos. Se conserva cada evento de compás y tonalidad, incluso si comparten tick. Solo para tempos se eliminan duplicados exactos y, si hay valores distintos en el mismo tick, se conserva el último y se avisa. La validación compara los compases originales y el mapa de tempos normalizado según esas reglas. Si falta tempo se usa 120 BPM y se avisa.

La validación comprueba por separado cada ataque y cierre de nota. Si dos notas convergen en el mismo destino con duraciones solapadas, se conservan todos sus ticks y se avisa de que MIDI no identifica qué ataque corresponde a cada cierre. Las notas sin cierre se terminan al final de la pista y se advierte que su duración original no estaba definida. Los reportes y la salida CLI usan UTF-8.

El reporte enumera notas únicas GM antes de convertir, colapsos, descartes de `program_change` y `control_change`, y eventos SysEx/meta u otros eventos omitidos. Incluye las tablas de tempo y compás y el resultado **APROBADO/FALLO** después de reabrir la salida. Se rechazan división SMPTE y formato 2. Los archivos dañados o las notas mal emparejadas se informan; no se cuantiza, cambia el PPQ, redondea tempo ni normaliza velocidad.

## Importar en FL Studio

1. Carga Kontakt 8 con el preset básico **Invasion**.
2. En FL Studio usa **File > Import > MIDI file** y selecciona la salida.
3. Marca **Import time signatures** e importa también los eventos de tempo.
4. Asigna la batería al instrumento Kontakt; el canal de salida es 1 salvo que hayas elegido 10.
5. Compara los Edit events de tempo y los compases con las tablas del reporte.

Si FL ignora el conductor al importar, prueba `--copy-timing` o `--format-zero`. La validación automática comprueba el contenido MIDI; la reproducción dentro de Kontakt y la importación en FL Studio requieren esos programas y no se ejecutan en el entorno en la nube.

## Pruebas

```bat
.venv\Scripts\python -m unittest discover -s tests -v
```

En Linux/macOS usa `python3 -m venv .venv`, `.venv/bin/python -m pip install -r requirements.txt` y `.venv/bin/python -m unittest discover -s tests -v`. Para la GUI se necesita Tkinter y una pantalla; la CLI no los necesita. Las pruebas generan MIDI sintéticos para comprobar la tabla Invasion, el mapa GM, cambios de tempo/compás, posiciones, velocidades y opciones de conversión.
