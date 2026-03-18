import json
import os
import sys
import time
import random
import string
import signal
import threading
import traceback
import psutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests
import socks  # PySocks — needed so requests can use socks5:// URLs
from colorama import init, Fore, Style

init(autoreset=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH  = os.path.join(SCRIPT_DIR, "config.json")
PROXY_PATH   = os.path.join(SCRIPT_DIR, "proxy.txt")
RESULTS_PATH = os.path.join(SCRIPT_DIR, "list.txt")

DISCORD_API_URL = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"

BANNER = rf"""
{Fore.CYAN}{Style.BRIGHT}
  ╔══════════════════════════════════════════════════════════════╗
  ║          {Fore.WHITE}Discord Username Availability Checker{Fore.CYAN}            ║
  ║          {Fore.LIGHTBLACK_EX}Rotating Proxies  ·  Multi-threaded{Fore.CYAN}              ║
  ╚══════════════════════════════════════════════════════════════╝
{Style.RESET_ALL}"""

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")

def log_info(msg: str)    -> None: print(f"  {Fore.LIGHTBLACK_EX}[{timestamp()}] {Fore.CYAN}[INFO]    {Fore.WHITE}{msg}")
def log_success(msg: str) -> None: print(f"  {Fore.LIGHTBLACK_EX}[{timestamp()}] {Fore.GREEN}[SUCCESS] {Fore.WHITE}{msg}")
def log_warn(msg: str)    -> None: print(f"  {Fore.LIGHTBLACK_EX}[{timestamp()}] {Fore.YELLOW}[WARN]    {Fore.WHITE}{msg}")
def log_error(msg: str)   -> None: print(f"  {Fore.LIGHTBLACK_EX}[{timestamp()}] {Fore.RED}[ERROR]   {Fore.WHITE}{msg}")
def log_rate(msg: str)    -> None: print(f"  {Fore.LIGHTBLACK_EX}[{timestamp()}] {Fore.MAGENTA}[RATE]    {Fore.WHITE}{msg}")
def log_debug(msg: str)   -> None: print(f"  {Fore.LIGHTBLACK_EX}[{timestamp()}] {Fore.LIGHTBLACK_EX}[DEBUG]   {msg}{Style.RESET_ALL}")
def log_proxy(msg: str)   -> None: print(f"  {Fore.LIGHTBLACK_EX}[{timestamp()}] {Fore.BLUE}[PROXY]   {Fore.WHITE}{msg}")

# ---------------------------------------------------------------------------
# System Monitor
# ---------------------------------------------------------------------------

class SystemMonitor:
    def __init__(self, interval: int = 30):
        self._interval = interval
        self._stop     = threading.Event()
        self._thread   = threading.Thread(target=self._run, daemon=True, name="SystemMonitor")
        self._process  = psutil.Process(os.getpid())

    def start(self) -> None: self._thread.start()
    def stop(self)  -> None: self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                cpu    = self._process.cpu_percent(interval=1)
                mem_mb = self._process.memory_info().rss / 1024 / 1024
                conns  = len(self._process.connections())
                log_debug(f"CPU={cpu:.1f}%  MEM={mem_mb:.1f}MB  THREADS={threading.active_count()}  CONNS={conns}")
            except Exception as exc:
                log_debug(f"Monitor error: {exc}")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Config:
    def __init__(self, path: str = CONFIG_PATH):
        if not os.path.isfile(path):
            log_error(f"Configuration file not found: {path}")
            sys.exit(1)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.webhook_url: str        = data.get("webhook_url", "")
        self.concurrency: int        = data.get("concurrency", 10)
        self.timeout: int            = data.get("timeout", 8)
        self.max_retries: int        = data.get("max_retries", 1)
        self.retry_delay_base: float = data.get("retry_delay_base", 1.0)
        self.proxy_cooldown: int     = data.get("proxy_cooldown_seconds", 30)
        self.rate_limit_margin: float = data.get("rate_limit_safety_margin", 0.5)
        if not self.webhook_url or "YOUR_WEBHOOK" in self.webhook_url:
            log_warn("Webhook URL not configured — available usernames will NOT be sent.")


# ---------------------------------------------------------------------------
# Proxy Manager  — 
# ---------------------------------------------------------------------------

class ProxyManager:
    """
    Gerencia proxies com rastreamento de status em tempo real.
    Durante as verificações normais, registra quais proxies funcionam e quais falham.
    Mantém uma "proxy ativa" preferida — quando ela para de funcionar, busca outra
    automaticamente e loga a transição para o usuário.
    """

    SOCKS_PORTS = {1080, 1081, 1085, 1086, 4145, 4153, 5678, 9050, 9051, 9150}
    _COOLDOWN_WARN_INTERVAL = 10  

    def __init__(self, path: str, cooldown_seconds: int = 60,
                 validate: bool = True, validate_threads: int = 20,
                 validate_timeout: int = 8):
        self._lock           = threading.Lock()
        self._proxies: list[dict] = []
        self._index          = 0
        self._cooldown       = cooldown_seconds
        self._cooldown_until: dict[str, datetime] = {}
        self._fail_count:     dict[str, int]      = {}
        self._success_count:  dict[str, int]      = {}
        self._permanent_ban:  set[str]            = set()
        self._last_cooldown_warn: float           = 0.0


        self._active_proxy: str | None = None

        self._load_file(path)
        if validate and self._proxies:
            self._pre_validate(validate_threads, validate_timeout, len(self._proxies))

    # ---- parsing ----

    @classmethod
    def _guess_scheme(cls, host_port: str) -> str:
        try:
            port = int(host_port.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return "http"
        return "socks5h" if port in cls.SOCKS_PORTS else "http"

    @classmethod
    def _parse_line(cls, line: str) -> dict | None:
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        explicit_scheme = None
        clean = line
        for prefix in ("socks5h://", "socks5://", "socks4://", "http://", "https://"):
            if clean.lower().startswith(prefix):
                explicit_scheme = prefix.rstrip(":/")
                clean = clean[len(prefix):]
                break
        try:
            parts = clean.split(":")
            if len(parts) == 4 and "@" not in clean:
                host, port, user, password = parts
                host_port   = f"{host}:{port}"
                credentials = f"{user}:{password}"
            elif "@" in clean:
                at_idx      = clean.index("@")
                credentials = clean[:at_idx]
                host_port   = clean[at_idx + 1:]
            else:
                credentials = None
                host_port   = clean
            if ":" not in host_port:
                return None
            scheme    = explicit_scheme or cls._guess_scheme(host_port)
            proxy_url = f"{scheme}://{credentials}@{host_port}" if credentials else f"{scheme}://{host_port}"
            return {"http": proxy_url, "https": proxy_url, "_key": host_port}
        except (ValueError, IndexError):
            return None

    def _add_proxy(self, proxy: dict) -> None:
        for existing in self._proxies:
            if existing["_key"] == proxy["_key"]:
                return
        self._proxies.append(proxy)

    def _load_file(self, path: str) -> None:
        if not os.path.isfile(path):
            log_warn(f"Proxy file not found: {path} — running without proxies.")
            return
        count = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parsed = self._parse_line(line)
                if parsed:
                    self._add_proxy(parsed)
                    count += 1
        if count:
            log_info(f"Loaded {Fore.GREEN}{count}{Fore.WHITE} proxies from file")
        else:
            log_warn("No valid proxies found in proxy.txt.")

    # ---- pre-validation ----

    def _pre_validate(self, threads: int, timeout: int, sample_size: int) -> None:
        total   = len(self._proxies)
        to_test = self._proxies[:] if total <= sample_size else random.sample(self._proxies, sample_size)
        log_info(f"Pre-validating {Fore.YELLOW}{len(to_test)}{Fore.WHITE} / {total} proxies …")
        alive: list[dict] = []
        alive_lock = threading.Lock()

        def _test(proxy: dict) -> None:
            try:
                r = requests.get("http://httpbin.org/ip",
                                 proxies={"http": proxy["http"], "https": proxy["https"]},
                                 timeout=timeout)
                if r.status_code == 200:
                    with alive_lock:
                        alive.append(proxy)
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=threads) as pool:
            list(pool.map(_test, to_test))

        if total > sample_size:
            tested_keys   = {p["_key"] for p in to_test}
            untested      = [p for p in self._proxies if p["_key"] not in tested_keys]
            self._proxies = alive + untested
        else:
            self._proxies = alive

        random.shuffle(self._proxies)
        log_info(f"Pre-validation done: {Fore.GREEN}{len(alive)}{Fore.WHITE} alive — "
                 f"{Fore.GREEN}{len(self._proxies)}{Fore.WHITE} total in pool")

    # ---- public API ----

    @property
    def has_proxies(self) -> bool:
        return len(self._proxies) > 0

    @property
    def pool_size(self) -> int:
        return len(self._proxies)

    def get_next(self) -> dict | None:
        """
        Retorna a próxima proxy disponível.
        Prioriza a proxy ativa atual se ela ainda estiver disponível.
        Retorna None quando todas estão em cooldown.
        """
        if not self._proxies:
            return None

        with self._lock:
            now   = datetime.now()
            total = len(self._proxies)

            # Tenta usar a proxy ativa preferida primeiro
            if self._active_proxy:
                key = self._active_proxy
                if (key not in self._permanent_ban
                        and (key not in self._cooldown_until or self._cooldown_until[key] <= now)):
                    for p in self._proxies:
                        if p["_key"] == key:
                            return {"http": p["http"], "https": p["https"], "_key": key}

            # Busca a próxima disponível (round-robin)
            max_scan = min(total, 50)
            for _ in range(max_scan):
                proxy = self._proxies[self._index % total]
                self._index = (self._index + 1) % total
                key = proxy["_key"]
                if key in self._permanent_ban:
                    continue
                if key in self._cooldown_until and self._cooldown_until[key] > now:
                    continue
                return {"http": proxy["http"], "https": proxy["https"], "_key": key}

            # Todas em cooldown
            now_ts = time.monotonic()
            if now_ts - self._last_cooldown_warn >= self._COOLDOWN_WARN_INTERVAL:
                self._last_cooldown_warn = now_ts
                on_cd = sum(1 for k, u in self._cooldown_until.items()
                            if u > now and k not in self._permanent_ban)
                log_warn(f"Todas as proxies em cooldown ({on_cd}/{total}) — aguardando …")
            return None

    def mark_success(self, proxy: dict | None) -> None:
        """Registra sucesso: remove cooldown, incrementa contador, promove a proxy ativa."""
        if proxy is None:
            return
        key = proxy.get("_key")
        if not key:
            return
        with self._lock:
            self._cooldown_until.pop(key, None)
            self._fail_count.pop(key, None)
            self._success_count[key] = self._success_count.get(key, 0) + 1

            # Promove como proxy ativa se ainda não tiver uma, ou se esta tiver mais sucessos
            if self._active_proxy is None:
                self._active_proxy = key
                log_proxy(f"{Fore.GREEN}OK{Fore.WHITE} {key} — usando como proxy ativa")
            elif self._active_proxy != key:
                # Só loga "OK" sem promover — a ativa atual ainda está funcionando
                pass

    def mark_failed(self, proxy: dict | None) -> None:
        """Registra falha: coloca em cooldown e, se era a proxy ativa, busca outra."""
        if proxy is None:
            return
        key = proxy.get("_key")
        if not key:
            return
        with self._lock:
            self._fail_count[key] = self._fail_count.get(key, 0) + 1
            self._cooldown_until[key] = datetime.now() + timedelta(seconds=5)

            was_active = (self._active_proxy == key)
            if was_active:
                self._active_proxy = None
                log_proxy(f"{Fore.RED}FALHOU{Fore.WHITE} {key} — procurando outra proxy …")
            else:
                log_proxy(f"{Fore.RED}FALHOU{Fore.WHITE} {key}")

    def soonest_available(self) -> float:
        """Segundos até a próxima proxy sair do cooldown (0 se já houver uma livre)."""
        with self._lock:
            now     = datetime.now()
            soonest = None
            for p in self._proxies:
                key = p["_key"]
                if key in self._permanent_ban:
                    continue
                until = self._cooldown_until.get(key)
                if until is None or until <= now:
                    return 0.0
                delta = (until - now).total_seconds()
                if soonest is None or delta < soonest:
                    soonest = delta
            return soonest or 0.0

    def notify_new_active(self, key: str) -> None:
        """Chamado pelo checker quando uma nova proxy diferente começa a funcionar."""
        with self._lock:
            if self._active_proxy != key:
                self._active_proxy = key
                log_proxy(f"{Fore.GREEN}ATIVA{Fore.WHITE} {key} — proxy funcionando")


# ---------------------------------------------------------------------------
# Username Generator
# ---------------------------------------------------------------------------

class UsernameGenerator:
    """Gera usernames aleatórios com suporte a sequências obrigatórias."""

    LETTERS = string.ascii_lowercase
    DIGITS  = string.digits
    DOT     = "."
    UNDER   = "_"

    @classmethod
    def generate(cls, count: int, length: int, mode: str,
                 required_sequence: str = "") -> list[str]:
        """
        Gera usernames aleatórios.
        mode: 'letters' | 'numbers' | 'alphanumeric'
        required_sequence:
          - string normal  → todos os usernames contêm essa sequência
          - '__repeat__'   → cada username recebe um par repetido aleatório
                             (ex: aa, bb, 00, 11) em posição aleatória
          - ''             → totalmente aleatório
        """
        if mode == "letters":
            charset = cls.LETTERS
        elif mode == "numbers":
            charset = cls.DIGITS
        elif mode == "alphanumeric":
            charset = cls.LETTERS + cls.DIGITS
        elif mode == "alphanumeric_dot":
            charset = cls.LETTERS + cls.DIGITS + cls.DOT
        elif mode == "alphanumeric_dot_under":
            charset = cls.LETTERS + cls.DIGITS + cls.DOT + cls.UNDER
        else:
            charset = cls.LETTERS + cls.DIGITS

        random_repeat = (required_sequence == "__repeat__")
        fixed_seq     = "" if random_repeat else required_sequence.lower()

        if fixed_seq and len(fixed_seq) >= length:
            log_warn(f"Sequência '{fixed_seq}' é grande demais para usernames de {length} chars — ignorando.")
            fixed_seq = ""

        repeat_pool: list[str] = []
        if random_repeat:
            if mode in ("letters", "alphanumeric", "alphanumeric_dot", "alphanumeric_dot_under"):
                repeat_pool += [c * 2 for c in cls.LETTERS]
            if mode in ("numbers", "alphanumeric", "alphanumeric_dot", "alphanumeric_dot_under"):
                repeat_pool += [c * 2 for c in cls.DIGITS]

        seen: set[str] = set()
        results: list[str] = []
        max_attempts = count * 20
        attempts     = 0

        while len(results) < count and attempts < max_attempts:
            attempts += 1

            if random_repeat:
                seq = random.choice(repeat_pool)
            else:
                seq = fixed_seq

            if seq:
                max_pos   = length - len(seq)
                pos       = random.randint(0, max_pos)
                left_len  = pos
                right_len = length - len(seq) - left_len
                name = (
                    "".join(random.choices(charset, k=left_len))
                    + seq
                    + "".join(random.choices(charset, k=right_len))
                )
            else:
                name = "".join(random.choices(charset, k=length))

            if name not in seen:
                seen.add(name)
                results.append(name)

        if len(results) < count:
            log_warn(f"Só foi possível gerar {len(results)} usernames únicos com os parâmetros escolhidos.")

        return results


# ---------------------------------------------------------------------------
# Username Checker
# ---------------------------------------------------------------------------

class UsernameChecker:
    AVAILABLE    = "available"
    TAKEN        = "taken"
    RATE_LIMITED = "rate_limited"
    ERROR        = "error"

    def __init__(self, config: Config, proxy_manager: ProxyManager):
        self.config        = config
        self.proxy_manager = proxy_manager
        self._rate_lock       = threading.Lock()
        self._rate_wait_until = datetime.min
        self._last_working_proxy: str | None = None
        self._last_proxy_lock = threading.Lock()

    def check(self, username: str) -> tuple[str, str]:
        """Verifica um username. Loga status da proxy a cada resultado."""
        for attempt in range(1, self.config.max_retries + 1):
            proxy = self.proxy_manager.get_next()

            # Sem proxy disponível — espera um pouco e tenta de novo
            if proxy is None:
                wait = self.proxy_manager.soonest_available()
                if wait > 0:
                    time.sleep(min(wait, 2.0))
                else:
                    time.sleep(0.5)
                continue

            key        = proxy.get("_key", "direct")
            proxy_dict = {k: v for k, v in proxy.items() if k != "_key"}

            try:
                resp = requests.post(
                    DISCORD_API_URL,
                    json={"username": username},
                    proxies=proxy_dict,
                    timeout=self.config.timeout,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                    },
                )

                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After", "?")
                    log_proxy(f"{Fore.YELLOW}RATE{Fore.WHITE} {key} (Retry-After: {retry_after})")
                    self.proxy_manager.mark_failed(proxy)
                    continue

                if resp.status_code == 200:
                    data  = resp.json()
                    taken = data.get("taken", True)
                    self.proxy_manager.mark_success(proxy)
                    self._on_proxy_worked(key)

                    if not taken:
                        log_success(
                            f"Username '{Fore.GREEN}{Style.BRIGHT}{username}{Style.RESET_ALL}' "
                            f"is {Fore.GREEN}AVAILABLE{Fore.WHITE}!"
                        )
                        return username, self.AVAILABLE
                    else:
                        log_info(
                            f"'{Fore.LIGHTBLACK_EX}{username}{Fore.WHITE}' is {Fore.RED}taken{Fore.WHITE}"
                        )
                        return username, self.TAKEN

                # HTTP Error
                self.proxy_manager.mark_failed(proxy)
                if attempt == self.config.max_retries:
                    log_error(f"HTTP {resp.status_code} para '{username}' (todas as tentativas falharam)")

            except (requests.exceptions.ProxyError,
                    requests.exceptions.ConnectTimeout,
                    requests.exceptions.ReadTimeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.RequestException) as exc:
                log_debug(f"[{username}] {type(exc).__name__}: {str(exc)[:100]} via {key}")
                self.proxy_manager.mark_failed(proxy)
                if attempt == self.config.max_retries:
                    log_error(f"Todas as tentativas falharam para '{Fore.YELLOW}{username}{Fore.WHITE}'")
            except Exception:
                log_debug(f"[{username}] Exceção inesperada:\n{traceback.format_exc()}")
                self.proxy_manager.mark_failed(proxy)

        return username, self.ERROR

    def _on_proxy_worked(self, key: str) -> None:
        """Detecta quando uma proxy diferente começa a funcionar e loga a transição."""
        with self._last_proxy_lock:
            if self._last_working_proxy != key:
                if self._last_working_proxy is not None:
                    log_proxy(
                        f"{Fore.GREEN}TROCOU{Fore.WHITE} "
                        f"{Fore.LIGHTBLACK_EX}{self._last_working_proxy}{Fore.WHITE} → "
                        f"{Fore.GREEN}{key}{Fore.WHITE}"
                    )
                self._last_working_proxy = key
                self.proxy_manager.notify_new_active(key)

    def soonest_available(self) -> float:
        return self.proxy_manager.soonest_available()


# ---------------------------------------------------------------------------
# Webhook Notifier
# ---------------------------------------------------------------------------

class WebhookNotifier:
    def __init__(self, webhook_url: str, timeout: int = 15):
        self.webhook_url = webhook_url
        self.timeout     = timeout
        self._enabled    = bool(webhook_url) and "YOUR_WEBHOOK" not in webhook_url

    def notify(self, username: str) -> bool:
        if not self._enabled:
            return False
        payload = {
            "embeds": [{
                "title": "✅ Username Available",
                "description": f"**`{username}`** is available on Discord!",
                "color": 0x57F287,
                "timestamp": datetime.utcnow().isoformat(),
                "footer": {"text": "Discord Username Checker"},
            }]
        }
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=self.timeout,
                                 headers={"Content-Type": "application/json"})
            if resp.status_code in (200, 204):
                log_info(f"Webhook enviado para '{Fore.GREEN}{username}{Fore.WHITE}'")
                return True
            elif resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 5))
                log_rate(f"Webhook rate limited — retry em {retry_after:.1f}s")
                time.sleep(retry_after)
                return self.notify(username)
            else:
                log_error(f"Webhook retornou HTTP {resp.status_code}")
                return False
        except requests.exceptions.RequestException as exc:
            log_error(f"Webhook error: {str(exc)[:80]}")
            return False


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class Application:
    def __init__(self):
        self.config        = Config()
        self.proxy_manager = ProxyManager(PROXY_PATH, self.config.proxy_cooldown)
        self.checker       = UsernameChecker(self.config, self.proxy_manager)
        self.notifier      = WebhookNotifier(self.config.webhook_url, self.config.timeout)
        self._shutdown     = threading.Event()
        self._file_lock    = threading.Lock()
        self.stats_lock    = threading.Lock()
        self.stats         = {"checked": 0, "available": 0, "taken": 0, "errors": 0}

    def show_menu(self) -> tuple[str, int, str]:
        """Retorna (mode, length, required_sequence)."""
        print(BANNER)
        print(f"  {Fore.LIGHTBLACK_EX}Usernames disponíveis serão salvos em {Fore.YELLOW}list.txt{Style.RESET_ALL}\n")
        print(f"  {Fore.WHITE}{Style.BRIGHT}Tipo de caractere:{Style.RESET_ALL}\n")
        print(f"    {Fore.CYAN}[1]{Fore.WHITE}  Só {Fore.YELLOW}letras{Fore.WHITE} (a-z)")
        print(f"    {Fore.CYAN}[2]{Fore.WHITE}  Só {Fore.YELLOW}números{Fore.WHITE} (0-9)")
        print(f"    {Fore.CYAN}[3]{Fore.WHITE}  {Fore.YELLOW}Letras + números{Fore.WHITE} (a-z, 0-9)")
        print(f"    {Fore.CYAN}[4]{Fore.WHITE}  {Fore.YELLOW}Letras + números + .{Fore.WHITE} (a-z, 0-9, .)")
        print(f"    {Fore.CYAN}[5]{Fore.WHITE}  {Fore.YELLOW}Letras + números + . + _{Fore.WHITE} (a-z, 0-9, ., _)")
        print()

        mode_map = {"1": "letters", "2": "numbers", "3": "alphanumeric",
                    "4": "alphanumeric_dot", "5": "alphanumeric_dot_under"}
        while True:
            try:
                choice = input(f"  {Fore.CYAN}>{Fore.WHITE} Escolha (1-5): ").strip()
                if choice in mode_map:
                    mode = mode_map[choice]
                    break
                log_warn("Digite 1, 2, 3, 4 ou 5.")
            except (EOFError, KeyboardInterrupt):
                print(); sys.exit(0)

        while True:
            try:
                length = int(input(f"  {Fore.CYAN}>{Fore.WHITE} Comprimento do username (ex: 3, 4, 5): ").strip())
                if 2 <= length <= 20:
                    break
                log_warn("Digite um número entre 2 e 20.")
            except ValueError:
                log_warn("Número inválido.")
            except (EOFError, KeyboardInterrupt):
                print(); sys.exit(0)

        print(f"\n  {Fore.WHITE}{Style.BRIGHT}Sequência obrigatória:{Style.RESET_ALL}\n")
        print(f"    {Fore.CYAN}[1]{Fore.WHITE}  Nenhuma — totalmente aleatório")
        print(f"    {Fore.CYAN}[2]{Fore.WHITE}  Digitar sequência específica {Fore.LIGHTBLACK_EX}(ex: ll, nn, 99, abc)")
        print(f"    {Fore.CYAN}[3]{Fore.WHITE}  Par repetido aleatório {Fore.LIGHTBLACK_EX}(ex: aa, bb, 00, 11 — muda a cada username)")
        print()

        seq      = ""
        seq_mode = "none"
        try:
            seq_choice = input(f"  {Fore.CYAN}>{Fore.WHITE} Escolha (1-3): ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); sys.exit(0)

        if seq_choice == "2":
            try:
                seq = input(f"  {Fore.CYAN}>{Fore.WHITE} Sequência: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(); sys.exit(0)
            if seq:
                seq_mode = "fixed"
                log_info(f"Todos os usernames conterão '{Fore.YELLOW}{seq}{Fore.WHITE}'")
        elif seq_choice == "3":
            seq_mode = "random_repeat"
            log_info(f"Cada username terá um par repetido aleatório {Fore.LIGHTBLACK_EX}(aa, bb, 00 …)")

        if seq_mode == "random_repeat":
            seq = "__repeat__"
        elif seq_mode == "none":
            seq = ""

        return mode, length, seq

    def _ask_count(self) -> int:
        while True:
            try:
                n = int(input(f"  {Fore.CYAN}>{Fore.WHITE} Quantos usernames gerar? ").strip())
                if n > 0:
                    return n
                log_warn("Digite um número positivo.")
            except ValueError:
                log_warn("Número inválido.")
            except (EOFError, KeyboardInterrupt):
                print(); sys.exit(0)

    # ---- worker ----

    def _worker(self, username: str) -> None:
        if self._shutdown.is_set():
            return
        time.sleep(random.uniform(0.05, 0.15))
        try:
            name, status = self.checker.check(username)
            with self.stats_lock:
                self.stats["checked"] += 1
                if status == UsernameChecker.AVAILABLE:
                    self.stats["available"] += 1
                elif status == UsernameChecker.TAKEN:
                    self.stats["taken"] += 1
                else:
                    self.stats["errors"] += 1
            if status == UsernameChecker.AVAILABLE:
                self._save(name)
                self.notifier.notify(name)
        except Exception:
            log_debug(f"[_worker] Exceção para '{username}':\n{traceback.format_exc()}")
            with self.stats_lock:
                self.stats["checked"] += 1
                self.stats["errors"] += 1

    def _save(self, username: str) -> None:
        with self._file_lock:
            with open(RESULTS_PATH, "a", encoding="utf-8") as f:
                f.write(username + "\n")

    # ---- run ----

    def run(self) -> None:
        mode, length, seq = self.show_menu()
        count = self._ask_count()

        mode_names = {"letters": "letras", "numbers": "números", "alphanumeric": "alfanumérico",
                      "alphanumeric_dot": "alfanumérico+.", "alphanumeric_dot_under": "alfanumérico+._"}
        if seq == "__repeat__":
            seq_info = f" com {Fore.YELLOW}par repetido aleatório{Fore.WHITE}"
        elif seq:
            seq_info = f" com sequência '{Fore.YELLOW}{seq}{Fore.WHITE}'"
        else:
            seq_info = ""
        log_info(f"Gerando {Fore.YELLOW}{count}{Fore.WHITE} usernames de {length} chars "
                 f"({mode_names[mode]}){seq_info} …")

        usernames = UsernameGenerator.generate(count, length, mode, seq)
        if not usernames:
            log_error("Nenhum username para verificar.")
            return

        log_info(f"Iniciando verificação de {Fore.YELLOW}{len(usernames)}{Fore.WHITE} usernames "
                 f"com {Fore.YELLOW}{self.config.concurrency}{Fore.WHITE} threads …")
        print(f"  {Fore.LIGHTBLACK_EX}{'─' * 58}")

        monitor    = SystemMonitor(interval=30)
        monitor.start()
        start_time = time.time()

        try:
            with ThreadPoolExecutor(max_workers=self.config.concurrency) as executor:
                futures = {executor.submit(self._worker, u): u for u in usernames}
                for future in as_completed(futures):
                    if self._shutdown.is_set():
                        break
                    try:
                        future.result()
                    except Exception as exc:
                        log_error(f"Erro inesperado na thread: {exc}")
                        log_debug(traceback.format_exc())
        except KeyboardInterrupt:
            self._shutdown.set()
            log_warn("Interrompido — encerrando …")
        except Exception as exc:
            log_error(f"Loop principal crashou: {exc}")
            log_debug(traceback.format_exc())
        finally:
            monitor.stop()

        self._print_summary(time.time() - start_time)

    def _print_summary(self, elapsed: float) -> None:
        print(f"\n  {Fore.LIGHTBLACK_EX}{'─' * 58}")
        print(f"  {Fore.WHITE}{Style.BRIGHT}Resumo{Style.RESET_ALL}\n")
        print(f"    {Fore.CYAN}Verificados : {Fore.WHITE}{self.stats['checked']}")
        print(f"    {Fore.GREEN}Disponíveis : {Fore.WHITE}{self.stats['available']}")
        print(f"    {Fore.RED}Ocupados    : {Fore.WHITE}{self.stats['taken']}")
        print(f"    {Fore.YELLOW}Erros       : {Fore.WHITE}{self.stats['errors']}")
        print(f"    {Fore.MAGENTA}Tempo       : {Fore.WHITE}{elapsed:.1f}s")
        print(f"  {Fore.LIGHTBLACK_EX}{'─' * 58}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _handle_sigint(sig, frame):
    print(f"\n  {Fore.YELLOW}[WARN]    Interrompido.{Style.RESET_ALL}")
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        Application().run()
    except SystemExit:
        pass
    except Exception as exc:
        log_error(f"Erro fatal: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
