"""

We acquire the python information by running an interrogation script via subprocess trigger. This operation is not
cheap, especially not on Windows. To not have to pay this hefty cost every time we apply multiple levels of
caching.
"""  # noqa: D205

from __future__ import annotations

import logging
import os
import random
import sys
from collections import OrderedDict
from pathlib import Path
from shlex import quote
from string import ascii_lowercase, ascii_uppercase, digits
from subprocess import Popen

from virtualenv.app_data import AppDataDisabled
from virtualenv.discovery.py_info import PythonInfo
from virtualenv.util.subprocess import subprocess

_CACHE = OrderedDict()
_CACHE[Path(sys.executable)] = PythonInfo()


def from_exe(cls, app_data, exe, env=None, raise_on_error=True, ignore_cache=False):  # noqa: FBT002, PLR0913
    env = os.environ if env is None else env
    result = _get_from_cache(cls, app_data, exe, env, ignore_cache=ignore_cache)
    if isinstance(result, Exception):
        if raise_on_error:
            raise result
        logging.info("%s", result)
        result = None
    return result


def _get_from_cache(cls, app_data, exe, env, ignore_cache=True):  # noqa: FBT002
    # note here we cannot resolve symlinks, as the symlink may trigger different prefix information if there's a
    # pyenv.cfg somewhere alongside on python3.5+
    exe_path = Path(exe)
    if not ignore_cache and exe_path in _CACHE:  # check in the in-memory cache
        result = _CACHE[exe_path]
    else:  # otherwise go through the app data cache
        py_info = _get_via_file_cache(cls, app_data, exe_path, exe, env)
        result = _CACHE[exe_path] = py_info
    # independent if it was from the file or in-memory cache fix the original executable location
    if isinstance(result, PythonInfo):
        result.executable = exe
    return result


def _get_via_file_cache(cls, app_data, path, exe, env):
    path_text = str(path)
    try:
        path_modified = path.stat().st_mtime
    except OSError:
        path_modified = -1
    if app_data is None:
        app_data = AppDataDisabled()
    py_info, py_info_store = None, app_data.py_info(path)
    with py_info_store.locked():
        if py_info_store.exists():  # if exists and matches load
            data = py_info_store.read()
            of_path, of_st_mtime, of_content = data["path"], data["st_mtime"], data["content"]
            if of_path == path_text and of_st_mtime == path_modified:
                py_info = cls._from_dict(of_content.copy())
                sys_exe = py_info.system_executable
                if sys_exe is not None and not os.path.exists(sys_exe):
                    py_info_store.remove()
                    py_info = None
            else:
                py_info_store.remove()
        if py_info is None:  # if not loaded run and save
            failure, py_info = _run_subprocess(cls, exe, app_data, env)
            if failure is None:
                data = {"st_mtime": path_modified, "path": path_text, "content": py_info._to_dict()}  # noqa: SLF001
                py_info_store.write(data)
            else:
                py_info = failure
    return py_info


COOKIE_LENGTH: int = 32


def gen_cookie():
    return "".join(
        random.choice(f"{ascii_lowercase}{ascii_uppercase}{digits}")  # noqa: S311
        for _ in range(COOKIE_LENGTH)
    )


def _run_subprocess(cls, exe, app_data, env):
    py_info_script = Path(os.path.abspath(__file__)).parent / "py_info.py"
    # Cookies allow to split the serialized stdout output generated by the script collecting the info from the output
    # generated by something else. The right way to deal with it is to create an anonymous pipe and pass its descriptor
    # to the child and output to it. But AFAIK all of them are either not cross-platform or too big to implement and are
    # not in the stdlib. So the easiest and the shortest way I could mind is just using the cookies.
    # We generate pseudorandom cookies because it easy to implement and avoids breakage from outputting modules source
    # code, i.e. by debug output libraries. We reverse the cookies to avoid breakages resulting from variable values
    # appearing in debug output.

    start_cookie = gen_cookie()
    end_cookie = gen_cookie()
    with app_data.ensure_extracted(py_info_script) as py_info_script:
        cmd = [exe, str(py_info_script), start_cookie, end_cookie]
        # prevent sys.prefix from leaking into the child process - see https://bugs.python.org/issue22490
        env = env.copy()
        env.pop("__PYVENV_LAUNCHER__", None)
        logging.debug("get interpreter info via cmd: %s", LogCmd(cmd))
        try:
            process = Popen(
                cmd,
                universal_newlines=True,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env=env,
                encoding="utf-8",
            )
            out, err = process.communicate()
            code = process.returncode
        except OSError as os_error:
            out, err, code = "", os_error.strerror, os_error.errno
    result, failure = None, None
    if code == 0:
        out_starts = out.find(start_cookie[::-1])

        if out_starts > -1:
            pre_cookie = out[:out_starts]

            if pre_cookie:
                sys.stdout.write(pre_cookie)

            out = out[out_starts + COOKIE_LENGTH :]

        out_ends = out.find(end_cookie[::-1])

        if out_ends > -1:
            post_cookie = out[out_ends + COOKIE_LENGTH :]

            if post_cookie:
                sys.stdout.write(post_cookie)

            out = out[:out_ends]

        result = cls._from_json(out)
        result.executable = exe  # keep original executable as this may contain initialization code
    else:
        msg = f"{exe} with code {code}{f' out: {out!r}' if out else ''}{f' err: {err!r}' if err else ''}"
        failure = RuntimeError(f"failed to query {msg}")
    return failure, result


class LogCmd:
    def __init__(self, cmd, env=None) -> None:
        self.cmd = cmd
        self.env = env

    def __repr__(self) -> str:
        cmd_repr = " ".join(quote(str(c)) for c in self.cmd)
        if self.env is not None:
            cmd_repr = f"{cmd_repr} env of {self.env!r}"
        return cmd_repr


def clear(app_data):
    app_data.py_info_clear()
    _CACHE.clear()


___all___ = [
    "from_exe",
    "clear",
    "LogCmd",
]
