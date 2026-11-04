#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PySide6-Essentials==6.10.3"]
# ///
"""Optional host tray for the launcher (docs/specs/tray.spec.md)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from PySide6.QtCore import (
    QLockFile,
    QObject,
    QProcess,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QSystemTrayIcon,
    QVBoxLayout,
)

# Simplified geometry from assets/logo.svg, legible at menu-bar size.
LOGO = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<path d="M2 16Q16 0 30 16Q16 32 2 16Z" fill="#0b1120" stroke="#818cf8"
stroke-width="2"/><circle cx="16" cy="16" r="8" fill="#6366f1"/>
<circle cx="16" cy="16" r="5.8" fill="#030712"/>
<path d="m14 13-3 3 3 3m4-6 3 3-3 3" fill="none" stroke="#67e8f9"
stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>"""


def icon(state: str) -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor("transparent"))
    painter = QPainter(pixmap)
    QSvgRenderer(LOGO).render(painter)
    color = {"Ready": "#22c55e", "Stopped": "#94a3b8"}.get(state, "#f59e0b")
    painter.setPen(QColor("#0b1120"))
    painter.setBrush(QColor(color))
    painter.drawEllipse(46, 46, 16, 16)
    painter.end()
    return QIcon(pixmap)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(args[0])
    if not executable:
        raise FileNotFoundError(args[0])
    # Arguments come from the installed launcher/config, never a shell string.
    return subprocess.run(  # noqa: S603
        [executable, *args[1:]],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=8,
        check=False,
    )


def get(port: int, path: str, token: str = "") -> tuple[int, bytes]:
    # HTTPConnection neither uses proxies nor follows redirects.
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(1_048_576)
    finally:
        connection.close()


@dataclass(frozen=True)
class Snapshot:
    state: str
    version: str = ""
    url: str = ""
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class Host:
    launcher: str
    home: str
    distro: str = ""
    user: str = ""

    def command(self, *args: str) -> tuple[str, ...]:
        if self.distro:
            return self.wrap(
                "env", f"ARGUS_HOME={self.home}", "sh", self.launcher, *args
            )
        return ("sh", self.launcher, *args)

    def wrap(self, *args: str) -> tuple[str, ...]:
        if self.distro:
            return (
                "wsl.exe",
                "--distribution",
                self.distro,
                "--user",
                self.user,
                "--exec",
                *args,
            )
        return args


def snapshot(host: Host, token: str) -> Snapshot:
    config = run(*host.command("__tray-config"))
    if config.returncode:
        return Snapshot("Configuration unavailable")
    version, port_text, container = config.stdout.splitlines()
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    url = f"http://127.0.0.1:{port}/"
    result = run(
        *host.wrap(
            "docker", "container", "inspect", "-f", "{{.State.Status}}", container
        )
    )
    if result.returncode:
        daemon = run(*host.wrap("docker", "info", "--format", "{{.ServerVersion}}"))
        state = "Docker unavailable" if daemon.returncode else "Stopped"
        return Snapshot(state, version, url)
    if result.stdout.strip() != "running":
        state = "Restarting" if result.stdout.strip() == "restarting" else "Stopped"
        return Snapshot(state, version, url)

    try:
        status, _ = get(port, "/readyz")
        state = "Ready" if status == 200 else "Unhealthy"
    except (OSError, HTTPException):
        return Snapshot("Unreachable", version, url)

    if not token:
        generated = run(*host.command("token"))
        if not generated.returncode:
            token = generated.stdout.strip()
    if not token:
        return Snapshot(state, version, url, ("Stats unavailable: sign in for stats",))
    try:
        status, body = get(port, "/api/system", token)
        if status != 200:
            message = (
                "Stats unavailable: sign in for stats"
                if status in (401, 403)
                else "Stats unavailable: API error"
            )
            return Snapshot(state, version, url, (message,))
        data = json.loads(body)
        tasks, agents = data["tasks"], data["agents"]
        minutes = int(data["uptime_s"]) // 60
        hours, minutes = divmod(minutes, 60)
        lines = (
            f"Tasks: {tasks.get('claimed', 0)} running · "
            f"{tasks.get('ready', 0)} ready · {tasks.get('blocked', 0)} blocked",
            f"Agents: {agents.get('active', 0)} active · "
            f"{agents.get('draining', 0)} draining",
            f"Inbox: {data['decisions_open']} open",
            f"Uptime: {hours}h {minutes}m",
        )
    except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
        lines = ("Stats unavailable",)
    return Snapshot(state, version, url, lines)


class Refresh(QThread):
    result = Signal(object)

    def __init__(self, host: Host, token: str) -> None:
        super().__init__()
        self.host, self.token = host, token

    def run(self) -> None:
        try:
            result = snapshot(self.host, self.token)
        except (OSError, subprocess.SubprocessError, ValueError):
            result = Snapshot("Status unavailable")
        self.result.emit(result)


class Tray(QObject):
    def __init__(self, app: QApplication, host: Host) -> None:
        super().__init__(app)
        self.app, self.host = app, host
        self.current = Snapshot("Checking…")
        self.token = ""
        self.refresh_job: Refresh | None = None
        self.refresh_again = False
        self.command: QProcess | None = None
        self.output = bytearray()
        self.menu = QMenu()
        self.status = self.menu.addAction("Argus — Checking…")
        self.status.setEnabled(False)
        self.stats = [self.menu.addAction("") for _ in range(4)]
        for row in self.stats:
            row.setEnabled(False)
            row.setVisible(False)
        self.menu.addSeparator()
        self.open_action = self.menu.addAction("Open Argus", self.open_argus)
        self.open_action.setEnabled(False)
        self.controls: dict[str, QAction] = {}
        for label, args in (
            ("Up", ("up", "--no-tray")),
            ("Down", ("down",)),
            ("Restart", ("up", "--no-tray")),
            ("Backup", ("backup",)),
            ("Recent logs…", ("logs", "--tail", "100")),
        ):
            action = self.menu.addAction(label)
            action.triggered.connect(
                lambda checked=False, label=label, args=args: self.run_command(
                    label, args
                )
            )
            action.setEnabled(False)
            self.controls[label] = action
        self.menu.addSeparator()
        self.menu.addAction("Refresh", self.refresh)
        self.menu.addAction("Sign in for stats…", self.sign_in)
        self.quit_action = self.menu.addAction(
            "Quit Tray (leave Argus running)", self.quit
        )
        self.tray = QSystemTrayIcon(icon(self.current.state), app)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.activate)
        self.tray.show()
        self.timer = QTimer(app)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(10_000)
        self.refresh()

    def activate(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_argus()

    def open_argus(self) -> None:
        if self.current.url and not QDesktopServices.openUrl(QUrl(self.current.url)):
            QMessageBox.warning(
                None, "Open Argus", "Could not open the default browser."
            )

    def sign_in(self) -> None:
        token, accepted = QInputDialog.getText(
            None,
            "Sign in for stats",
            "Argus token (kept only for this tray session):",
            QLineEdit.EchoMode.Password,
        )
        if accepted:
            self.token = token.strip()
            self.refresh()

    def refresh(self) -> None:
        if self.command:
            return
        if self.refresh_job:
            self.refresh_again = True
            return
        self.refresh_job = Refresh(self.host, self.token)
        self.refresh_job.result.connect(self.render)
        self.refresh_job.finished.connect(self.refreshed)
        self.refresh_job.start()

    @Slot()
    def refreshed(self) -> None:
        if self.refresh_job:
            self.refresh_job.deleteLater()
        self.refresh_job = None
        if self.refresh_again:
            self.refresh_again = False
            self.refresh()

    @Slot(object)
    def render(self, current: Snapshot) -> None:
        if self.command:
            return
        self.current = current
        title = f"Argus {current.version} — {current.state}".replace("  ", " ")
        self.status.setText(title)
        self.tray.setIcon(icon(current.state))
        self.tray.setToolTip("\n".join((title, *current.lines)))
        self.open_action.setEnabled(bool(current.url))
        for index, row in enumerate(self.stats):
            row.setVisible(index < len(current.lines))
            row.setText(current.lines[index] if index < len(current.lines) else "")
        available = bool(current.url) and current.state not in (
            "Docker unavailable",
            "Configuration unavailable",
            "Status unavailable",
        )
        for label, action in self.controls.items():
            allowed = current.state == "Stopped" if label == "Up" else available
            if label in ("Down", "Restart", "Backup", "Recent logs…"):
                allowed = allowed and current.state != "Stopped"
            action.setEnabled(allowed and self.command is None)

    def run_command(self, label: str, args: tuple[str, ...]) -> None:
        if self.command:
            return
        self.command = QProcess(self.app)
        self.command.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.command.readyReadStandardOutput.connect(self.read_output)
        self.command.finished.connect(
            lambda code, status: self.command_finished(label, code)
        )
        self.command.errorOccurred.connect(
            lambda error: (
                self.command_finished(label, -1)
                if error == QProcess.ProcessError.FailedToStart
                else None
            )
        )
        self.output.clear()
        self.quit_action.setEnabled(False)
        for action in self.controls.values():
            action.setEnabled(False)
        self.status.setText(f"Argus — {label}…")
        program, *arguments = self.host.command(*args)
        self.command.start(program, arguments)
        self.command.closeWriteChannel()

    def read_output(self) -> None:
        if self.command:
            self.output.extend(self.command.readAllStandardOutput().data())
            del self.output[:-65_536]

    def command_finished(self, label: str, code: int) -> None:
        if not self.command:
            return
        self.read_output()
        self.command.deleteLater()
        self.command = None
        self.quit_action.setEnabled(True)
        text = self.output.decode(errors="replace")
        # `up` prints the generated token. Never surface it in GUI output.
        text = "\n".join(
            line for line in text.splitlines() if not line.startswith("Admin token:")
        )
        if self.token:
            text = text.replace(self.token, "[redacted]")
        if code:
            QMessageBox.warning(
                None, f"Argus — {label} failed", text or "Command failed."
            )
        elif label == "Recent logs…":
            dialog = QDialog()
            dialog.setWindowTitle("Argus — Recent logs")
            dialog.resize(800, 500)
            layout = QVBoxLayout(dialog)
            logs = QPlainTextEdit()
            logs.setReadOnly(True)
            logs.setPlainText(text)
            layout.addWidget(logs)
            close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            close.rejected.connect(dialog.reject)
            layout.addWidget(close)
            dialog.exec()
        else:
            self.tray.showMessage("Argus", f"{label} completed.")
        self.render(self.current)
        self.refresh()

    def quit(self) -> None:
        if self.command:
            return
        self.timer.stop()
        self.tray.hide()
        # A bounded read may still be finishing; never destroy a running QThread.
        if self.refresh_job:
            self.refresh_job.wait()
        self.app.quit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--wsl-distro", default="")
    parser.add_argument("--wsl-user", default="")
    parser.add_argument("--wsl-home", default="")
    parser.add_argument("--background", action="store_true")
    args = parser.parse_args()
    if sys.platform not in ("linux", "darwin", "win32"):
        parser.error("the tray supports Linux, macOS and Windows through WSL 2")
    if sys.platform == "win32" and not all(
        (args.wsl_distro, args.wsl_user, args.wsl_home)
    ):
        parser.error("run `argus tray` inside your WSL 2 distribution")
    host = Host(
        args.launcher if args.wsl_distro else str(Path(args.launcher).resolve()),
        args.wsl_home if args.wsl_distro else str(args.home.resolve()),
        args.wsl_distro,
        args.wsl_user,
    )
    os.environ["ARGUS_HOME"] = str(args.home.resolve())
    if args.background:
        # Detach on Windows as well as Unix; the WSL/uv bootstrap can then exit.
        arguments = [arg for arg in sys.argv[1:] if arg != "--background"]
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        try:
            with (args.home / "tray.log").open("ab") as log:
                subprocess.Popen(  # noqa: S603 -- our own interpreter and script
                    [sys.executable, str(Path(__file__).resolve()), *arguments],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=sys.platform != "win32",
                    creationflags=flags,
                )
        except OSError as exc:
            print(f"argus: could not start desktop tray: {exc}", file=sys.stderr)
            return 1
        return 0
    app = QApplication(["argus-tray"])
    app.setApplicationName("Argus")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print(
            "argus: no system tray available; enable a tray "
            "(on GNOME, the AppIndicator extension) and retry",
            file=sys.stderr,
        )
        return 1
    lock = QLockFile(str(args.home / "tray.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        print("argus: a tray is already running for this config", file=sys.stderr)
        return 1
    tray = Tray(app, host)
    app.setWindowIcon(icon("Ready"))
    try:
        return app.exec()
    finally:
        tray.quit()
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
