from __future__ import annotations

import asyncio
import ctypes
import io
import json
import math
import os
import platform
import re
import sys
import threading
import urllib.parse
import wave
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import winsound
except ImportError:
    winsound = None

from PyQt6.QtCore import QTimer, Qt, pyqtSignal, QObject
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from telethon import TelegramClient, events, errors, utils
from telethon.tl.types import Channel, Chat, User


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = Path.home() / ".attention_switch"
CONFIG_PATH = APP_DIR / "config.json"
BASE_DIR = app_base_dir()
ENV_PATH = BASE_DIR / ".env"
DEFAULT_ALERT_TEXT = (
    "ВНИМАНИЕ, ПИШЕТ ЛЮБИМАЯ ДЕВУШКА, "
    "СРОЧНО СВЕРНИ ВСЕ ДЕЛА И ПОГОВОРИ С НЕЙ"
)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def configured_alert_text() -> str:
    return env_value("ATTN_ALERT_TEXT", DEFAULT_ALERT_TEXT) or DEFAULT_ALERT_TEXT


load_env_file(ENV_PATH)


def ensure_app_dir() -> Path:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    return APP_DIR


def sanitize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return digits or "default"


def session_name_for(api_id: str, phone: str) -> str:
    return f"session_{api_id.strip()}_{sanitize_phone(phone)}"


def format_now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def format_telegram_error(exc: Exception) -> str:
    if isinstance(exc, errors.ApiIdInvalidError):
        return "Неверный API ID или API Hash."
    if isinstance(exc, errors.PhoneNumberInvalidError):
        return "Номер телефона не похож на Telegram-номер."
    if isinstance(exc, errors.PhoneCodeInvalidError):
        return "Код подтверждения неверный."
    if isinstance(exc, errors.PhoneCodeExpiredError):
        return "Код подтверждения уже истек. Запроси новый."
    if isinstance(exc, errors.PasswordHashInvalidError):
        return "Пароль 2FA неверный."
    if isinstance(exc, errors.FloodWaitError):
        return f"Telegram просит подождать {exc.seconds} сек."
    if isinstance(exc, TimeoutError):
        return "Telegram не ответил вовремя."
    return str(exc) or exc.__class__.__name__


class ConfigStore:
    def __init__(self) -> None:
        ensure_app_dir()
        self.data: dict[str, Any] = {
            "api_id": env_value("ATTN_API_ID"),
            "api_hash": env_value("ATTN_API_HASH"),
            "phone": env_value("ATTN_PHONE"),
            "session_name": env_value("ATTN_SESSION_NAME"),
            "monitored_chat_id": None,
            "monitored_chat_title": env_value("ATTN_MONITORED_CHAT_TITLE"),
        }
        self.load()
        self.apply_env_overrides()

    def load(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, dict):
            self.data.update(raw)

    def apply_env_overrides(self) -> None:
        api_id = env_value("ATTN_API_ID")
        api_hash = env_value("ATTN_API_HASH")
        phone = env_value("ATTN_PHONE")
        session_name = env_value("ATTN_SESSION_NAME")
        monitored_chat_id = env_value("ATTN_MONITORED_CHAT_ID")
        monitored_chat_title = env_value("ATTN_MONITORED_CHAT_TITLE")

        if api_id:
            self.data["api_id"] = api_id
        if api_hash:
            self.data["api_hash"] = api_hash
        if phone:
            self.data["phone"] = phone
        if monitored_chat_title:
            self.data["monitored_chat_title"] = monitored_chat_title

        if monitored_chat_id:
            try:
                self.data["monitored_chat_id"] = int(monitored_chat_id)
            except ValueError:
                pass

        if session_name:
            self.data["session_name"] = session_name
        elif self.data.get("api_id") and self.data.get("phone"):
            self.data["session_name"] = session_name_for(
                self.data["api_id"],
                self.data["phone"],
            )

    def save(self) -> None:
        ensure_app_dir()
        CONFIG_PATH.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def update(self, **kwargs: Any) -> None:
        self.data.update(kwargs)
        self.save()

    def has_saved_session(self) -> bool:
        session_name = self.data.get("session_name") or ""
        if not session_name:
            return False
        return (APP_DIR / f"{session_name}.session").exists()


class AlertSound:
    def __init__(self) -> None:
        self.wave_data = self._build_wave_data()

    def _build_wave_data(self) -> bytes:
        buffer = io.BytesIO()
        sample_rate = 44100
        duration = 1.8
        frame_count = int(sample_rate * duration)
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            for index in range(frame_count):
                t = index / sample_rate
                segment = int(t / 0.18) % 2
                frequency = 920 if segment == 0 else 1380
                envelope = 0.58 * (1 - min((t % 0.18) / 0.18, 1.0) * 0.18)
                sample = int(
                    32767
                    * envelope
                    * math.sin(2 * math.pi * frequency * t)
                )
                wav_file.writeframesraw(sample.to_bytes(2, "little", signed=True))
        return buffer.getvalue()

    def play(self) -> None:
        if winsound is None:
            QApplication.beep()
            return
        winsound.PlaySound(
            self.wave_data,
            winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )

    def stop(self) -> None:
        if winsound is not None:
            winsound.PlaySound(None, 0)


class WindowsController:
    @staticmethod
    def minimize_all_windows() -> bool:
        if platform.system() != "Windows":
            return False

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        current_pid = kernel32.GetCurrentProcessId()
        shell_classes = {"Shell_TrayWnd", "Progman", "WorkerW"}

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetParent(hwnd):
                return True

            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == current_pid:
                return True

            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_name, 255)
            if class_name.value in shell_classes:
                return True

            if user32.IsIconic(hwnd):
                return True

            user32.ShowWindow(hwnd, 11)
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        return True

    @staticmethod
    def open_telegram_chat(link: str) -> bool:
        if platform.system() != "Windows" or not hasattr(os, "startfile"):
            return False
        try:
            os.startfile(link)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False


class TelegramService(QObject):
    status = pyqtSignal(str)
    code_requested = pyqtSignal()
    password_requested = pyqtSignal()
    authorized = pyqtSignal(str)
    chats_loaded = pyqtSignal(object)
    monitoring_changed = pyqtSignal(object)
    alert_triggered = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.client: TelegramClient | None = None
        self.dialog_cache: dict[int, dict[str, Any]] = {}
        self.monitored_chat_id: int | None = None
        self.phone = ""
        self.api_id = ""
        self.api_hash = ""
        self.session_name = ""

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coro: Any) -> Future[Any]:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        future.add_done_callback(self._handle_future)
        return future

    def _handle_future(self, future: Future[Any]) -> None:
        try:
            future.result()
        except errors.SessionPasswordNeededError:
            self.password_requested.emit()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.error.emit(format_telegram_error(exc))

    def start_login(self, api_id: str, api_hash: str, phone: str) -> None:
        self.api_id = api_id.strip()
        self.api_hash = api_hash.strip()
        self.phone = phone.strip()
        self.session_name = session_name_for(self.api_id, self.phone)
        self._submit(self._start_login())

    def submit_code(self, code: str) -> None:
        self._submit(self._submit_code(code))

    def submit_password(self, password: str) -> None:
        self._submit(self._submit_password(password))

    def refresh_chats(self) -> None:
        self._submit(self._load_chats())

    def monitor_chat(self, peer_id: int) -> None:
        self.monitored_chat_id = peer_id
        chat = self.dialog_cache.get(peer_id)
        self.monitoring_changed.emit(chat)

    def shutdown(self) -> None:
        if not self.loop.is_running():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop)
            future.result(timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)

    async def _start_login(self) -> None:
        self.status.emit("Подключаюсь к Telegram...")
        await self._disconnect()
        session_path = ensure_app_dir() / self.session_name
        self.client = TelegramClient(str(session_path), int(self.api_id), self.api_hash)
        self.client.add_event_handler(
            self._handle_new_message,
            events.NewMessage(incoming=True),
        )
        await self.client.connect()
        if await self.client.is_user_authorized():
            await self._finish_authorization()
            return
        await self.client.send_code_request(self.phone)
        self.status.emit("Код отправлен. Введи его в следующем шаге.")
        self.code_requested.emit()

    async def _submit_code(self, code: str) -> None:
        if self.client is None:
            raise RuntimeError("Сначала подключись к Telegram.")
        try:
            await self.client.sign_in(phone=self.phone, code=code.strip())
        except errors.SessionPasswordNeededError:
            self.status.emit("Аккаунт защищен 2FA. Нужен пароль.")
            self.password_requested.emit()
            return
        await self._finish_authorization()

    async def _submit_password(self, password: str) -> None:
        if self.client is None:
            raise RuntimeError("Сначала подключись к Telegram.")
        await self.client.sign_in(password=password)
        await self._finish_authorization()

    async def _finish_authorization(self) -> None:
        if self.client is None:
            return
        me = await self.client.get_me()
        self.status.emit("Сессия готова. Загружаю чаты...")
        self.authorized.emit(utils.get_display_name(me) or "Telegram")
        await self._load_chats()

    async def _load_chats(self) -> None:
        if self.client is None:
            raise RuntimeError("Нет активной Telegram-сессии.")
        dialogs: list[dict[str, Any]] = []
        self.dialog_cache.clear()
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            peer_id = utils.get_peer_id(entity)
            chat = self._chat_to_dict(entity, dialog.name or "Без названия")
            chat["peer_id"] = peer_id
            chat["last_text"] = ""
            if dialog.message is not None:
                raw = (dialog.message.message or "").strip()
                chat["last_text"] = raw[:110] + ("..." if len(raw) > 110 else "")
            self.dialog_cache[peer_id] = chat
            dialogs.append(chat)
        dialogs.sort(key=lambda item: item["title"].lower())
        self.status.emit(f"Чаты загружены: {len(dialogs)}.")
        self.chats_loaded.emit(dialogs)
        if self.monitored_chat_id in self.dialog_cache:
            self.monitoring_changed.emit(self.dialog_cache[self.monitored_chat_id])

    def _chat_to_dict(self, entity: User | Chat | Channel, title: str) -> dict[str, Any]:
        kind = "chat"
        username = getattr(entity, "username", None)
        phone = getattr(entity, "phone", None)
        supports_precise_open = False
        if isinstance(entity, User):
            kind = "person"
            supports_precise_open = bool(username or phone)
        elif isinstance(entity, Channel):
            kind = "channel" if entity.broadcast else "group"
            supports_precise_open = True
        elif isinstance(entity, Chat):
            kind = "group"
            supports_precise_open = bool(username)

        subtitle_parts = [kind.upper()]
        if username:
            subtitle_parts.append(f"@{username}")
        elif phone:
            subtitle_parts.append(phone)
        if not supports_precise_open:
            subtitle_parts.append("без точного deep link")

        return {
            "entity_id": entity.id,
            "title": title,
            "kind": kind,
            "username": username,
            "phone": phone,
            "supports_precise_open": supports_precise_open,
            "subtitle": "  |  ".join(subtitle_parts),
        }

    async def _handle_new_message(self, event: events.NewMessage.Event) -> None:
        if self.monitored_chat_id is None or event.chat_id != self.monitored_chat_id:
            return

        chat = self.dialog_cache.get(self.monitored_chat_id)
        if chat is None:
            return

        sender_name = chat["title"]
        try:
            sender = await event.get_sender()
            if sender is not None:
                sender_name = utils.get_display_name(sender) or sender_name
        except Exception:
            pass

        text = (event.raw_text or "").strip() or "[медиа]"
        if len(text) > 180:
            text = text[:177] + "..."

        self.alert_triggered.emit(
            {
                "chat": chat,
                "sender": sender_name,
                "text": text,
                "message_id": event.id,
                "link": self._build_deep_link(chat, event.id),
            }
        )

    def _build_deep_link(self, chat: dict[str, Any], message_id: int) -> str:
        username = chat.get("username")
        phone = chat.get("phone")
        kind = chat.get("kind")
        entity_id = chat.get("entity_id")

        if username:
            username_quoted = urllib.parse.quote(username, safe="")
            if kind in {"channel", "group"}:
                return f"tg://resolve?domain={username_quoted}&post={message_id}"
            return f"tg://resolve?domain={username_quoted}"

        if phone:
            return f"tg://resolve?phone={urllib.parse.quote(phone, safe='')}"

        if kind in {"channel", "group"} and entity_id:
            return f"tg://privatepost?channel={entity_id}&post={message_id}"

        return "tg://"

    async def _disconnect(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None


class StepBadge(QLabel):
    def __init__(self, title: str) -> None:
        super().__init__(title)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(38)
        self.setProperty("state", "idle")

    def set_state(self, state: str) -> None:
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)


class ChatCard(QFrame):
    def __init__(self, chat: dict[str, Any]) -> None:
        super().__init__()
        self.setObjectName("chatCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        title = QLabel(chat["title"])
        title.setObjectName("chatTitle")
        subtitle = QLabel(chat["subtitle"])
        subtitle.setObjectName("chatSubtitle")
        preview = QLabel(chat.get("last_text") or "Сообщение-подсказка появится здесь после загрузки.")
        preview.setWordWrap(True)
        preview.setObjectName("chatPreview")

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(preview)


class AlertBanner(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.sound = AlertSound()
        self.flash_timer = QTimer(self)
        self.flash_timer.timeout.connect(self._toggle_theme)
        self.flash_timer.setInterval(220)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.dismiss)
        self.hot = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.shell = QFrame()
        self.shell.setObjectName("bannerShell")
        shell_layout = QVBoxLayout(self.shell)
        shell_layout.setContentsMargins(28, 26, 28, 26)
        shell_layout.setSpacing(10)

        self.title_label = QLabel(configured_alert_text())
        self.title_label.setObjectName("bannerTitle")
        self.title_label.setWordWrap(True)
        self.subtitle_label = QLabel("Новый сигнал из Telegram.")
        self.subtitle_label.setObjectName("bannerSubtitle")
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("bannerMeta")
        self.dismiss_button = QPushButton("Снять тревогу")
        self.dismiss_button.setObjectName("ghostButton")
        self.dismiss_button.clicked.connect(self.dismiss)

        shell_layout.addWidget(self.title_label)
        shell_layout.addWidget(self.subtitle_label)
        shell_layout.addWidget(self.meta_label)
        shell_layout.addWidget(self.dismiss_button, 0, Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self.shell)
        self._apply_theme()

    def show_alert(self, chat_title: str, sender: str, text: str) -> None:
        self.title_label.setText(configured_alert_text())
        self.subtitle_label.setText(f"{chat_title}  |  {sender}")
        self.meta_label.setText(text)
        self._position()
        self.hot = True
        self._apply_theme()
        self.show()
        self.raise_()
        self.activateWindow()
        self.sound.play()
        self.flash_timer.start()
        self.hide_timer.start(9000)

    def dismiss(self) -> None:
        self.flash_timer.stop()
        self.hide_timer.stop()
        self.sound.stop()
        self.hide()

    def mousePressEvent(self, _event: Any) -> None:
        self.dismiss()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(event)

    def _toggle_theme(self) -> None:
        self.hot = not self.hot
        self._apply_theme()

    def _apply_theme(self) -> None:
        background = "#ff512f" if self.hot else "#ffb703"
        text = "#190c05" if self.hot else "#2a1207"
        self.shell.setStyleSheet(
            f"""
            QFrame#bannerShell {{
                background: {background};
                border: 2px solid rgba(255,255,255,0.32);
                border-radius: 28px;
            }}
            QLabel#bannerTitle {{
                color: {text};
                font-size: 28px;
                font-weight: 900;
                letter-spacing: 1px;
            }}
            QLabel#bannerSubtitle {{
                color: {text};
                font-size: 20px;
                font-weight: 700;
            }}
            QLabel#bannerMeta {{
                color: {text};
                font-size: 15px;
            }}
            """
        )

    def _position(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(960, 240)
            return
        geometry = screen.availableGeometry()
        width = min(980, geometry.width() - 80)
        height = 250
        x = geometry.x() + (geometry.width() - width) // 2
        y = geometry.y() + 34
        self.setGeometry(x, y, width, height)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config = ConfigStore()
        self.service = TelegramService()
        self.banner = AlertBanner()
        self.chats: list[dict[str, Any]] = []
        self.filtered_chats: list[dict[str, Any]] = []
        self.active_chat: dict[str, Any] | None = None
        self.account_name = ""

        self._build_ui()
        self._connect_signals()
        self._load_saved_values()
        self._go_to_page(0)

    def _build_ui(self) -> None:
        self.setWindowTitle("Attention Switch")
        self.resize(1180, 760)
        self.setMinimumSize(1020, 680)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(28, 28, 28, 28)
        shell.setSpacing(22)

        hero = QFrame()
        hero.setObjectName("heroCard")
        hero.setFixedWidth(320)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(26, 26, 26, 26)
        hero_layout.setSpacing(18)

        hero_badge = QLabel("WINDOWS 10 / 11")
        hero_badge.setObjectName("heroBadge")
        hero_title = QLabel("ATTENTION\nSWITCH")
        hero_title.setObjectName("heroTitle")
        hero_copy = QLabel(
            "Шуточный пульт тревоги: подключаешь Telegram, ставишь один чат на слежку, "
            "и при новом сообщении все сворачивается, Telegram прыгает на чат, а сверху "
            "орет заметный баннер."
        )
        hero_copy.setWordWrap(True)
        hero_copy.setObjectName("heroCopy")

        hero_layout.addWidget(hero_badge, 0, Qt.AlignmentFlag.AlignLeft)
        hero_layout.addWidget(hero_title)
        hero_layout.addWidget(hero_copy)

        self.step_badges = [
            StepBadge("1. Telegram"),
            StepBadge("2. Код"),
            StepBadge("3. Чат"),
            StepBadge("4. Тревога"),
        ]
        for badge in self.step_badges:
            hero_layout.addWidget(badge)

        self.selected_chat_box = QFrame()
        self.selected_chat_box.setObjectName("selectionBox")
        selection_layout = QVBoxLayout(self.selected_chat_box)
        selection_layout.setContentsMargins(18, 16, 18, 16)
        selection_layout.setSpacing(6)
        selection_title = QLabel("Текущий чат")
        selection_title.setObjectName("selectionCaption")
        self.selected_chat_label = QLabel("Пока не выбран")
        self.selected_chat_label.setObjectName("selectionTitle")
        self.selected_chat_hint = QLabel("Сначала войди в Telegram и выбери диалог.")
        self.selected_chat_hint.setObjectName("selectionHint")
        self.selected_chat_hint.setWordWrap(True)
        selection_layout.addWidget(selection_title)
        selection_layout.addWidget(self.selected_chat_label)
        selection_layout.addWidget(self.selected_chat_hint)
        hero_layout.addWidget(self.selected_chat_box)

        self.hero_status = QLabel("Жду подключения.")
        self.hero_status.setWordWrap(True)
        self.hero_status.setObjectName("heroStatus")
        hero_layout.addStretch(1)
        hero_layout.addWidget(self.hero_status)

        content = QFrame()
        content.setObjectName("contentCard")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(28, 28, 28, 28)
        content_layout.setSpacing(18)

        self.page_title = QLabel("Подключение Telegram")
        self.page_title.setObjectName("pageTitle")
        self.page_subtitle = QLabel("Введи данные API и номер телефона.")
        self.page_subtitle.setObjectName("pageSubtitle")
        content_layout.addWidget(self.page_title)
        content_layout.addWidget(self.page_subtitle)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_connect_page())
        self.stack.addWidget(self._build_verify_page())
        self.stack.addWidget(self._build_chat_page())
        self.stack.addWidget(self._build_armed_page())
        content_layout.addWidget(self.stack, 1)

        log_card = QFrame()
        log_card.setObjectName("logCard")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(18, 18, 18, 18)
        log_layout.setSpacing(10)
        log_title = QLabel("Лента статусов")
        log_title.setObjectName("sectionTitle")
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(120)
        self.log_output.setObjectName("logOutput")
        self.log_output.setFixedHeight(170)
        log_layout.addWidget(log_title)
        log_layout.addWidget(self.log_output)
        content_layout.addWidget(log_card)

        shell.addWidget(hero)
        shell.addWidget(content, 1)
        self._apply_styles()

    def _build_connect_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(18)

        helper = QLabel(
            "API ID и API Hash можно взять на <a href='https://my.telegram.org/apps'>my.telegram.org/apps</a>. "
            "Можно положить данные в .env рядом с приложением, поля подтянутся автоматически."
        )
        helper.setObjectName("infoText")
        helper.setOpenExternalLinks(True)
        helper.setWordWrap(True)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)

        self.api_id_input = QLineEdit()
        self.api_id_input.setPlaceholderText("Например, 123456")
        self.api_hash_input = QLineEdit()
        self.api_hash_input.setPlaceholderText("Длинная строка из Telegram API")
        self.api_hash_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.phone_input = QLineEdit()
        self.phone_input.setPlaceholderText("+7 999 123-45-67")

        grid.addWidget(QLabel("API ID"), 0, 0)
        grid.addWidget(self.api_id_input, 1, 0)
        grid.addWidget(QLabel("API Hash"), 0, 1)
        grid.addWidget(self.api_hash_input, 1, 1)
        grid.addWidget(QLabel("Телефон"), 2, 0, 1, 2)
        grid.addWidget(self.phone_input, 3, 0, 1, 2)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.restore_button = QPushButton("Быстрый вход")
        self.restore_button.setObjectName("ghostButton")
        self.connect_button = QPushButton("Подключить Telegram")
        self.connect_button.setObjectName("primaryButton")
        buttons.addWidget(self.restore_button)
        buttons.addStretch(1)
        buttons.addWidget(self.connect_button)

        layout.addWidget(helper)
        layout.addLayout(grid)
        layout.addStretch(1)
        layout.addLayout(buttons)
        return page

    def _build_verify_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(18)

        self.verify_hint = QLabel("Telegram уже отправил код. Введи его сюда.")
        self.verify_hint.setWordWrap(True)
        self.verify_hint.setObjectName("infoText")

        self.code_label = QLabel("Код")
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("Код из Telegram")
        self.password_label = QLabel("Пароль 2FA")
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Пароль двухфакторной защиты")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_label.hide()
        self.password_input.hide()

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.back_to_connect_button = QPushButton("Назад")
        self.back_to_connect_button.setObjectName("ghostButton")
        self.verify_button = QPushButton("Подтвердить")
        self.verify_button.setObjectName("primaryButton")
        buttons.addWidget(self.back_to_connect_button)
        buttons.addStretch(1)
        buttons.addWidget(self.verify_button)

        layout.addWidget(self.verify_hint)
        layout.addWidget(self.code_label)
        layout.addWidget(self.code_input)
        layout.addWidget(self.password_label)
        layout.addWidget(self.password_input)
        layout.addStretch(1)
        layout.addLayout(buttons)
        return page

    def _build_chat_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(16)

        self.account_badge = QLabel("Сессия не активна")
        self.account_badge.setObjectName("accountBadge")
        self.chat_search = QLineEdit()
        self.chat_search.setPlaceholderText("Фильтр по названию, username или типу")
        self.chat_list = QListWidget()
        self.chat_list.setSpacing(10)
        self.chat_list.setObjectName("chatList")

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.refresh_button = QPushButton("Обновить список")
        self.refresh_button.setObjectName("ghostButton")
        self.arm_button = QPushButton("Следить за этим чатом")
        self.arm_button.setObjectName("primaryButton")
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(self.arm_button)

        layout.addWidget(self.account_badge)
        layout.addWidget(self.chat_search)
        layout.addWidget(self.chat_list, 1)
        layout.addLayout(buttons)
        return page

    def _build_armed_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(18)

        self.arm_state = QLabel("Тревога не вооружена.")
        self.arm_state.setObjectName("armedBadge")
        self.arm_chat_title = QLabel("Чат не выбран")
        self.arm_chat_title.setObjectName("pageTitleSmall")
        self.arm_chat_hint = QLabel(
            "При новом входящем сообщении приложение свернет окна, откроет Telegram и покажет тревожный баннер."
        )
        self.arm_chat_hint.setWordWrap(True)
        self.arm_chat_hint.setObjectName("infoText")

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.change_chat_button = QPushButton("Сменить чат")
        self.change_chat_button.setObjectName("ghostButton")
        self.test_alert_button = QPushButton("Тест тревоги")
        self.test_alert_button.setObjectName("primaryButton")
        buttons.addWidget(self.change_chat_button)
        buttons.addStretch(1)
        buttons.addWidget(self.test_alert_button)

        layout.addWidget(self.arm_state)
        layout.addWidget(self.arm_chat_title)
        layout.addWidget(self.arm_chat_hint)
        layout.addStretch(1)
        layout.addLayout(buttons)
        return page

    def _connect_signals(self) -> None:
        self.connect_button.clicked.connect(self._start_login)
        self.restore_button.clicked.connect(self._start_login)
        self.back_to_connect_button.clicked.connect(lambda: self._go_to_page(0))
        self.verify_button.clicked.connect(self._submit_verification)
        self.refresh_button.clicked.connect(self.service.refresh_chats)
        self.arm_button.clicked.connect(self._arm_selected_chat)
        self.change_chat_button.clicked.connect(lambda: self._go_to_page(2))
        self.test_alert_button.clicked.connect(self._run_test_alert)
        self.chat_search.textChanged.connect(self._filter_chat_list)
        self.chat_list.itemDoubleClicked.connect(lambda _item: self._arm_selected_chat())

        self.service.status.connect(self._append_log)
        self.service.code_requested.connect(self._on_code_requested)
        self.service.password_requested.connect(self._on_password_requested)
        self.service.authorized.connect(self._on_authorized)
        self.service.chats_loaded.connect(self._on_chats_loaded)
        self.service.monitoring_changed.connect(self._on_monitoring_changed)
        self.service.alert_triggered.connect(self._on_alert_triggered)
        self.service.error.connect(self._on_error)

    def _load_saved_values(self) -> None:
        self.api_id_input.setText(self.config.data.get("api_id", ""))
        self.api_hash_input.setText(self.config.data.get("api_hash", ""))
        self.phone_input.setText(self.config.data.get("phone", ""))
        self.restore_button.setVisible(self.config.has_saved_session())
        saved_chat_title = self.config.data.get("monitored_chat_title", "")
        if saved_chat_title:
            self.selected_chat_label.setText(saved_chat_title)
            self.selected_chat_hint.setText("Чат сохранен. После входа приложение восстановит слежку.")
            self.arm_chat_title.setText(saved_chat_title)

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QWidget#root {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1a120f, stop:0.5 #26130f, stop:1 #3a190f);
            }
            QFrame#heroCard, QFrame#contentCard, QFrame#logCard {
                background: rgba(19, 12, 10, 0.88);
                border: 1px solid rgba(255, 194, 120, 0.18);
                border-radius: 28px;
            }
            QLabel {
                color: #f8efe9;
            }
            QLabel#heroBadge, QLabel#accountBadge, QLabel#armedBadge {
                background: rgba(255, 183, 3, 0.12);
                border: 1px solid rgba(255, 183, 3, 0.28);
                border-radius: 14px;
                color: #ffd166;
                font-size: 12px;
                font-weight: 700;
                padding: 8px 12px;
            }
            QLabel#heroTitle {
                color: #fff4ec;
                font-size: 34px;
                font-weight: 900;
                line-height: 1.1;
            }
            QLabel#heroCopy, QLabel#heroStatus, QLabel#infoText, QLabel#selectionHint, QLabel#pageSubtitle, QLabel#chatSubtitle, QLabel#chatPreview {
                color: #d9c0b6;
                font-size: 14px;
            }
            QLabel#selectionCaption {
                color: #ffcf99;
                font-size: 12px;
                font-weight: 700;
            }
            QLabel#selectionTitle, QLabel#chatTitle, QLabel#pageTitleSmall {
                color: #fff4ec;
                font-size: 22px;
                font-weight: 800;
            }
            QLabel#pageTitle {
                color: #fff4ec;
                font-size: 28px;
                font-weight: 900;
            }
            QLabel#sectionTitle {
                color: #fff4ec;
                font-size: 16px;
                font-weight: 700;
            }
            QFrame#selectionBox, QFrame#chatCard {
                background: rgba(255, 246, 235, 0.05);
                border: 1px solid rgba(255, 194, 120, 0.16);
                border-radius: 22px;
            }
            StepBadge, QLabel[state="idle"] {
                color: #af9185;
            }
            QLabel[state="idle"], QLabel[state="done"], QLabel[state="active"] {
                border-radius: 16px;
                padding: 8px 12px;
                font-size: 13px;
                font-weight: 700;
            }
            QLabel[state="idle"] {
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.06);
            }
            QLabel[state="done"] {
                background: rgba(255, 183, 3, 0.14);
                border: 1px solid rgba(255, 183, 3, 0.22);
                color: #ffd166;
            }
            QLabel[state="active"] {
                background: rgba(255, 81, 47, 0.2);
                border: 1px solid rgba(255, 81, 47, 0.34);
                color: #fff4ec;
            }
            QLineEdit, QPlainTextEdit, QListWidget {
                background: rgba(255, 247, 240, 0.06);
                border: 1px solid rgba(255, 194, 120, 0.16);
                border-radius: 16px;
                color: #fff7f3;
                padding: 12px 14px;
                font-size: 14px;
                selection-background-color: #ff7f50;
            }
            QLineEdit:focus, QPlainTextEdit:focus, QListWidget:focus {
                border: 1px solid rgba(255, 81, 47, 0.52);
            }
            QListWidget::item {
                border: none;
                margin: 0;
                padding: 0;
            }
            QPushButton {
                min-height: 44px;
                border-radius: 16px;
                font-size: 14px;
                font-weight: 700;
                padding: 0 16px;
            }
            QPushButton#primaryButton {
                background: #ff6b35;
                color: #140a06;
                border: none;
            }
            QPushButton#primaryButton:hover {
                background: #ff824d;
            }
            QPushButton#ghostButton {
                background: rgba(255, 255, 255, 0.04);
                color: #fff4ec;
                border: 1px solid rgba(255, 194, 120, 0.18);
            }
            QPushButton#ghostButton:hover {
                background: rgba(255, 255, 255, 0.08);
            }
            """
        )

    def _go_to_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        titles = {
            0: ("Подключение Telegram", "Введи данные API и номер телефона."),
            1: ("Подтверждение входа", "Код и при необходимости пароль 2FA."),
            2: ("Выбор чата", "Оставь один диалог под наблюдением."),
            3: ("Режим тревоги", "Теперь приложение ждет новое сообщение в выбранном чате."),
        }
        title, subtitle = titles[index]
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)
        for idx, badge in enumerate(self.step_badges):
            if idx < index:
                badge.set_state("done")
            elif idx == index:
                badge.set_state("active")
            else:
                badge.set_state("idle")

    def _append_log(self, text: str) -> None:
        line = f"[{format_now()}] {text}"
        self.log_output.appendPlainText(line)
        self.hero_status.setText(text)

    def _start_login(self) -> None:
        api_id = self.api_id_input.text().strip()
        api_hash = self.api_hash_input.text().strip()
        phone = self.phone_input.text().strip()

        if not api_id.isdigit():
            self._on_error("API ID должен быть числом.")
            return
        if not api_hash:
            self._on_error("API Hash пустой.")
            return
        if not phone:
            self._on_error("Нужен номер телефона.")
            return

        self.config.update(
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
            session_name=session_name_for(api_id, phone),
        )
        self.connect_button.setEnabled(False)
        self.restore_button.setEnabled(False)
        self.service.start_login(api_id, api_hash, phone)

    def _submit_verification(self) -> None:
        if self.password_input.isVisible():
            password = self.password_input.text().strip()
            if not password:
                self._on_error("Нужен пароль 2FA.")
                return
            self.verify_button.setEnabled(False)
            self.service.submit_password(password)
            return

        code = self.code_input.text().strip()
        if not code:
            self._on_error("Код подтверждения пустой.")
            return
        self.verify_button.setEnabled(False)
        self.service.submit_code(code)

    def _on_code_requested(self) -> None:
        self.connect_button.setEnabled(True)
        self.restore_button.setEnabled(True)
        self.verify_button.setEnabled(True)
        self.verify_hint.setText("Telegram уже отправил код. Введи его сюда.")
        self.password_label.hide()
        self.password_input.hide()
        self.password_input.clear()
        self.code_input.clear()
        self.code_input.setFocus()
        self._go_to_page(1)

    def _on_password_requested(self) -> None:
        self.verify_button.setEnabled(True)
        self.password_label.show()
        self.password_input.show()
        self.password_input.setFocus()
        self.verify_hint.setText("Этот аккаунт с 2FA. Оставь код как есть и введи пароль.")
        self._go_to_page(1)

    def _on_authorized(self, account_name: str) -> None:
        self.account_name = account_name
        self.account_badge.setText(f"Вошел как {account_name}")
        self.verify_button.setEnabled(True)
        self.connect_button.setEnabled(True)
        self.restore_button.setEnabled(True)
        self._go_to_page(2)

    def _on_chats_loaded(self, chats: list[dict[str, Any]]) -> None:
        self.chats = chats
        self.filtered_chats = chats
        self._rebuild_chat_list()
        saved_chat_id = self.config.data.get("monitored_chat_id")
        if saved_chat_id is not None:
            for chat in chats:
                if chat["peer_id"] == saved_chat_id:
                    self.active_chat = chat
                    self.service.monitor_chat(saved_chat_id)
                    break

    def _filter_chat_list(self) -> None:
        needle = self.chat_search.text().strip().lower()
        if not needle:
            self.filtered_chats = self.chats
        else:
            self.filtered_chats = [
                chat
                for chat in self.chats
                if needle in chat["title"].lower()
                or needle in chat["subtitle"].lower()
                or needle in chat.get("last_text", "").lower()
            ]
        self._rebuild_chat_list()

    def _rebuild_chat_list(self) -> None:
        self.chat_list.clear()
        for chat in self.filtered_chats:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, chat["peer_id"])
            card = ChatCard(chat)
            item.setSizeHint(card.sizeHint())
            self.chat_list.addItem(item)
            self.chat_list.setItemWidget(item, card)

    def _arm_selected_chat(self) -> None:
        item = self.chat_list.currentItem()
        if item is None:
            self._on_error("Сначала выбери чат из списка.")
            return
        peer_id = item.data(Qt.ItemDataRole.UserRole)
        chat = next((entry for entry in self.filtered_chats if entry["peer_id"] == peer_id), None)
        if chat is None:
            self._on_error("Выбранный чат не найден.")
            return
        self.active_chat = chat
        self.service.monitor_chat(peer_id)
        self.config.update(
            monitored_chat_id=peer_id,
            monitored_chat_title=chat["title"],
        )

    def _on_monitoring_changed(self, chat: dict[str, Any] | None) -> None:
        if not chat:
            return
        self.active_chat = chat
        self.selected_chat_label.setText(chat["title"])
        hint = "Точный deep link доступен." if chat["supports_precise_open"] else "Если deep link ограничен, Telegram просто откроется на главном окне."
        self.selected_chat_hint.setText(hint)
        self.arm_chat_title.setText(chat["title"])
        self.arm_state.setText("Режим тревоги включен")
        self._append_log(f"Слежка включена для чата: {chat['title']}.")
        self._go_to_page(3)

    def _run_test_alert(self) -> None:
        if self.active_chat is None:
            self._on_error("Сначала выбери чат.")
            return
        self._on_alert_triggered(
            {
                "chat": self.active_chat,
                "sender": "Тестовый режим",
                "text": "Проверка баннера и открытия Telegram.",
                "message_id": 1,
                "link": "tg://",
            }
        )

    def _on_alert_triggered(self, payload: dict[str, Any]) -> None:
        chat = payload["chat"]
        self._append_log(f"Сработала тревога: {chat['title']} / {payload['sender']}.")
        WindowsController.minimize_all_windows()

        def open_chat() -> None:
            if not WindowsController.open_telegram_chat(payload["link"]):
                self._append_log("Не удалось открыть tg:// ссылку. Проверь установлен ли Telegram Desktop.")

        QTimer.singleShot(180, open_chat)
        QTimer.singleShot(
            320,
            lambda: self.banner.show_alert(chat["title"], payload["sender"], payload["text"]),
        )

    def _on_error(self, text: str) -> None:
        self.connect_button.setEnabled(True)
        self.restore_button.setEnabled(True)
        self.verify_button.setEnabled(True)
        self._append_log(f"Ошибка: {text}")
        QMessageBox.warning(self, "Ошибка", text)

    def closeEvent(self, event: Any) -> None:
        self.banner.dismiss()
        self.service.shutdown()
        super().closeEvent(event)


def enable_windows_dpi() -> None:
    if platform.system() != "Windows":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main() -> int:
    if platform.system() == "Windows":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    enable_windows_dpi()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("Attention Switch")
    app.setFont(QFont("Trebuchet MS", 10))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
