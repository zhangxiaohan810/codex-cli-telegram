#!/usr/bin/env python3

import argparse
import errno
import fcntl
import json
import os
import pty
import select
import signal
import socket
import sys
import termios
import time
import tty


def main():
    parser = argparse.ArgumentParser(description="Run Codex CLI in a PTY with a local input control socket.")
    parser.add_argument("--control-host", default="127.0.0.1")
    parser.add_argument("--control-port", type=int, default=8767)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if not args.command:
        print("missing command", file=sys.stderr)
        return 2

    old_tty = None
    if sys.stdin.isatty():
        old_tty = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

    server = None
    child_pid = None
    master_fd = None
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.control_host, args.control_port))
        server.listen(16)
        server.setblocking(False)
        print(f"[codex-pty] control listening on {args.control_host}:{args.control_port}", file=sys.stderr)

        child_pid, master_fd = pty.fork()
        if child_pid == 0:
            os.execvpe(args.command[0], args.command, os.environ)

        resize_pty(master_fd)

        def on_winch(signum, frame):
            resize_pty(master_fd)

        signal.signal(signal.SIGWINCH, on_winch)
        return run_loop(child_pid, master_fd, server)
    finally:
        if old_tty is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_tty)
        if server is not None:
            server.close()
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass


def run_loop(child_pid, master_fd, server):
    clients = {}
    stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    stdout_fd = sys.stdout.fileno()

    while True:
        exited = os.waitpid(child_pid, os.WNOHANG)
        if exited[0] == child_pid:
            return exit_status(exited[1])

        read_fds = [master_fd, server]
        if stdin_fd is not None:
            read_fds.append(stdin_fd)
        read_fds.extend(clients.keys())

        ready, _, _ = select.select(read_fds, [], [], 0.2)
        for item in ready:
            if item == master_fd:
                data = safe_read(master_fd, 65536)
                if not data:
                    _, status = os.waitpid(child_pid, 0)
                    return exit_status(status)
                os.write(stdout_fd, data)
            elif item == stdin_fd:
                data = safe_read(stdin_fd, 4096)
                if data:
                    os.write(master_fd, data)
            elif item == server:
                conn, _addr = server.accept()
                conn.setblocking(False)
                clients[conn] = bytearray()
            else:
                data = safe_socket_read(item)
                if data:
                    clients[item].extend(data)
                else:
                    payload = bytes(clients.pop(item))
                    handle_control_request(item, payload, master_fd)


def handle_control_request(conn, payload, master_fd):
    try:
        request = json.loads(payload.decode("utf-8"))
        action = request.get("action")
        if action not in ("write", "submit"):
            raise ValueError("unsupported action")
        text = request.get("text")
        if not isinstance(text, str):
            raise ValueError("text must be a string")

        if action == "submit":
            submit_text(master_fd, text, request)
        else:
            os.write(master_fd, text.encode("utf-8"))
        response = {"ok": True}
    except Exception as exc:
        response = {"ok": False, "error": str(exc)}

    try:
        conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
    finally:
        conn.close()


def submit_text(master_fd, text, request):
    char_delay_ms = request.get("charDelayMs", 12)
    submit_delay_ms = request.get("submitDelayMs", 80)
    enter = decode_enter_sequence(request.get("enter", "cr"))

    for char in text:
        os.write(master_fd, char.encode("utf-8"))
        if char_delay_ms > 0:
            time.sleep(char_delay_ms / 1000)

    if submit_delay_ms > 0:
        time.sleep(submit_delay_ms / 1000)
    os.write(master_fd, enter.encode("utf-8"))


def decode_enter_sequence(value):
    if value == "lf":
        return "\n"
    if value == "crlf":
        return "\r\n"
    return "\r"


def resize_pty(master_fd):
    if not sys.stdout.isatty():
        return
    try:
        size = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def safe_read(fd, size):
    try:
        return os.read(fd, size)
    except OSError as exc:
        if exc.errno in (errno.EIO, errno.EBADF):
            return b""
        raise


def safe_socket_read(conn):
    try:
        return conn.recv(65536)
    except BlockingIOError:
        return b""


def exit_status(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
