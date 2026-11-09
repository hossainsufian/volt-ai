"""Desktop window hosting the Volt AI web chat."""

from __future__ import annotations

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices, QIcon
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QMainWindow

from desktop_app.volt_web import STATIC_DIR


class _VoltPage(QWebEnginePage):
    """Keeps Volt inside the window; outside links open in the browser."""

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # noqa: N802
        if url.host() in ("127.0.0.1", "localhost"):
            return True
        QDesktopServices.openUrl(url)
        return False

    def createWindow(self, _type):  # noqa: N802 - links with target=_blank
        page = _VoltPage(self.profile(), self)
        page.urlChanged.connect(lambda u: (QDesktopServices.openUrl(u), page.deleteLater()))
        return page


class VoltChatWindow(QMainWindow):
    def __init__(self, url: str) -> None:
        super().__init__()
        self.setWindowTitle("Volt AI")
        self.setWindowIcon(QIcon(str(STATIC_DIR / "icon-192.png")))
        self.resize(1100, 760)
        self.setMinimumSize(380, 520)
        profile = QWebEngineProfile("volt-ai", self)
        self.view = QWebEngineView(self)
        self.view.setPage(_VoltPage(profile, self.view))
        self.view.page().profile().downloadRequested.connect(self._download)
        self.view.setUrl(QUrl(url))
        self.setCentralWidget(self.view)

    def _download(self, item) -> None:
        from pathlib import Path
        folder = Path.home() / "Pictures" / "Volt AI"
        folder.mkdir(parents=True, exist_ok=True)
        item.setDownloadDirectory(str(folder))
        item.accept()
