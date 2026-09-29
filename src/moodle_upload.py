import ctypes
import os
import sys
import traceback

# ─── Инициализация многопоточности Xlib (на случай, если что-то ещё её использует) ───
if sys.platform.startswith("linux"):
    try:
        ctypes.CDLL("libX11.so.6").XInitThreads()
    except Exception as _e:
        print(f"[warn] XInitThreads: {_e}", file=sys.stderr)

os.environ.setdefault("QT_X11_NO_MITSHM", "1")
os.environ.setdefault("XLIB_SKIP_ARGB_VISUALS", "1")

import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import pandas as pd
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import json
import queue

LOG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "moodle_upload.log"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

REQUEST_TIMEOUT = 180


class MoodleClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=3, pool_maxsize=3, max_retries=0
        )
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def get_users_by_usernames(self, usernames: list):
        params = {"field": "username"}
        for i, username in enumerate(usernames):
            params[f"values[{i}]"] = username
        return self._make_request("core_user_get_users_by_field", params)

    def _make_request(self, function_name: str, params: dict):
        url = f"{self.base_url}/webservice/rest/server.php"
        params = dict(params)
        params['wstoken'] = self.token
        params['wsfunction'] = function_name
        params['moodlewsrestformat'] = 'json'

        try:
            response = self.session.post(url, data=params, timeout=REQUEST_TIMEOUT)

            if response.status_code == 403:
                return {"exception": "AccessDenied",
                        "message": "403 Forbidden. Проверьте: токен, права, веб-сервис, WAF."}
            if response.status_code == 401:
                return {"exception": "Unauthorized", "message": "Неверный токен (401)."}
            if response.status_code == 429:
                return {"exception": "RateLimited", "message": "Превышен лимит (429)."}
            if response.status_code >= 500:
                return {"exception": "ServerError",
                        "message": f"Ошибка сервера ({response.status_code})."}
            if response.status_code != 200:
                return {"exception": "HTTPError",
                        "message": f"HTTP {response.status_code}: {response.text[:300]}"}

            try:
                result = response.json()
                if isinstance(result, dict) and "exception" in result:
                    errcode = result.get("errorcode", result.get("exception"))
                    errmsg = result.get("message", "Неизвестная ошибка")
                    debuginfo = result.get("debuginfo", "")

                    details = f"{errcode}: {errmsg}"
                    if debuginfo:
                        details += f" | DEBUG: {debuginfo}"

                    return {
                        "exception": errcode,
                        "message": details,
                    }
                return result
            except ValueError:
                return {
                    "exception": "ParseError",
                    "message": (
                        f"Не-JSON ответ ({response.headers.get('Content-Type')}): "
                        f"{response.text[:300]}"
                    ),
                }

        except requests.exceptions.ConnectionError as e:
            return {"exception": "ConnectionError", "message": f"Ошибка подключения: {e}"}
        except requests.exceptions.Timeout:
            return {"exception": "Timeout", "message": f"Таймаут ({REQUEST_TIMEOUT}с)."}
        except requests.exceptions.RequestException as e:
            return {"exception": "RequestError", "message": f"Ошибка запроса: {e}"}
        except Exception as e:
            return {"exception": "Unknown", "message": f"{type(e).__name__}: {e}"}

    def get_cohorts(self):
        return self._make_request('core_cohort_get_cohorts', {})

    def create_cohort(self, name: str, id_number: str):
        params = {
            'cohorts[0][name]': name,
            'cohorts[0][idnumber]': id_number,
            'cohorts[0][categorytype][type]': 'system',
            'cohorts[0][categorytype][value]': '1',
            'cohorts[0][description]': '',
            'cohorts[0][descriptionformat]': 1,
            'cohorts[0][visible]': 1,
        }
        return self._make_request('core_cohort_create_cohorts', params)
    
    def create_or_update_users(self, users_data: list):
        """Создаёт пользователей через core_user_create_users."""
        params = {}

        for i, user in enumerate(users_data):
            for field in ("username", "password", "firstname", "lastname", "email"):
                value = str(user.get(field, "") or "").strip()
                if not value:
                    return {
                        "exception": "MissingRequiredField",
                        "message": f"У пользователя отсутствует обязательное поле {field}",
                    }
                params[f"users[{i}][{field}]"] = value

            params[f"users[{i}][auth]"] = "manual"

            city = str(user.get("city", "") or "").strip()
            if city:
                params[f"users[{i}][city]"] = city

            # Moodle ожидает двухбуквенный код страны: RU, KZ и т. п.
            country = str(user.get("country", "") or "").strip().upper()
            if len(country) == 2 and country.isalpha():
                params[f"users[{i}][country]"] = country

        return self._make_request("core_user_create_users", params)
    
    def create_or_get_users(self, users_data: list):
        """Находит имеющихся пользователей и создаёт отсутствующих.
        Возвращает список ID и список ошибок создания.
        """
        users_by_username = {}

        for row_number, user in enumerate(users_data, start=2):
            username = str(user.get("username", "")).strip()
            if not username:
                return [], [f"В CSV пустой username, строка {row_number}"]

            # При чтении CSV числовые значения иногда превращаются в строки вида 123.0.
            if username.endswith(".0") and username[:-2].isdigit():
                username = username[:-2]

            users_by_username[username] = {
                key: str(user.get(key, "") or "").strip()
                for key in (
                    "username", "password", "firstname", "lastname",
                    "email", "city", "country",
                )
            }
            users_by_username[username]["username"] = username

        usernames = list(users_by_username)
        found = {}

        # Ищем существующих пользователей небольшими порциями.
        for start in range(0, len(usernames), 100):
            batch = usernames[start:start + 100]
            result = self.get_users_by_usernames(batch)

            if isinstance(result, dict) and "exception" in result:
                return [], [
                    "Поиск пользователей: "
                    f"{result.get('message', result)}. "
                    "Проверьте, что core_user_get_users_by_field добавлена "
                    "во внешний сервис Moodle."
                ]

            if not isinstance(result, list):
                return [], [f"Неожиданный ответ при поиске пользователей: {result!r}"]

            for user in result:
                if isinstance(user, dict) and user.get("username") and user.get("id"):
                    found[user["username"]] = user["id"]

        missing = [
            users_by_username[username]
            for username in usernames
            if username not in found
        ]
        errors = []

        # Сначала пробуем создать пользователей пачкой. Если Moodle отвергнет
        # пачку целиком, пробуем по одному, чтобы выяснить, какие строки ошибочны.
        for start in range(0, len(missing), 25):
            batch = missing[start:start + 25]
            result = self.create_or_update_users(batch)

            if isinstance(result, list):
                for user in result:
                    if isinstance(user, dict) and user.get("username") and user.get("id"):
                        found[user["username"]] = user["id"]
                continue

            if not (isinstance(result, dict) and "exception" in result):
                logger.error(
                    "Не удалось создать пользователя %r: %s",
                    user["username"],
                    message
                )
                errors.append(f"Неожиданный ответ при создании пользователей: {result!r}")
                continue

            for user in batch:
                if not user["password"]:
                    logger.error(
                            "Не удалось создать пользователя %r: %s",
                            user["username"],
                            message
                        )
                    errors.append(
                        f"{user['username']}: нет пароля для создания нового пользователя"
                    )
                    continue

                single_result = self.create_or_update_users([user])

                if isinstance(single_result, list):
                    for created in single_result:
                        if (
                            isinstance(created, dict)
                            and created.get("username")
                            and created.get("id")
                        ):
                            found[created["username"]] = created["id"]
                else:
                    message = (
                        single_result.get("message", str(single_result))
                        if isinstance(single_result, dict)
                        else repr(single_result)
                    )
                    logger.error(
                        "Не удалось создать пользователя %r: %s",
                        user["username"],
                        message
                    )
                    errors.append(f"{user['username']}: {message}")

        # В результате будут ID и ранее существовавших, и созданных пользователей.
        return list(dict.fromkeys(found.values())), errors

    def add_members_to_cohort(self, cohort_id: int, user_ids: list):
        params = {}
        for i, user_id in enumerate(user_ids):
            params[f'members[{i}][cohorttype][type]'] = 'id'
            params[f'members[{i}][cohorttype][value]'] = str(cohort_id)
            params[f'members[{i}][usertype][type]'] = 'id'
            params[f'members[{i}][usertype][value]'] = str(user_id)
        return self._make_request('core_cohort_add_cohort_members', params)


class MoodleAutomationApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Автоматизация Moodle")
        self.root.geometry("1300x920")

        self.is_running = False
        self.stop_requested = False
        self.worker_thread = None

        self.users_data = []
        self.csv_file_path = ""

        self.ui_queue = queue.Queue()
        self.site_names = [f"Сайт {i + 1}" for i in range(4)]
        self.config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "moodle_sites.json"
        )

        self._setup_styles()
        self.setup_ui()

        self.root.after(50, self._drain_ui_queue)

        if os.path.isfile(self.config_path):
            self.load_site_config(self.config_path)

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use('clam')  # 'clam' выглядит приличнее стандартного
        style.configure('Run.TButton', foreground='white', background='#28a745')
        style.configure('Stop.TButton', foreground='white', background='#dc3545')
        style.map('Run.TButton', background=[('active', '#218838')])
        style.map('Stop.TButton', background=[('active', '#c82333')])

    # ─────────────────────────── Конфигурация сайтов ───────────────────────────

    def choose_site_config(self):
        path = filedialog.askopenfilename(
            title="Выберите конфигурацию сайтов",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if path:
            self.load_site_config(path)

    def load_site_config(self, path):
        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)

            sites = data.get("sites")
            if not isinstance(sites, list):
                raise ValueError('В JSON должен быть список "sites".')

            if len(sites) > len(self.site_entries):
                raise ValueError(
                    f"В конфигурации {len(sites)} сайтов, "
                    f"но интерфейс поддерживает не более {len(self.site_entries)}."
                )

            for i, (url_entry, token_entry, status_label) in enumerate(self.site_entries):
                url_entry.delete(0, "end")
                token_entry.delete(0, "end")
                status_label.configure(text="⚪ Ожидание")
                self.site_names[i] = f"Сайт {i + 1}"
                self.site_name_labels[i].configure(text=self.site_names[i])

            for i, site in enumerate(sites):
                if not isinstance(site, dict):
                    raise ValueError(f"Элемент sites[{i}] должен быть объектом.")

                name = str(site.get("name", f"Сайт {i + 1}")).strip()
                url = str(site.get("url", "")).strip().rstrip("/")
                token = str(site.get("token", "")).strip()

                if not url or not token:
                    raise ValueError(
                        f"У сайта «{name}» должны быть заполнены поля url и token."
                    )

                url_entry, token_entry, _ = self.site_entries[i]
                url_entry.insert(0, url)
                token_entry.insert(0, token)

                self.site_names[i] = name or f"Сайт {i + 1}"
                self.site_name_labels[i].configure(text=self.site_names[i])

            self.config_path = path
            self.log_message(
                f"Загружена конфигурация сайтов: {os.path.basename(path)}",
                "SUCCESS"
            )

        except (OSError, json.JSONDecodeError, ValueError) as e:
            self.show_error_popup(f"Не удалось загрузить конфигурацию:\n{e}")

    # ─────────────────────────────── UI ───────────────────────────────

    def setup_ui(self):
        main_frame = ttk.Frame(self.root, padding=15)
        main_frame.pack(fill="both", expand=True)

        ttk.Label(
            main_frame,
            text="Автоматизация загрузки пользователей в Moodle",
            font=("Arial", 20, "bold")
        ).pack(pady=(0, 15))

        # Инфо-блок
        info_frame = ttk.LabelFrame(main_frame, text="Информация", padding=10)
        info_frame.pack(fill="x", pady=(0, 12))
        ttk.Label(
            info_frame,
            text=("⚠️ Все 4 сайта обрабатываются параллельно. "
                  "Когорта будет создана, если её нет, или использована существующая (по idnumber). "
                  "Убедитесь, что в веб-сервис добавлены функции: core_cohort_create_cohorts, "
                  "core_cohort_get_cohorts, core_user_create_users, core_cohort_add_cohort_members."),
            justify="left", wraplength=1250
        ).pack()

        # Сайты
        sites_frame = ttk.LabelFrame(main_frame, text="Конфигурация сайтов Moodle", padding=10)
        sites_frame.pack(fill="x", pady=(0, 12))

        ttk.Button(
            sites_frame,
            text="📂 Загрузить конфигурацию сайтов",
            command=self.choose_site_config,
            width=30
        ).pack(anchor="w", pady=(0, 6))

        self.site_name_labels = []
        self.site_entries = []
        for i in range(4):
            site_frame = ttk.Frame(sites_frame, relief="groove", borderwidth=1)
            site_frame.pack(fill="x", pady=2)

            name_label = ttk.Label(
                site_frame,
                text=self.site_names[i],
                width=15,
                font=("Arial", 11, "bold"),
                anchor="w"
            )
            name_label.pack(side="left", padx=(5, 10), pady=4)
            self.site_name_labels.append(name_label)

            url_entry = ttk.Entry(site_frame, width=50)
            url_entry.pack(side="left", padx=(0, 5), pady=4)

            token_entry = ttk.Entry(site_frame, width=35, show="•")
            token_entry.pack(side="left", padx=(0, 5), pady=4)

            status_label = ttk.Label(site_frame, text="⚪ Ожидание", width=20)
            status_label.pack(side="left", padx=(5, 5), pady=4)

            self.site_entries.append((url_entry, token_entry, status_label))

        # Параметры
        params_frame = ttk.LabelFrame(main_frame, text="Параметры", padding=10)
        params_frame.pack(fill="x", pady=(0, 12))

        ttk.Label(
            params_frame,
            text="Название глобальной группы (когорты):",
            font=("Arial", 12, "bold")
        ).pack(anchor="w", pady=(0, 4))
        self.cohort_name_var = tk.StringVar()
        ttk.Entry(params_frame, textvariable=self.cohort_name_var, width=80).pack(
            fill="x", pady=(0, 8))

        # Файл
        file_frame = ttk.LabelFrame(main_frame, text="CSV файл", padding=10)
        file_frame.pack(fill="x", pady=(0, 12))

        self.file_path_var = tk.StringVar(value="CSV файл не выбран")
        ttk.Label(file_frame, textvariable=self.file_path_var).pack(anchor="w", pady=(0, 4))
        ttk.Button(file_frame, text="📂 Выбрать CSV файл", command=self.load_csv,
                   width=30).pack(anchor="w")

        # Preview
        preview_frame = ttk.LabelFrame(main_frame, text="Предварительный просмотр (первые 20 строк)", padding=10)
        preview_frame.pack(fill="both", expand=False, pady=(0, 12))

        self.preview_textbox = tk.Text(preview_frame, wrap="none", height=10,
                                       state="disabled", font=("Courier", 10))
        preview_scroll = ttk.Scrollbar(preview_frame, orient="vertical",
                                       command=self.preview_textbox.yview)
        self.preview_textbox.configure(yscrollcommand=preview_scroll.set)
        preview_scroll.pack(side="right", fill="y")
        self.preview_textbox.pack(fill="both", expand=True)

        # Кнопки
        buttons_frame = ttk.Frame(main_frame)
        buttons_frame.pack(fill="x", pady=(0, 12))

        self.run_button = ttk.Button(
            buttons_frame,
            text="▶ Запустить процесс",
            command=self.start_process,
            style="Run.TButton",
            width=25
        )
        self.run_button.pack(side="left", padx=5, pady=8)

        self.stop_button = ttk.Button(
            buttons_frame,
            text="⏹ Остановить",
            command=self.stop_process,
            style="Stop.TButton",
            width=20,
            state="disabled"
        )
        self.stop_button.pack(side="left", padx=5, pady=8)

        # Лог
        log_frame = ttk.LabelFrame(main_frame, text="Лог выполнения", padding=10)
        log_frame.pack(fill="both", expand=True)

        self.log_textbox = tk.Text(log_frame, height=12, state="disabled",
                                   font=("Courier", 10))
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical",
                                   command=self.log_textbox.yview)
        self.log_textbox.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_textbox.pack(fill="both", expand=True)

    # ─────────────────── Thread-safe UI helpers ───────────────────

    def _ui_log(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefixes = {
            "SUCCESS": "✅ ", "WARNING": "⚠️ ", "ERROR": "❌ ",
            "STEP": "📋 ", "INFO": "ℹ️ ",
        }
        prefix = prefixes.get(level, "")
        try:
            self.log_textbox.configure(state="normal")
            self.log_textbox.insert("end", f"[{timestamp}] {prefix}{message}\n")
            self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")
        except Exception as e:
            logger.error(f"Ошибка UI-лога: {e}")

    def _ui_update_status(self, site_index: int, status: str):
        try:
            if 0 <= site_index < len(self.site_entries):
                self.site_entries[site_index][2].configure(text=status)
        except Exception:
            pass

    def _drain_ui_queue(self):
        try:
            while True:
                try:
                    item = self.ui_queue.get_nowait()
                except queue.Empty:
                    break

                action = item[0]

                if action == "log":
                    _, message, level = item
                    self._ui_log(message, level)
                elif action == "status":
                    _, site_index, status = item
                    self._ui_update_status(site_index, status)
                elif action == "finish":
                    self._finish_process()
        except Exception:
            logger.exception("Ошибка обработки очереди интерфейса")

        try:
            if self.root.winfo_exists():
                self.root.after(50, self._drain_ui_queue)
        except Exception:
            pass

    def log_message(self, message, level="INFO"):
        self.ui_queue.put(("log", message, level))

    def update_site_status(self, site_index, status):
        self.ui_queue.put(("status", site_index, status))

    # ─────────────────────────────── CSV ───────────────────────────────

    def load_csv(self):
        path = filedialog.askopenfilename(
            filetypes=[("CSV files", "*.csv"), ("All", "*.*")]
        )
        if not path:
            return

        self.csv_file_path = path
        self.file_path_var.set(f"📄 Выбранный файл: {os.path.basename(path)}")

        try:
            try:
                df = pd.read_csv(
                    path,
                    sep=";",
                    encoding="utf-8",
                    dtype=str,
                    keep_default_na=False
                )
                if len(df.columns) <= 1:
                    raise ValueError("Только одна колонка, попробуем другой разделитель")
            except Exception:
                df = pd.read_csv(path, encoding='utf-8')

            required = ['username', 'password', 'firstname', 'lastname',
                        'email', 'city', 'country']
            missing = [c for c in required if c not in df.columns]
            if missing:
                self.show_error_popup(
                    f"В CSV отсутствуют колонки: {', '.join(missing)}"
                )
                return

            df = df.fillna('')
            self.users_data = df.to_dict('records')

            preview = df.head(20).to_string(index=False)
            self.preview_textbox.configure(state="normal")
            self.preview_textbox.delete("1.0", "end")
            self.preview_textbox.insert("end", preview)
            self.preview_textbox.configure(state="disabled")

            self.log_message(
                f"Загружено {len(self.users_data)} записей из {os.path.basename(path)}",
                "SUCCESS"
            )
        except Exception as e:
            logger.error(f"Ошибка чтения CSV: {e}")
            self.show_error_popup(f"Ошибка чтения файла:\n{e}")

    def show_error_popup(self, message):
        messagebox.showerror("Ошибка", message)

    # ─────────────────────────── Moodle: обработка ───────────────────────────

    def _find_or_create_cohort(self, client: MoodleClient, site_num: int, cohort_name: str):
        existing = client.get_cohorts()

        if isinstance(existing, dict) and "exception" in existing:
            return None, (
                "Получение списка когорт: "
                f"{existing.get('message', existing)}. "
                "Проверьте core_cohort_get_cohorts в настройках внешнего сервиса."
            )

        if not isinstance(existing, list):
            return None, f"Неожиданный ответ Moodle при получении когорт: {existing!r}"

        cohort_id = None

        for cohort in existing:
            if isinstance(cohort, dict) and cohort.get("idnumber") == cohort_name:
                cohort_id = cohort.get("id")
                self.log_message(
                    f"[Сайт {site_num}] Найдена когорта ID={cohort_id}",
                    "SUCCESS"
                )
                break
            
        if cohort_id is None:
            self.log_message(
                f"[Сайт {site_num}] Создание новой когорты '{cohort_name}'...",
                "INFO"
            )
            result = client.create_cohort(cohort_name, cohort_name)

            if isinstance(result, dict) and 'exception' in result:
                return None, f"Создание когорты: {result['message']}"

            if isinstance(result, list) and len(result) > 0 and 'id' in result[0]:
                cohort_id = result[0]['id']
                self.log_message(
                    f"[Сайт {site_num}] ✓ Когорта создана, ID={cohort_id}",
                    "SUCCESS"
                )
            else:
                return None, f"Не получен ID когорты. Ответ: {result}"
        else:
            self.update_site_status(site_num - 1, "⏳ Когорта найдена")

        return cohort_id, None

    def process_single_site(self, site_index: int, base_url: str, token: str,
                            cohort_name: str, users_data: list):
        site_num = site_index + 1

        if not base_url or not token:
            return {"site": site_num, "success": False, "message": "Пустая конфигурация"}
        if not cohort_name:
            return {"site": site_num, "success": False, "message": "Не задано имя когорты"}
        if not users_data:
            return {"site": site_num, "success": False, "message": "Нет пользователей"}

        self.update_site_status(site_index, "⏳ Подключение")
        self.log_message(f"[Сайт {site_num}] Начало работы: {base_url}", "STEP")

        client = MoodleClient(base_url, token)
        try:
            self.update_site_status(site_index, "⏳ Поиск когорты")
            cohort_id, error = self._find_or_create_cohort(client, site_num, cohort_name)

            if error:
                self.update_site_status(site_index, "❌ Когорта")
                self.log_message(f"[Сайт {site_num}] Ошибка: {error}", "ERROR")
                return {"site": site_num, "success": False, "message": error}

            if self.stop_requested:
                return {"site": site_num, "success": False, "message": "Остановлено"}

            # 2. Находим пользователей и создаём отсутствующих.
            self.update_site_status(site_index, f"⏳ {len(users_data)} польз.")
            self.log_message(
                f"[Сайт {site_num}] Поиск и загрузка {len(users_data)} пользователей..."
            )

            user_ids, user_errors = client.create_or_get_users(users_data)

            for error in user_errors:
                self.log_message(f"[Сайт {site_num}] Ошибка пользователя: {error}", "ERROR")

            if not user_ids:
                message = "; ".join(user_errors) or "Moodle не вернул ID пользователей"
                self.update_site_status(site_index, "❌ Пользователи")
                return {
                    "site": site_num,
                    "success": False,
                    "message": message,
                }

            self.log_message(
                f"[Сайт {site_num}] Найдено/создано пользователей: {len(user_ids)}",
                "SUCCESS"
            )

            if self.stop_requested:
                return {"site": site_num, "success": False, "message": "Остановлено"}

            # 3. Добавляем ID пользователей в когорту.
            self.update_site_status(site_index, "⏳ В когорту")
            self.log_message(
                f"[Сайт {site_num}] Добавление {len(user_ids)} пользователей в когорту..."
            )

            result = client.add_members_to_cohort(cohort_id, user_ids)

            if isinstance(result, dict) and "exception" in result:
                self.update_site_status(site_index, "❌ Добавление")
                self.log_message(
                    f"[Сайт {site_num}] Ошибка добавления в когорту: "
                    f"{result.get('message', result)}",
                    "ERROR"
                )
                return {
                    "site": site_num,
                    "success": False,
                    "message": f"Добавление в когорту: {result.get('message', result)}",
                }

            if isinstance(result, dict) and result.get("warnings"):
                for warning in result["warnings"]:
                    if isinstance(warning, dict):
                        self.log_message(
                            f"[Сайт {site_num}] Предупреждение Moodle: "
                            f"{warning.get('message', warning)}",
                            "WARNING"
                        )

            self.log_message(
                f"[Сайт {site_num}] Пользователи добавлены в когорту",
                "SUCCESS"
            )

            self.update_site_status(site_index, f"✅ {len(user_ids)} польз.")

            return {
                "site": site_num,
                "success": not user_errors,
                "message": (
                    f"В когорту добавлено: {len(user_ids)}"
                    + (f"; ошибок пользователей: {len(user_errors)}" if user_errors else "")
                ),
            }

        except Exception as e:
            tb = traceback.format_exc()
            logger.exception("Критическая ошибка на сайте %s", site_num)
            self.update_site_status(site_index, "❌ Исключение")
            self.log_message(
                f"[Сайт {site_num}] Исключение: {type(e).__name__}: {e}\n{tb}",
                "ERROR"
            )
            return {
                "site": site_num,
                "success": False,
                "message": f"{type(e).__name__}: {e}",
            }
        finally:
            client.close()

    def worker_main(self, configs, cohort_name, users_data):
        try:
            with ThreadPoolExecutor(max_workers=4) as executor:
                future_to_site = {}
                for i, (url, token) in enumerate(configs):
                    if not url or not token:
                        self.update_site_status(i, "⊘ Пропуск")
                        continue
                    fut = executor.submit(
                        self.process_single_site, i, url, token, cohort_name, users_data
                    )
                    future_to_site[fut] = i

                results = []
                for fut in as_completed(future_to_site):
                    if self.stop_requested:
                        break
                    try:
                        r = fut.result()
                        results.append(r)
                    except Exception as e:
                        site_idx = future_to_site[fut]
                        results.append({
                            "site": site_idx + 1,
                            "success": False,
                            "message": f"Исключение потока: {e}",
                        })

            self.log_message("", "INFO")
            self.log_message("=" * 60, "STEP")
            self.log_message("📊 ИТОГОВЫЙ ОТЧЕТ", "STEP")
            self.log_message("=" * 60, "STEP")

            ok, err = 0, 0
            for r in results:
                if r.get("success"):
                    self.log_message(
                        f"✓ Сайт {r['site']}: УСПЕХ — {r['message']}", "SUCCESS"
                    )
                    ok += 1
                else:
                    self.log_message(
                        f"✗ Сайт {r['site']}: ОШИБКА — {r['message']}", "ERROR"
                    )
                    err += 1

            self.log_message(f"ИТОГО: Успешно: {ok} | Ошибок: {err}", "INFO")
            self.log_message("=" * 60, "STEP")

        except Exception as e:
            logger.exception("Критическая ошибка в рабочем потоке")
            self.log_message(f"Критическая ошибка: {e}", "ERROR")
        finally:
            self.ui_queue.put(("finish",))

    def _finish_process(self):
        self.is_running = False
        try:
            self.run_button.configure(state="normal", text="▶ Запустить процесс")
            self.stop_button.configure(state="disabled")
        except Exception:
            pass

    def start_process(self):
        if self.is_running:
            return
        if not self.csv_file_path or not self.users_data:
            self.show_error_popup("Выберите CSV-файл с пользователями.")
            return

        cohort_name = self.cohort_name_var.get().strip()
        if not cohort_name:
            self.show_error_popup("Введите название глобальной группы.")
            return

        configs = [(u.get().strip(), t.get().strip()) for (u, t, _) in self.site_entries]
        if not any(u and t for u, t in configs):
            self.show_error_popup("Заполните хотя бы одну конфигурацию сайта.")
            return

        self.is_running = True
        self.stop_requested = False
        self.run_button.configure(state="disabled", text="⏳ Выполняется...")
        self.stop_button.configure(state="normal")

        for i, (u, t, _) in enumerate(self.site_entries):
            if not u.get().strip() or not t.get().strip():
                self.update_site_status(i, "⊘ Пропуск")
            else:
                self.update_site_status(i, "⏳ В очереди")

        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")

        self.log_message("🚀 Начало процесса автоматизации", "STEP")
        self.log_message(f"Пользователей в CSV: {len(self.users_data)}", "INFO")
        self.log_message(f"Название когорты: {cohort_name}", "INFO")
        self.log_message("Все 4 сайта обрабатываются параллельно", "INFO")

        self.worker_thread = threading.Thread(
            target=self.worker_main,
            args=(configs, cohort_name, self.users_data),
            daemon=True,
        )
        self.worker_thread.start()

    def stop_process(self):
        if self.is_running:
            self.stop_requested = True
            self.log_message("⏹ Запрошена остановка...", "WARNING")

    def on_close(self):
        self.stop_requested = True
        self.is_running = False
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    try:
        root = tk.Tk()
        root.update_idletasks()
        app = MoodleAutomationApp(root)
        root.protocol("WM_DELETE_WINDOW", app.on_close)
        root.mainloop()
    except Exception as e:
        logger.exception("Критическая ошибка запуска приложения")
        print(f"Ошибка запуска: {e}")


if __name__ == "__main__":
    main()
