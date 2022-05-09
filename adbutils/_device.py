#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Created on Fri May 06 2022 10:33:39 by codeskyblue
"""

import datetime
import io
import json
import os
import pathlib
import re
import stat
import struct
import subprocess
import tempfile
import time
import typing
import warnings
from contextlib import contextmanager
from dataclasses import asdict
from typing import Optional, Union

import apkutils2
import requests
from deprecation import deprecated
from PIL import Image, UnidentifiedImageError
from retry import retry

from ._adb import AdbConnection, BaseClient
from ._proto import *
from ._utils import (APKReader, ReadProgress, adb_path, get_free_port,
                     humanize, list2cmdline)
from ._version import __version__
from .errors import AdbError, AdbInstallError

_DISPLAY_RE = re.compile(
    r'.*DisplayViewport{.*?valid=true, .*?orientation=(?P<orientation>\d+), .*?deviceWidth=(?P<width>\d+), deviceHeight=(?P<height>\d+).*'
)


class BaseDevice:
    """ Basic operation for a device """
    def __init__(self, client: BaseClient, serial: str):
        self._client = client
        self._serial = serial
        self._properties = {}  # store properties data

    @property
    def serial(self) -> str:
        return self._serial

    def _get_with_command(self, cmd: str) -> str:
        with self._client._connect() as c:
            cmds = ["host-serial", self._serial, cmd]
            c.send_command(":".join(cmds))
            c.check_okay()
            return c.read_string_block()

    def get_state(self) -> str:
        """ return device state {offline,bootloader,device} """
        return self._get_with_command("get-state")

    def get_serialno(self) -> str:
        """ return the real device id, not the connect serial """
        return self._get_with_command("get-serialno")

    def get_devpath(self) -> str:
        """ example return: usb:12345678Y """
        return self._get_with_command("get-devpath")

    def __repr__(self):
        return "AdbDevice(serial={})".format(self.serial)

    @property
    def sync(self) -> 'Sync':
        return Sync(self._client, self.serial)

    @property
    def prop(self) -> "Property":
        return Property(self)

    def adb_output(self, *args, **kwargs):
        """Run adb command use subprocess and get its content

        Returns:
            string of output

        Raises:
            EnvironmentError
        """
        cmds = [adb_path(), '-s', self._serial
                ] if self._serial else [adb_path()]
        cmds.extend(args)
        cmdline = list2cmdline(map(str, cmds))
        try:
            return subprocess.check_output(cmdline,
                                           stdin=subprocess.DEVNULL,
                                           stderr=subprocess.STDOUT,
                                           shell=True).decode('utf-8')
        except subprocess.CalledProcessError as e:
            if kwargs.get('raise_error', True):
                raise EnvironmentError(
                    "subprocess", cmdline,
                    e.output.decode('utf-8', errors='ignore'))

    def shell(self,
              cmdargs: Union[str, list, tuple],
              stream: bool = False,
              timeout: Optional[float] = None,
              rstrip=True) -> str:
        """Run shell inside device and get it's content

        Args:
            rstrip (bool): strip the last empty line (Default: True)
            stream (bool): return stream instead of string output (Default: False)
            timeout (float): set shell timeout

        Returns:
            string of output

        Raises:
            AdbTimeout

        Examples:
            shell("ls -l")
            shell(["ls", "-l"])
            shell("ls | grep data")
        """
        ret = self._client.shell(self._serial, cmdargs, stream=stream, timeout=timeout)
        if stream:
            return ret
        return ret.rstrip() if rstrip else ret

    def shell2(self,
              cmdargs: Union[str, list, tuple],
              timeout: Optional[float] = None,
              rstrip=True) -> ShellReturn:
        """
        Run shell command with detail output

        Returns:
            ShellOutput
        
        Raises:
            AdbTimeout
        """
        if isinstance(cmdargs, (list, tuple)):
            cmdargs = list2cmdline(cmdargs)
        assert isinstance(cmdargs, str)
        MAGIC = "X4EXIT:"
        newcmd = cmdargs + f"; echo {MAGIC}$?"
        output = self.shell(newcmd, timeout=timeout, rstrip=rstrip)
        rindex = output.rfind(MAGIC)
        if rindex == -1:  # normally will not possible
            raise AdbError("shell output invalid", output)
        returncoode = int(output[rindex + len(MAGIC):])
        return ShellReturn(command=cmdargs,
                           returncode=returncoode,
                           output=output[:rindex])

    def forward(self, local: str, remote: str):
        return self._client.forward(self._serial, local, remote)

    def forward_port(self, remote: Union[int, str]) -> int:
        """ forward remote port to local random port """
        if isinstance(remote, int):
            remote = "tcp:" + str(remote)
        for f in self.forward_list():
            if f.serial == self._serial and f.remote == remote and f.local.startswith(
                    "tcp:"):
                return int(f.local[len("tcp:"):])
        local_port = get_free_port()
        self._client.forward(self._serial, "tcp:" + str(local_port), remote)
        return local_port

    def forward_list(self):
        return self._client.forward_list(self._serial)

    def reverse(self, remote: str, local: str):
        return self._client.reverse(self._serial, remote, local)

    def reverse_list(self):
        return self._client.reverse_list(self._serial)

    def push(self, local: str, remote: str):
        self.adb_output("push", local, remote)

    def create_connection(self, network: Network, address: Union[int, str]):
        """
        Used to connect a socket (unix of tcp) on the device

        Returns:
            socket object

        Raises:
            AssertionError, ValueError
        """
        c = self._client._connect()
        c.send_command("host:transport:" + self._serial)
        c.check_okay()
        if network == Network.TCP:
            assert isinstance(address, int)
            c.send_command("tcp:" + str(address))
            c.check_okay()
        elif network in [Network.UNIX, Network.LOCAL_ABSTRACT]:
            assert isinstance(address, str)
            c.send_command("localabstract:" + address)
            c.check_okay()
        elif network in [Network.LOCAL_FILESYSTEM, Network.LOCAL, Network.DEV, Network.LOCAL_RESERVED]:
            c.send_command(network + ":" + address)
            c.check_okay()
        else:
            raise ValueError("Unsupported network type", network)
        return c.conn

    def root(self):
        """ just implemented, not tested """
        # Ref: https://github.com/Swind/pure-python-adb/blob/master/ppadb/command/transport/__init__.py#L179
        with self._client._connect() as c:
            c.send_command("root:")
            c.check_okay()
            return c.read_string_block()


class Property():
    def __init__(self, d: BaseDevice):
        self._d = d

    def __str__(self):
        return f"product:{self.name} model:{self.model} device:{self.device}"

    def get(self, name: str, cache=True) -> str:
        if cache and name in self._d._properties:
            return self._d._properties[name]
        value = self._d._properties[name] = self._d.shell(['getprop', name]).strip()
        return value

    @property
    def name(self):
        return self.get("ro.product.name", cache=True)

    @property
    def model(self):
        return self.get("ro.product.model", cache=True)

    @property
    def device(self):
        return self.get("ro.product.device", cache=True)


_OKAY = "OKAY"
_FAIL = "FAIL"
_DENT = "DENT"  # Directory Entity
_DONE = "DONE"
_DATA = "DATA"


class Sync():
    def __init__(self, adbclient: BaseClient, serial: str):
        self._adbclient = adbclient
        self._serial = serial

    @contextmanager
    def _prepare_sync(self, path: str, cmd: str):
        c = self._adbclient._connect()
        try:
            c.send_command(":".join(["host", "transport", self._serial]))
            c.check_okay()
            c.send_command("sync:")
            c.check_okay()
            # {COMMAND}{LittleEndianPathLength}{Path}
            c.conn.send(
                cmd.encode("utf-8") + struct.pack("<I", len(path)) +
                path.encode("utf-8"))
            yield c
        finally:
            c.close()

    def exists(self, path: str) -> bool:
        finfo = self.stat(path)
        return finfo.mtime is not None

    def stat(self, path: str) -> FileInfo:
        with self._prepare_sync(path, "STAT") as c:
            assert "STAT" == c.read_string(4)
            mode, size, mtime = struct.unpack("<III", c.conn.recv(12))
            # when mtime is 0, windows will error
            mdtime = datetime.datetime.fromtimestamp(mtime) if mtime else None
            return FileInfo(mode, size, mdtime, path)

    def iter_directory(self, path: str):
        with self._prepare_sync(path, "LIST") as c:
            while 1:
                response = c.read_string(4)
                if response == _DONE:
                    break
                mode, size, mtime, namelen = struct.unpack(
                    "<IIII", c.conn.recv(16))
                name = c.read_string(namelen)
                try:
                    mtime = datetime.datetime.fromtimestamp(mtime)
                except OSError:  # bug in Python 3.6
                    mtime = datetime.datetime.now()
                yield FileInfo(mode, size, mtime, name)

    def list(self, path: str) -> typing.List[str]:
        return list(self.iter_directory(path))

    def push(self,
             src: typing.Union[pathlib.Path, str, bytes, bytearray, typing.BinaryIO],
             dst: typing.Union[pathlib.Path, str],
             mode: int = 0o755,
             check: bool = False) -> int:
        # IFREG: File Regular
        # IFDIR: File Directory
        if isinstance(src, pathlib.Path):
            src = src.open("rb")
        elif isinstance(src, str):
            src = pathlib.Path(src).open("rb")
        elif isinstance(src, (bytes, bytearray)):
            src = io.BytesIO(src)
        else:
            if not hasattr(src, "read"):
                raise TypeError("Invalid src type: %s" % type(src))
        
        if isinstance(dst, pathlib.Path):
            dst = dst.as_posix()
        path = dst + "," + str(stat.S_IFREG | mode)
        total_size = 0
        with self._prepare_sync(path, "SEND") as c:
            r = src if hasattr(src, "read") else open(src, "rb")
            try:
                while True:
                    chunk = r.read(4096)
                    if not chunk:
                        mtime = int(datetime.datetime.now().timestamp())
                        c.conn.send(b"DONE" + struct.pack("<I", mtime))
                        break
                    c.conn.send(b"DATA" + struct.pack("<I", len(chunk)))
                    c.conn.send(chunk)
                    total_size += len(chunk)
                assert c.read_string(4) == _OKAY
            finally:
                if hasattr(r, "close"):
                    r.close()
        if check:
            file_size = self.stat(dst).size
            if total_size != file_size:
                raise AdbError("Push not complete, expect pushed %d, actually pushed %d" % (total_size, file_size))
        return total_size

    def iter_content(self, path: str) -> typing.Iterator[bytes]:
        with self._prepare_sync(path, "RECV") as c:
            while True:
                cmd = c.read_string(4)
                if cmd == _FAIL:
                    str_size = struct.unpack("<I", c.read(4))[0]
                    error_message = c.read_string(str_size)
                    raise AdbError(error_message, path)
                elif cmd == _DONE:
                    break
                elif cmd == _DATA:
                    chunk_size = struct.unpack("<I", c.read(4))[0]
                    chunk = c.read(chunk_size)
                    if len(chunk) != chunk_size:
                        raise RuntimeError("read chunk missing")
                    yield chunk
                else:
                    raise AdbError("Invalid sync cmd", cmd)

    def read_bytes(self, path: str) -> bytes:
        return b''.join(self.iter_content(path))

    def read_text(self, path: str, encoding: str = 'utf-8') -> str:
        """ read content of a file """
        return self.read_bytes(path).decode(encoding=encoding)

    def pull(self, src: str, dst: typing.Union[str, pathlib.Path]) -> int:
        """
        Pull file from device:src to local:dst

        Returns:
            file size
        """
        if isinstance(dst, str):
            dst = pathlib.Path(dst)
        with dst.open("wb") as f:
            size = 0
            for chunk in self.iter_content(src):
                f.write(chunk)
                size += len(chunk)
            return size


class AdbDevice(BaseDevice):
    """ provide custom functions for some complex operations """

    def screenshot(self) -> Image.Image:
        """ not thread safe """
        try:
            inner_tmp_path = "/sdcard/tmp001.png"
            self.shell(['rm', inner_tmp_path])
            self.shell(["screencap", "-p", inner_tmp_path])

            with tempfile.TemporaryDirectory() as tmpdir:
                target_path = os.path.join(tmpdir, "tmp001.png")
                self.sync.pull(inner_tmp_path, target_path)
                im = Image.open(target_path)
                im.load()
                self._width, self._height = im.size
                return im.convert("RGB")
        except UnidentifiedImageError:
            w, h = self.window_size()
            return Image.new("RGB", (w, h), (220, 120, 100))

    def switch_screen(self, status: bool):
        """
        turn screen on/off

        Args:
            status (bool)
        """
        _key_dict = {
            True: '224',
            False: '223',
        }
        return self.keyevent(_key_dict[status])

    def switch_airplane(self, status: bool):
        """
        turn airplane-mode on/off

        Args:
            status (bool)
        """
        base_setting_cmd = ["settings", "put", "global", "airplane_mode_on"]
        base_am_cmd = [
            "am", "broadcast", "-a", "android.intent.action.AIRPLANE_MODE",
            "--ez", "state"
        ]
        if status:
            base_setting_cmd += ['1']
            base_am_cmd += ['true']
        else:
            base_setting_cmd += ['0']
            base_am_cmd += ['false']

        # TODO better idea about return value?
        self.shell(base_setting_cmd)
        return self.shell(base_am_cmd)

    def switch_wifi(self, status: bool) -> str:
        """
        turn WiFi on/off

        Args:
            status (bool)
        """
        arglast = 'enable' if status else 'disable'
        cmdargs = ['svc', 'wifi', arglast]
        return self.shell(cmdargs)

    def keyevent(self, key_code: typing.Union[int, str]) -> str:
        """ adb _run input keyevent KEY_CODE """
        return self.shell(['input', 'keyevent', str(key_code)])

    def click(self, x, y):
        """
        simulate android tap

        Args:
            x, y: int
        """
        x, y = map(str, [x, y])
        return self.shell(['input', 'tap', x, y])

    def swipe(self, sx, sy, ex, ey, duration: float = 1.0):
        """
        swipe from start point to end point

        Args:
            sx, sy: start point(x, y)
            ex, ey: end point(x, y)
        """
        x1, y1, x2, y2 = map(str, [sx, sy, ex, ey])
        return self.shell(
            ['input', 'swipe', x1, y1, x2, y2,
             str(int(duration * 1000))])

    def send_keys(self, text: str):
        """ 
        Type a given text 

        Args:
            text: text to be type
        """
        escaped_text = self._escape_special_characters(text)
        return self.shell(['input', 'text', escaped_text])

    @staticmethod
    def _escape_special_characters(text):
        """
        A helper that escape special characters

        Args:
            text: str
        """
        escaped = text.translate(
            str.maketrans({
                "-": r"\-",
                "+": r"\+",
                "[": r"\[",
                "]": r"\]",
                "(": r"\(",
                ")": r"\)",
                "{": r"\{",
                "}": r"\}",
                "\\": r"\\\\",
                "^": r"\^",
                "$": r"\$",
                "*": r"\*",
                ".": r"\.",
                ",": r"\,",
                ":": r"\:",
                "~": r"\~",
                ";": r"\;",
                ">": r"\>",
                "<": r"\<",
                "%": r"\%",
                "#": r"\#",
                "\'": r"\\'",
                "\"": r'\\"',
                "`": r"\`",
                "!": r"\!",
                "?": r"\?",
                "|": r"\|",
                "=": r"\=",
                "@": r"\@",
                "/": r"\/",
                "_": r"\_",
                " ": r"%s",  # special
                "&": r"\&"
            }))
        return escaped

    def wlan_ip(self) -> str:
        """
        get device wlan ip

        Raises:
            AdbError
        """
        result = self.shell(['ifconfig', 'wlan0'])
        m = re.search(r'inet\s*addr:(.*?)\s', result, re.DOTALL)
        if m:
            return m.group(1)

        # Huawei P30, has no ifconfig
        result = self.shell(['ip', 'addr', 'show', 'dev', 'wlan0'])
        m = re.search(r'inet (\d+.*?)/\d+', result)
        if m:
            return m.group(1)

        # On VirtualDevice, might use eth0
        result = self.shell(['ifconfig', 'eth0'])
        m = re.search(r'inet\s*addr:(.*?)\s', result, re.DOTALL)
        if m:
            return m.group(1)

        raise AdbError("fail to parse wlan ip")


    @retry(BrokenPipeError, delay=5.0, jitter=[3, 5], tries=3)
    def install(self,
                path_or_url: str,
                nolaunch: bool = False,
                uninstall: bool = False,
                silent: bool = False,
                callback: typing.Callable[[str], None] = None):
        """
        Install APK to device

        Args:
            path_or_url: local path or http url
            nolaunch: do not launch app after install
            uninstall: uninstall app before install
            silent: disable log message print
            callback: only two event now: <"BEFORE_INSTALL" | "FINALLY">
        
        Raises:
            AdbInstallError, BrokenPipeError
        """
        if re.match(r"^https?://", path_or_url):
            resp = requests.get(path_or_url, stream=True)
            resp.raise_for_status()
            length = int(resp.headers.get("Content-Length", 0))
            r = ReadProgress(resp.raw, length)
            print("tmpfile path:", r.filepath())
        else:
            length = os.stat(path_or_url).st_size
            fd = open(path_or_url, "rb")
            r = ReadProgress(fd, length, source_path=path_or_url)

        def _dprint(*args):
            if not silent:
                print(*args)

        dst = "/data/local/tmp/tmp-%d.apk" % (int(time.time() * 1000))
        _dprint("push to %s" % dst)

        start = time.time()
        self.sync.push(r, dst)

        # parse apk package-name
        apk = apkutils2.APK(r.filepath())
        package_name = apk.manifest.package_name
        main_activity = apk.manifest.main_activity
        if main_activity and main_activity.find(".") == -1:
            main_activity = "." + main_activity

        version_name = apk.manifest.version_name
        _dprint("packageName:", package_name)
        _dprint("mainActivity:", main_activity)
        _dprint("apkVersion: {}".format(version_name))
        _dprint("Success pushed, time used %d seconds" % (time.time() - start))

        new_dst = "/data/local/tmp/{}-{}.apk".format(package_name,
                                                     version_name)
        self.shell(["mv", dst, new_dst])

        dst = new_dst
        info = self.sync.stat(dst)
        print("verify pushed apk, md5: %s, size: %s" %
              (r._hash, humanize(info.size)))
        assert info.size == r.copied

        if uninstall:
            _dprint("Uninstall app first")
            self.uninstall(package_name)

        _dprint("install to android system ...")
        try:
            start = time.time()
            if callback:
                callback("BEFORE_INSTALL")

            self.install_remote(dst, clean=True)
            _dprint("Success installed, time used %d seconds" %
                    (time.time() - start))
            if not nolaunch:
                _dprint("Launch app: %s/%s" % (package_name, main_activity))
                self.app_start(package_name, main_activity)

        except AdbInstallError as e:
            if e.reason in [
                    "INSTALL_FAILED_PERMISSION_MODEL_DOWNGRADE",
                    "INSTALL_FAILED_UPDATE_INCOMPATIBLE",
                    "INSTALL_FAILED_VERSION_DOWNGRADE"
            ]:
                _dprint("uninstall %s because %s" % (package_name, e.reason))
                self.uninstall(package_name)
                self.install_remote(dst, clean=True)
                _dprint("Success installed, time used %d seconds" %
                        (time.time() - start))
                if not nolaunch:
                    _dprint("Launch app: %s/%s" %
                            (package_name, main_activity))
                    self.app_start(package_name, main_activity)
                    # self.shell([
                    #     'am', 'start', '-n', package_name + "/" + main_activity
                    # ])
            elif e.reason == "INSTALL_FAILED_CANCELLED_BY_USER":
                _dprint("Catch error %s, reinstall" % e.reason)
                self.install_remote(dst, clean=True)
                _dprint("Success installed, time used %d seconds" %
                        (time.time() - start))
            else:
                # print to console
                print(
                    "Failure " + e.reason + "\n" +
                    "Remote apk is not removed. Manually install command:\n\t"
                    + "adb shell pm install -r -t " + dst)
                raise
        finally:
            if callback:
                callback("FINALLY")

    def install_remote(self,
                       remote_path: str,
                       clean: bool = False,
                       flags: list = ["-r", "-t"]):
        """
        Args:
            remote_path: remote package path
            clean(bool): remove when installed, default(False)
            flags (list): default ["-r", "-t"]

        Raises:
            AdbInstallError
        """
        args = ["pm", "install"] + flags + [remote_path]
        output = self.shell(args)
        if "Success" not in output:
            raise AdbInstallError(output)
        if clean:
            self.shell(["rm", remote_path])

    def uninstall(self, pkg_name: str):
        """
        Uninstall app by package name

        Args:
            pkg_name (str): package name
        """
        return self.shell(["pm", "uninstall", pkg_name])

    def getprop(self, prop: str) -> str:
        return self.shell(['getprop', prop]).strip()

    def list_packages(self) -> typing.List[str]:
        """
        Returns:
            list of package names
        """
        result = []
        output = self.shell(["pm", "list", "packages"])
        for m in re.finditer(r'^package:([^\s]+)\r?$', output, re.M):
            result.append(m.group(1))
        return list(sorted(result))

    def package_info(self, package_name: str) -> typing.Union[dict, None]:
        """
        version_code might be empty

        Returns:
            None or dict(version_name, version_code, signature)
        """
        output = self.shell(['dumpsys', 'package', package_name])
        m = re.compile(r'versionName=(?P<name>[\w.]+)').search(output)
        version_name = m.group('name') if m else ""
        m = re.compile(r'versionCode=(?P<code>\d+)').search(output)
        version_code = m.group('code') if m else ""
        if version_code == "0":
            version_code = ""
        m = re.search(r'PackageSignatures\{.*?\[(.*)\]\}', output)
        signature = m.group(1) if m else None
        if not version_name and signature is None:
            return None
        m = re.compile(r"pkgFlags=\[\s*(.*)\s*\]").search(output)
        pkgflags = m.group(1) if m else ""
        pkgflags = pkgflags.split()

        time_regex = r"[-\d]+\s+[:\d]+"
        m = re.compile(f"firstInstallTime=({time_regex})").search(output)
        first_install_time = datetime.datetime.strptime(
            m.group(1), "%Y-%m-%d %H:%M:%S") if m else None

        m = re.compile(f"lastUpdateTime=({time_regex})").search(output)
        last_update_time = datetime.datetime.strptime(
            m.group(1).strip(), "%Y-%m-%d %H:%M:%S") if m else None

        return dict(package_name=package_name,
                    version_name=version_name,
                    version_code=version_code,
                    flags=pkgflags,
                    first_install_time=first_install_time,
                    last_update_time=last_update_time,
                    signature=signature)

    def rotation(self) -> int:
        """
        Returns:
            int [0, 1, 2, 3]
        """
        for line in self.shell('dumpsys display').splitlines():
            m = _DISPLAY_RE.search(line, 0)
            if not m:
                continue
            o = int(m.group('orientation'))
            return int(o)

        output = self.shell(
            'LD_LIBRARY_PATH=/data/local/tmp /data/local/tmp/minicap -i')
        try:
            if output.startswith('INFO:'):
                output = output[output.index('{'):]
            data = json.loads(output)
            return data['rotation'] / 90
        except ValueError:
            pass

        raise AdbError("rotation get failed")

    def _raw_window_size(self) -> WindowSize:
        output = self.shell("wm size")
        o = re.search(r"Override size: (\d+)x(\d+)", output)
        m = re.search(r"Physical size: (\d+)x(\d+)", output)
        if o:
            w, h = o.group(1), o.group(2)
            return WindowSize(int(w), int(h))
        elif m:
            w, h = m.group(1), m.group(2)
            return WindowSize(int(w), int(h))

        for line in self.shell('dumpsys display').splitlines():
            m = _DISPLAY_RE.search(line, 0)
            if not m:
                continue
            w = int(m.group('width'))
            h = int(m.group('height'))
            return WindowSize(w, h)
        raise AdbError("get window size failed")

    def window_size(self) -> WindowSize:
        """
        Return screen (width, height)

        Virtual keyborad may get small d.info['displayHeight']
        """
        w, h = self._raw_window_size()
        s, l = min(w, h), max(w, h)
        horizontal = self.rotation() % 2 == 1
        return WindowSize(l, s) if horizontal else WindowSize(s, l)

    def app_start(self, package_name: str, activity: str = None):
        """ start app with "am start" or "monkey"
        """
        if activity:
            self.shell(['am', 'start', '-n', package_name + "/" + activity])
        else:
            self.shell([
                "monkey", "-p", package_name, "-c",
                "android.intent.category.LAUNCHER", "1"
            ])

    def app_stop(self, package_name: str):
        """ stop app with "am force-stop"
        """
        self.shell(['am', 'force-stop', package_name])

    def app_clear(self, package_name: str):
        self.shell(["pm", "clear", package_name])

    def is_screen_on(self):
        output = self.shell(["dumpsys", "power"])
        return 'mHoldingDisplaySuspendBlocker=true' in output

    def open_browser(self, url: str):
        if not re.match("^https?://", url):
            url = "https://" + url
        self.shell(
            ['am', 'start', '-a', 'android.intent.action.VIEW', '-d', url])

    def dump_hierarchy(self) -> str:
        """
        uiautomator dump

        Returns:
            content of xml
        
        Raises:
            AdbError
        """
        output = self.shell(
            'uiautomator dump /data/local/tmp/uidump.xml && echo success')
        if "success" not in output:
            raise AdbError("uiautomator dump failed", output)

        buf = b''
        for chunk in self.sync.iter_content("/data/local/tmp/uidump.xml"):
            buf += chunk
        return buf.decode("utf-8")

    @deprecated(deprecated_in="0.15.0",
                removed_in="0.1.0",
                current_version=__version__,
                details="Use app_current instead")
    def current_app(self) -> dict:
        """
        Returns:
            dict(package, activity, pid?)

        Raises:
            AdbError
        """
        info = self.app_current()
        return asdict(info)

    @retry(AdbError, delay=.5, tries=3, jitter=.1)
    def app_current(self) -> RunningAppInfo:
        """
        Returns:
            RunningAppInfo(package, activity, pid?)  pid can be 0

        Raises:
            AdbError
        """
        # Related issue: https://github.com/openatx/uiautomator2/issues/200
        # $ adb shell dumpsys window windows
        # Example output:
        #   mCurrentFocus=Window{41b37570 u0 com.incall.apps.launcher/com.incall.apps.launcher.Launcher}
        #   mFocusedApp=AppWindowToken{422df168 token=Token{422def98 ActivityRecord{422dee38 u0 com.example/.UI.play.PlayActivity t14}}}
        # Regexp
        #   r'mFocusedApp=.*ActivityRecord{\w+ \w+ (?P<package>.*)/(?P<activity>.*) .*'
        #   r'mCurrentFocus=Window{\w+ \w+ (?P<package>.*)/(?P<activity>.*)\}')
        _focusedRE = re.compile(
            r'mCurrentFocus=Window{.*\s+(?P<package>[^\s]+)/(?P<activity>[^\s]+)\}'
        )
        m = _focusedRE.search(self.shell(['dumpsys', 'window', 'windows']))
        if m:
            return RunningAppInfo(package=m.group('package'),
                               activity=m.group('activity'))

        # search mResumedActivity
        # https://stackoverflow.com/questions/13193592/adb-android-getting-the-name-of-the-current-activity
        package = None
        output = self.shell(['dumpsys', 'activity', 'activities'])
        _recordRE = re.compile(r'mResumedActivity: ActivityRecord\{.*?\s+(?P<package>[^\s]+)/(?P<activity>[^\s]+)\s.*?\}')
        m = _recordRE.search(output)
        if m:
            package = m.group("package")

        # try: adb shell dumpsys activity top
        _activityRE = re.compile(
            r'ACTIVITY (?P<package>[^\s]+)/(?P<activity>[^/\s]+) \w+ pid=(?P<pid>\d+)'
        )
        output = self.shell(['dumpsys', 'activity', 'top'])
        ms = _activityRE.finditer(output)
        ret = None
        for m in ms:
            ret = RunningAppInfo(package=m.group('package'),
                                 activity=m.group('activity'),
                                 pid=int(m.group('pid')))
            if ret.package == package:
                return ret

        if ret:  # get last result
            return ret
        raise AdbError("Couldn't get focused app")

    def remove(self, path: str):
        """ rm device file """
        self.shell(["rm", path])


    def screenrecord(self, remote_path=None, no_autostart=False):
        """
        Args:
            remote_path: device video path
            no_autostart: do not start screenrecord, when call this method
        """
        # self.shell2("screenrecord -h")
        return _ScreenRecord(self, remote_path, autostart=not no_autostart)


class _ScreenRecord():
    def __init__(self, d: AdbDevice, remote_path=None, autostart=False):
        """ The maxium record time is 3 minutes """
        self._d = d
        if not remote_path:
            remote_path = "/sdcard/video-%d.mp4" % int(time.time() * 1000)
        self._remote_path = remote_path
        self._stream = None
        self._stopped = False
        self._started = False

        if autostart:
            self.start()

    def start(self):
        """ start recording """
        if self._started:
            warnings.warn("screenrecord already started", UserWarning)
            return
        self._stream: AdbConnection = self._d.shell(["screenrecord", self._remote_path],
                                     stream=True)
        self._started = True

    def stop(self):
        """ stop recording """
        if not self._started:
            raise RuntimeError("screenrecord is not started")

        if self._stopped:
            return
        self._stream.send(b"\003")
        self._stream.read_until_close()
        self._stream.close()
        self._stopped = True

    def stop_and_pull(self, path: typing.Union[str, pathlib.Path]):
        """ pull remote to local and remove remote file """
        if isinstance(path, pathlib.Path):
            path = path.as_posix()
        self.stop()
        self._d.sync.pull(self._remote_path, path)
        self._d.remove(self._remote_path)

    def close(self):  # alias of stop
        return self.stop()

    def close_and_pull(self, path: str):  # alias of stop_and_pull
        return self.stop_and_pull(path=path)
