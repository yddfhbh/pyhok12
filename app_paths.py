import os
import shutil
import sys
import tempfile
from pathlib import Path


APP_DIR_NAME = "TetrisScan"


def get_bundle_base_dir():
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def get_launch_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    argv0 = Path(sys.argv[0]) if sys.argv else None
    if argv0 and argv0.suffix.lower() == ".exe" and argv0.exists():
        return argv0.resolve().parent
    return Path(__file__).resolve().parent


def is_directory_writable(path):
    try:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, delete=True):
            pass
        return True
    except Exception:
        return False


def get_runtime_data_dir():
    launch_dir = get_launch_base_dir()
    if is_directory_writable(launch_dir):
        return launch_dir

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        fallback = Path(local_appdata) / APP_DIR_NAME
    else:
        fallback = Path.home() / "AppData" / "Local" / APP_DIR_NAME

    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_resource_path(*parts):
    return get_bundle_base_dir().joinpath(*parts)


def ensure_runtime_file(relative_path):
    relative = Path(relative_path)
    target = get_runtime_data_dir() / relative
    if target.exists():
        return target

    source = get_resource_path(*relative.parts)
    if source.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return target

    return target


def resolve_runtime_file(relative_path):
    relative = Path(relative_path)
    if relative.is_absolute():
        return relative

    launch_candidate = get_launch_base_dir() / relative
    if launch_candidate.exists():
        return launch_candidate

    return ensure_runtime_file(relative)
