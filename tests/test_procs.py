"""Linux process facts against a fake /proc tree. Runs on any OS."""
import os

from hunch import cdp
from hunch.procs import (linux_endpoint_listeners, linux_proc_cmdline, linux_process_alive,
                         linux_running_identity)


def _stat(pid, comm, state, ppid, start):
    tail = [state, str(ppid)] + ["0"] * 17 + [str(start)]
    return f"{pid} ({comm}) " + " ".join(tail) + "\n"


def _tcp_line(port, inode):
    return (f"   0: 0100007F:{port:04X} 00000000:0000 0A 00000000:00000000 "
            f"00:00000000 00000000 0 0 {inode} 1 0000000000000000 100 0 0 10 0\n")


def _proc(root, pid, *, comm="chrome", state="S", ppid=1, start=100, exe="chrome",
          cmdline=b"", sockets=()):
    proc = root / str(pid)
    proc.mkdir()
    (proc / "stat").write_text(_stat(pid, comm, state, ppid, start))
    binary = root / "bin" / exe
    binary.parent.mkdir(exist_ok=True)
    binary.write_text("")
    (proc / "exe").symlink_to(binary)
    (proc / "cmdline").write_bytes(cmdline)
    fd = proc / "fd"
    fd.mkdir()
    for index, inode in enumerate(sockets):
        (fd / str(index)).symlink_to(f"socket:[{inode}]")


def _tree(tmp_path, boot="boot-1"):
    boot_id = tmp_path / "sys" / "kernel" / "random"
    boot_id.mkdir(parents=True)
    (boot_id / "boot_id").write_text(boot + "\n")
    net = tmp_path / "net"
    net.mkdir()
    return tmp_path


def test_linux_identity_uses_boot_id_and_real_executable(tmp_path):
    root = _tree(tmp_path)
    _proc(root, 10, comm="google-chrome", start=4242, exe="chrome-bin")
    identity = linux_running_identity(10, root)
    assert identity["pid"] == 10
    assert identity["started_at"] == "boot-1:4242"
    assert identity["bundle_id"] == "" and identity["version"] == "" and identity["build"] == ""
    assert identity["path"] == os.path.realpath(root / "bin" / "chrome-bin")
    assert identity["name"] == "google-chrome"


def test_linux_listeners_accept_a_forked_child_on_the_same_socket(tmp_path):
    root = _tree(tmp_path)
    (root / "net" / "tcp").write_text("sl local rem st\n" + _tcp_line(9222, 555))
    _proc(root, 10, ppid=1, sockets=("555",))
    _proc(root, 20, ppid=10, sockets=("555",))
    listeners = linux_endpoint_listeners(9222, root)
    assert cdp._endpoint_owner(listeners) == 10


def test_linux_listeners_refuse_two_unrelated_sockets(tmp_path):
    root = _tree(tmp_path)
    (root / "net" / "tcp").write_text("sl local rem st\n" + _tcp_line(9222, 555) + _tcp_line(9222, 777))
    _proc(root, 10, ppid=1, sockets=("555",))
    _proc(root, 30, ppid=1, sockets=("777",))
    assert cdp._endpoint_owner(linux_endpoint_listeners(9222, root)) is None


def test_linux_cmdline_and_zombie(tmp_path):
    root = _tree(tmp_path)
    _proc(root, 10, cmdline=b"chrome\0--user-data-dir=/tmp/profile\0")
    _proc(root, 11, state="Z")
    assert linux_proc_cmdline(10, root) == "chrome --user-data-dir=/tmp/profile"
    assert linux_process_alive(10, root) is True
    assert linux_process_alive(11, root) is False
    assert linux_running_identity(11, root) == {}
    assert linux_process_alive(99, root) is False


def test_decimal_socket_inode_is_a_single_kernel_socket():
    listeners = {10: {"parent": 1, "sockets": {"555"}},
                 20: {"parent": 10, "sockets": {"555"}}}
    assert cdp._endpoint_owner(listeners) == 10
    assert cdp._endpoint_owner({10: {"parent": 1, "sockets": {"0"}},
                                20: {"parent": 10, "sockets": {"0"}}}) is None
    assert cdp._endpoint_owner({10: {"parent": 1, "sockets": {"0x0"}},
                                20: {"parent": 10, "sockets": {"0x0"}}}) is None


def test_linux_app_path_prefers_an_absolute_path_then_hunch_chrome(tmp_path, monkeypatch):
    binary = tmp_path / "chrome"
    binary.write_bytes(b"\x7fELF")
    binary.chmod(0o755)
    wrapper = tmp_path / "google-chrome-stable"
    wrapper.write_text("#!/bin/sh\nexec chrome\n")
    from hunch.procs import linux_app_path
    assert linux_app_path(str(binary)) == os.path.realpath(binary)
    monkeypatch.setenv("HUNCH_CHROME", str(wrapper))
    assert linux_app_path("Google Chrome") == os.path.realpath(binary)


def test_bundle_metadata_is_empty_off_darwin(monkeypatch):
    from hunch import targets
    monkeypatch.setattr(targets.sys, "platform", "linux")
    assert targets.bundle_metadata("/usr/bin/google-chrome") == {}
