"""Corrección específica de la configuración ICL de esta PC, con respaldo.

Sin --apply sólo muestra el cambio. No lee ni escribe archivos de licencia.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time

SDK_ROOT = Path(r"C:\ProgramData\HORIBA Scientific\SDK\ICL")
ACTIVE_INI = Path(r"C:\ProgramData\HORIBA Scientific\EzSpec\ICL\icl_settings.ini")
ICL_EXE = Path(r"C:\Program Files\HORIBA Scientific\SDK\icl.exe")

SDK_CONFIG = """[%General]
FormatVersion=1

[Modules]
Lmb=false
Amb=false
Spectograph=false
InstrumentXmb=false
SampleController=false
Ccd=true
Mono=true
SingleChannelDetector=true
SAQ3=true

[Instrument]
Model=SDK
"""


def repaired_active(text):
    changes = dict(Model="SDK", Mono="true", Ccd="true", Lmb="false", Amb="false",
                   Spectograph="false", InstrumentXmb="false", SampleController="false", SAQ3="true")
    for key, value in changes.items():
        pattern = r"(?m)^" + re.escape(key) + r"=[^\r\n]*"
        if not re.search(pattern, text):
            raise ValueError("No existe la clave {} en la configuración activa; no se modificó ningún archivo.".format(key))
        text = re.sub(pattern, key+"="+value, text)
    return text


def repair(sdk_root=SDK_ROOT, active_ini=ACTIVE_INI, apply=False):
    sdk_root, active_ini = Path(sdk_root).resolve(), Path(active_ini).resolve()
    sdk_target = sdk_root / "icl_settings.ini"
    # Se valida el destino absoluto antes de renombrar una carpeta.
    if sdk_target.resolve().parent != sdk_root or active_ini.name != "icl_settings.ini":
        raise ValueError("Destinos fuera de las carpetas de configuración previstas.")
    new_active = repaired_active(active_ini.read_text(encoding="utf-8-sig"))
    report = dict(sdk_target=str(sdk_target), active_ini=str(active_ini),
                  sdk_is_directory=sdk_target.is_dir(), changes="Model=SDK; Mono/Ccd/SAQ3=true; módulos Duetta=false",
                  applied=False)
    if not apply:
        return report
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    sdk_backup = sdk_target.with_name(sdk_target.name+".backup_"+stamp)
    active_backup = active_ini.with_name(active_ini.name+".backup_"+stamp)
    shutil.copy2(str(active_ini), str(active_backup))
    was_directory = sdk_target.is_dir()
    if was_directory:
        sdk_target.rename(sdk_backup)
    else:
        shutil.copy2(str(sdk_target), str(sdk_backup))
    try:
        sdk_target.write_text(SDK_CONFIG, encoding="ascii")
        active_ini.write_text(new_active, encoding="utf-8")
    except Exception:
        shutil.copy2(str(active_backup), str(active_ini))
        if was_directory:
            if sdk_target.is_file():
                sdk_target.unlink()
            sdk_backup.rename(sdk_target)
        else:
            shutil.copy2(str(sdk_backup), str(sdk_target))
        raise
    report.update(applied=True, sdk_backup=str(sdk_backup), active_backup=str(active_backup))
    return report


def port_open():
    try:
        with socket.create_connection(("127.0.0.1", 25010), timeout=.3):
            return True
    except OSError:
        return False


def restart():
    import websocket
    if port_open():
        connection = websocket.create_connection("ws://127.0.0.1:25010", timeout=8)
        try:
            connection.send(json.dumps(dict(id=1, command="icl_shutdown", parameters={})))
        finally:
            connection.close()
        deadline = time.monotonic()+8
        while port_open():
            if time.monotonic() > deadline:
                raise RuntimeError("ICL no cerró normalmente. La configuración está guardada; cierre ICL antes de reiniciarlo.")
            time.sleep(.2)
    subprocess.Popen([str(ICL_EXE)], cwd=str(ICL_EXE.parent),
                     creationflags=subprocess.CREATE_NO_WINDOW,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    report = repair(apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.restart:
        if not args.apply:
            parser.error("--restart requiere --apply")
        restart()
        print("ICL reiniciado. Ejecute horiba-diagnose para comprobar los módulos.")


if __name__ == "__main__":
    main()
