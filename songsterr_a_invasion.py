"""Interfaz gráfica y CLI del conversor Songsterr → GGD Invasion."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from invasion_core import Options, convert, inspect_midi
from invasion_mapping import GM_NAMES, TARGETS, ensure_mapping, load_mapping, note_name


DEFAULT_MAPPING_PATH = Path(__file__).with_name("mapping_invasion.json")


def inventory_text(candidate):
    lines = [f"Pista {candidate.index}: {candidate.name}", "Notas únicas encontradas (antes de convertir):"]
    for number, count in sorted(candidate.notes.items()):
        lines.append(f"  MIDI {number:3} | {GM_NAMES.get(number, 'GM desconocida')} | {count}")
    return "\n".join(lines)


def parser():
    result = argparse.ArgumentParser(
        description="Convierte batería GM de Songsterr a GGD Invasion conservando ticks y mapa de tempo.",
        epilog="Sin archivo abre la interfaz gráfica. Los índices de pista empiezan en cero.",
    )
    result.add_argument("archivo", nargs="?", type=Path)
    result.add_argument("--gui", action="store_true", help="abrir la interfaz gráfica")
    result.add_argument("--track", type=int, help="índice de la pista de batería")
    result.add_argument("--merge-drums", action="store_true", help="combinar todas las pistas de batería detectadas")
    result.add_argument("--output", type=Path, help="ruta del MIDI de salida")
    result.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING_PATH, help="JSON de remapeo editable")
    result.add_argument("--no-alternate-kicks", action="store_true", help="desactivar alternancia L/R")
    result.add_argument("--channel", type=int, choices=(1, 10), default=1, help="canal MIDI de salida")
    result.add_argument("--format-zero", action="store_true", help="MIDI formato 0 con una sola pista")
    result.add_argument("--copy-timing", action="store_true", help="copiar tempo y compás a la batería")
    result.add_argument("--fl-tempo-score", action="store_true", help="exportar automatización de tempo .fsc (experimental, comprobar en FL)")
    result.add_argument("--include-unmapped", action="store_true", help="incluir pista NO_MAPEADAS")
    result.add_argument("--fixed-duration", type=int, metavar="TICKS", help="duración fija en ticks (opcional)")
    result.add_argument("--adk", action="store_true", help="permitir destino ADK 22 en el mapa editable")
    result.add_argument("--octave-offset", type=int, default=2, help="octavas FL sobre Kontakt (por defecto 2)")
    result.add_argument("--list-tracks", action="store_true", help="mostrar pistas y notas, sin convertir")
    return result


def cli(args):
    inspection = inspect_midi(args.archivo)
    for warning in inspection.warnings:
        print(f"AVISO: {warning}")
    if args.list_tracks:
        candidate_indexes = {c.index for c in inspection.candidates}
        for index, track in enumerate(inspection.midi.tracks):
            name = next((m.name for kind in ("track_name", "instrument_name")
                         for m in track if m.type == kind and m.name), f"Pista {index}")
            marker = " [candidata a batería]" if index in candidate_indexes else ""
            print(f"Pista {index}: {name}{marker}")
    for candidate in inspection.candidates:
        print(inventory_text(candidate))
        print(f"  Detección: {candidate.reason}\n")
    if args.list_tracks:
        return 0
    if not inspection.candidates:
        raise ValueError("No se detectó una pista de batería con notas en canal 10 o nombre de batería.")
    track = args.track
    if args.merge_drums and track is not None:
        raise ValueError("Usa --merge-drums o --track, no ambas opciones a la vez.")
    if track is None and len(inspection.candidates) > 1 and not args.merge_drums:
        if not sys.stdin.isatty():
            raise ValueError("Hay varias pistas candidatas; elige una con --track INDICE.")
        try:
            track = int(input("Elige el índice de la pista de batería: "))
        except (ValueError, EOFError) as error:
            raise ValueError("Debes elegir un índice válido de pista.") from error
    if track is not None and track not in {c.index for c in inspection.candidates}:
        raise ValueError("--track debe ser el índice de una de las pistas candidatas mostradas.")
    result = convert(
        args.archivo,
        args.output,
        track_index=track,
        mapping_path=args.mapping,
        options=Options(
            output_channel=args.channel - 1,
            alternate_kicks=not args.no_alternate_kicks,
            allow_adk=args.adk,
            format_zero=args.format_zero,
            copy_timing=args.copy_timing,
            include_unmapped=args.include_unmapped,
            fixed_duration=args.fixed_duration,
            octave_offset=args.octave_offset,
            merge_drums=args.merge_drums,
        ),
    )
    print(result.report)
    print(f"MIDI: {result.output_path}\nReporte: {result.report_path}\nMapa: {args.mapping}")
    if args.fl_tempo_score and result.passed:
        from fl_tempo_score import export_tempo_score
        score = export_tempo_score(result.output_path)
        print(f"Tempo FL (experimental): {score.path}\nTabla exacta de tempos: {score.csv_path}")
        for warning in score.warnings:
            print(f"AVISO: {warning}")
    return 0 if result.passed else 2


def run_gui(args):
    # Tkinter se importa aquí para que la CLI funcione también sin pantalla.
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    root = tk.Tk()
    root.title("Songsterr → GGD Invasion")
    root.geometry("1040x780")
    root.minsize(800, 600)
    selected_file = tk.StringVar(value=str(args.archivo or ""))
    selected_mapping = tk.StringVar(value=str(args.mapping))
    alternate = tk.BooleanVar(value=not args.no_alternate_kicks)
    channel_ten = tk.BooleanVar(value=args.channel == 10)
    format_zero = tk.BooleanVar(value=args.format_zero)
    copy_timing = tk.BooleanVar(value=args.copy_timing)
    fl_tempo_score = tk.BooleanVar(value=args.fl_tempo_score)
    include_unmapped = tk.BooleanVar(value=args.include_unmapped)
    adk = tk.BooleanVar(value=args.adk)
    merge_drums = tk.BooleanVar(value=args.merge_drums)
    fixed_enabled = tk.BooleanVar(value=args.fixed_duration is not None)
    duration = tk.StringVar(value=str(args.fixed_duration or 120))
    octave_offset = tk.StringVar(value=str(args.octave_offset))
    candidates = []
    last_output = [None]
    messages = queue.Queue()
    busy = [False]
    scanned_file = [None]

    outer = ttk.Frame(root, padding=12)
    outer.pack(fill="both", expand=True)
    files = ttk.Frame(outer)
    files.pack(fill="x")
    ttk.Label(files, text="Archivo MIDI:").grid(row=0, column=0, sticky="w")
    ttk.Entry(files, textvariable=selected_file).grid(row=0, column=1, sticky="ew", padx=8)
    file_button = ttk.Button(files, text="Elegir archivo…")
    file_button.grid(row=0, column=2)
    ttk.Label(files, text="Mapa editable:").grid(row=1, column=0, sticky="w", pady=6)
    ttk.Entry(files, textvariable=selected_mapping).grid(row=1, column=1, sticky="ew", padx=8)
    map_button = ttk.Button(files, text="Elegir JSON…")
    map_button.grid(row=1, column=2)
    files.columnconfigure(1, weight=1)

    track_row = ttk.Frame(outer)
    track_row.pack(fill="x", pady=8)
    ttk.Label(track_row, text="Pista de batería:").pack(side="left")
    tracks = ttk.Combobox(track_row, state="readonly", width=65)
    tracks.pack(side="left", fill="x", expand=True, padx=8)
    scan_button = ttk.Button(track_row, text="Leer pistas")
    scan_button.pack(side="left")

    options_frame = ttk.LabelFrame(outer, text="Opciones", padding=8)
    options_frame.pack(fill="x")
    for position, (label, variable) in enumerate([
        ("Alternar bombos L/R", alternate), ("Salida en canal 10 (por defecto 1)", channel_ten),
        ("Formato 0 (una pista)", format_zero), ("Copiar tempo y compás en batería", copy_timing),
        ("Incluir NO_MAPEADAS", include_unmapped), ("Permitir ADK 22 en el mapa", adk),
        ("Combinar todas las pistas de batería", merge_drums),
        ("Tempo FL .fsc (experimental)", fl_tempo_score),
    ]):
        ttk.Checkbutton(options_frame, text=label, variable=variable).grid(
            row=position // 2, column=position % 2, sticky="w", padx=5, pady=2
        )
    ttk.Checkbutton(options_frame, text="Duración fija (ticks):", variable=fixed_enabled).grid(row=4, column=0, sticky="w", padx=5)
    ttk.Entry(options_frame, textvariable=duration, width=9).grid(row=4, column=1, sticky="w", padx=5)
    ttk.Label(options_frame, text="Octavas FL sobre Kontakt C3=60:").grid(row=5, column=0, sticky="w", padx=5, pady=5)
    ttk.Spinbox(options_frame, from_=-8, to=8, textvariable=octave_offset, width=8).grid(row=5, column=1, sticky="w", padx=5)

    actions = ttk.Frame(outer)
    actions.pack(fill="x", pady=8)
    convert_button = ttk.Button(actions, text="Convertir")
    convert_button.pack(side="left")
    open_button = ttk.Button(actions, text="Abrir carpeta", state="disabled")
    open_button.pack(side="left", padx=8)
    status = tk.StringVar(value="Elige un MIDI para ver sus pistas y notas.")
    ttk.Label(actions, textvariable=status).pack(side="left", padx=8)

    notebook = ttk.Notebook(outer)
    notebook.pack(fill="both", expand=True)
    log = ScrolledText(notebook, wrap="word", font=("Consolas", 10), state="disabled")
    notebook.add(log, text="Inventario y reporte")
    map_frame = ttk.Frame(notebook)
    notebook.add(map_frame, text="Notas Invasion (número MIDI manda)")
    table = ttk.Treeview(map_frame, columns=("piece", "midi", "kontakt", "fl"), show="headings")
    for column, label, width in [("piece", "Pieza", 290), ("midi", "MIDI", 80), ("kontakt", "Kontakt C3=60", 150), ("fl", "FL Studio", 150)]:
        table.heading(column, text=label)
        table.column(column, width=width, anchor="w")
    scrollbar = ttk.Scrollbar(map_frame, orient="vertical", command=table.yview)
    table.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    table.pack(fill="both", expand=True)

    def append_log(text):
        log.configure(state="normal")
        log.insert("end", text + "\n")
        log.see("end")
        log.configure(state="disabled")

    def update_table(*_):
        try:
            offset = int(octave_offset.get())
        except ValueError:
            return
        table.delete(*table.get_children())
        for target in TARGETS:
            table.insert("", "end", values=(target["piece"], target["midi"], note_name(target["midi"], 3), note_name(target["midi"], 3 + offset)))
        table.heading("fl", text=f"FL C{3 + offset}=60")

    def scan():
        nonlocal candidates
        if busy[0]:
            return False
        candidates = []
        tracks.set("")
        tracks.configure(values=())
        try:
            inspection = inspect_midi(Path(selected_file.get()))
            scanned_file[0] = Path(selected_file.get()).resolve()
            candidates = inspection.candidates
            tracks.configure(values=[f"{c.index}: {c.name} — {sum(c.notes.values())} golpes ({c.reason})" for c in candidates])
            for warning in inspection.warnings:
                append_log(f"AVISO: {warning}")
            for candidate in candidates:
                append_log(inventory_text(candidate))
            if len(candidates) == 1:
                tracks.current(0)
            elif args.track is not None:
                for i, candidate in enumerate(candidates):
                    if candidate.index == args.track:
                        tracks.current(i)
                        break
            if not candidates:
                raise ValueError("No se detectó ninguna pista de batería con notas.")
            if merge_drums.get():
                status.set(f"Se combinarán {len(candidates)} pistas de batería.")
            else:
                status.set("Elige una pista." if len(candidates) > 1 else "Pista detectada. Lista para convertir.")
            return True
        except Exception as error:
            append_log(f"ERROR: {error}")
            status.set("No se pudo leer el MIDI.")
            messagebox.showerror("No se pudo leer el MIDI", str(error), parent=root)
            return False

    def choose_file():
        path = filedialog.askopenfilename(filetypes=[("MIDI", "*.mid *.midi"), ("Todos", "*.*")], parent=root)
        if path:
            selected_file.set(path)
            scan()

    def choose_mapping():
        path = filedialog.askopenfilename(filetypes=[("Mapa JSON", "*.json")], parent=root)
        if path:
            selected_mapping.set(path)

    def set_busy(value):
        busy[0] = value
        for widget in (convert_button, file_button, map_button, scan_button):
            widget.configure(state="disabled" if value else "normal")
        tracks.configure(state="disabled" if value else "readonly")

    def start_conversion():
        if busy[0]:
            return
        # Releer permite detectar un archivo reemplazado sin depender de una lista antigua.
        same_file = scanned_file[0] == Path(selected_file.get()).resolve()
        chosen_index = candidates[tracks.current()].index if same_file and 0 <= tracks.current() < len(candidates) else None
        if not scan():
            return
        if chosen_index is not None:
            for i, candidate in enumerate(candidates):
                if candidate.index == chosen_index:
                    tracks.current(i)
                    break
        if tracks.current() < 0 and not merge_drums.get():
            messagebox.showerror("Elige una pista", "Hay varias candidatas. Elige la pista de batería.", parent=root)
            return
        try:
            ticks = int(duration.get()) if fixed_enabled.get() else None
            if ticks is not None and ticks <= 0:
                raise ValueError("La duración fija debe ser un entero positivo en ticks.")
            opts = Options(
                output_channel=9 if channel_ten.get() else 0,
                alternate_kicks=alternate.get(), allow_adk=adk.get(),
                format_zero=format_zero.get(), copy_timing=copy_timing.get(),
                include_unmapped=include_unmapped.get(), fixed_duration=ticks,
                octave_offset=int(octave_offset.get()),
                merge_drums=merge_drums.get(),
            )
            source = Path(selected_file.get())
            mapping = Path(selected_mapping.get())
            ensure_mapping(mapping)
            load_mapping(mapping, allow_adk=opts.allow_adk)
            index = None if opts.merge_drums else candidates[tracks.current()].index
            write_fl_score = fl_tempo_score.get()
        except Exception as error:
            messagebox.showerror("Opciones inválidas", str(error), parent=root)
            return
        for candidate in candidates if opts.merge_drums else [candidates[tracks.current()]]:
            append_log(inventory_text(candidate))
        set_busy(True)
        status.set("Convirtiendo y validando el archivo escrito…")

        def worker():
            try:
                result = convert(source, args.output, track_index=index, mapping_path=mapping, options=opts)
                extra = ""
                if write_fl_score and result.passed:
                    try:
                        from fl_tempo_score import export_tempo_score
                        score = export_tempo_score(result.output_path)
                        extra = f"Tempo FL (experimental): {score.path}\nTabla exacta de tempos: {score.csv_path}\n"
                        extra += "\n".join(f"AVISO: {warning}" for warning in score.warnings)
                    except Exception as error:
                        extra = f"El MIDI se generó. No se pudo exportar el tempo FL: {error}"
                messages.put(("result", (result, extra)))
            except Exception as error:
                messages.put(("error", str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def poll():
        try:
            while True:
                kind, item = messages.get_nowait()
                set_busy(False)
                if kind == "error":
                    append_log(f"ERROR: {item}")
                    status.set("Conversión fallida.")
                    messagebox.showerror("Conversión fallida", item, parent=root)
                else:
                    item, extra = item
                    append_log(item.report)
                    append_log(f"MIDI: {item.output_path}\nReporte: {item.report_path}")
                    if extra:
                        append_log(extra)
                    last_output[0] = item.output_path
                    open_button.configure(state="normal")
                    status.set("APROBADO MIDI; comprobar importación en FL" if item.passed else "FALLO: archivo no fiable")
                    if not item.passed:
                        messagebox.showwarning("Validación fallida", "El archivo no es fiable. Consulta el reporte.", parent=root)
        except queue.Empty:
            pass
        root.after(100, poll)

    def open_folder():
        if last_output[0] is None:
            return
        folder = str(Path(last_output[0]).resolve().parent)
        try:
            if os.name == "nt":
                os.startfile(folder)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder])
        except OSError as error:
            messagebox.showerror("No se pudo abrir la carpeta", str(error), parent=root)

    file_button.configure(command=choose_file)
    map_button.configure(command=choose_mapping)
    scan_button.configure(command=scan)
    convert_button.configure(command=start_conversion)
    open_button.configure(command=open_folder)
    tracks.bind("<<ComboboxSelected>>", lambda _: append_log(inventory_text(candidates[tracks.current()])) if tracks.current() >= 0 else None)
    octave_offset.trace_add("write", update_table)
    update_table()
    def close_window():
        if busy[0]:
            messagebox.showinfo("Conversión en curso", "Espera a que termine la conversión para cerrar la ventana.", parent=root)
        else:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close_window)
    if args.archivo:
        root.after(0, scan)
    root.after(100, poll)
    root.mainloop()
    return 0


def main(argv=None):
    # Windows puede usar una página de códigos antigua al redirigir el reporte.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parser().parse_args(argv)
    try:
        if args.gui or args.archivo is None:
            return run_gui(args)
        return cli(args)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
