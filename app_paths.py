import os
import shutil
import sys
import tempfile
from pathlib import Path


APP_DIR_NAME = "TetrioPcHelper"


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


def get_user_data_dir():
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        fallback = Path(local_appdata) / APP_DIR_NAME
    else:
        fallback = Path.home() / "AppData" / "Local" / APP_DIR_NAME

    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_runtime_data_dir():
    return get_user_data_dir()


def get_resource_path(*parts):
    return get_bundle_base_dir().joinpath(*parts)


def get_user_data_path(*parts):
    path = get_user_data_dir().joinpath(*parts)
    parent = path if not path.suffix else path.parent
    parent.mkdir(parents=True, exist_ok=True)
    return path


def ensure_runtime_file(relative_path):
    relative = Path(relative_path)
    target = get_user_data_path(*relative.parts)
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

    return ensure_runtime_file(relative)


def resolve_node_executable():
    bundled = get_resource_path("runtime", "node.exe")
    if bundled.exists():
        return str(bundled)

    path_node = shutil.which("node.exe") or shutil.which("node")
    if path_node:
        return path_node

    raise FileNotFoundError("Node.js 실행 파일을 찾을 수 없습니다.")
